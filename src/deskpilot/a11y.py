"""Read an application's AT-SPI accessibility tree."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, cast

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import DBusErrorResponse, unwrap_msg

from . import screen
from .hypr import Window

SESSION_BUS_NAME = "org.a11y.Bus"
SESSION_BUS_PATH = "/org/a11y/bus"
SESSION_BUS_INTERFACE = "org.a11y.Bus"
A11Y_BUS_NAME = "org.a11y.atspi.Registry"
A11Y_ROOT_PATH = "/org/a11y/atspi/accessible/root"
ACCESSIBLE_INTERFACE = "org.a11y.atspi.Accessible"
COMPONENT_INTERFACE = "org.a11y.atspi.Component"
TEXT_INTERFACE = "org.a11y.atspi.Text"
SELECTION_INTERFACE = "org.a11y.atspi.Selection"
APPLICATION_INTERFACE = "org.a11y.atspi.Application"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
DBUS_INTERFACE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
COORDINATE_SCREEN = 0
COORDINATE_WINDOW = 1
NULL_PATH = "/org/a11y/atspi/null"
# AT-SPI's RelationType for "this element is labelled by those".
RELATION_LABELLED_BY = 2
# How far up from an element its toplevel may be before the walk gives up on a tree that loops.
MAX_ANCESTORS = 200
# How many elements one read of a tree takes unless the caller says: each costs the app several D-Bus round trips.
MAX_NODES = 2000
# How many levels one read nests. Each level is two levels of JSON, an element and its list of children, and what
# carries a tree to its reader stops at a fixed depth: pydantic, which serialises an MCP result, at 255, serde_json,
# which many callers parse with, at 128, and Python's own encoder at its call stack. 50 levels is 100 of JSON, clear
# of all three with the envelope around it. Deeper elements stay unread for the next batch, as past the node budget.
MAX_DEPTH = 50
CALL_TIMEOUT = 2.0
# D-Bus errors meaning the element's app or object no longer exists, so an id that named it is stale. Any other
# failure, such as a timeout, is the bus being unavailable. GDBus, which GTK serves its tree over, answers a call on
# the path of an accessible it has unregistered with UnknownMethod ("No such interface org.a11y.atspi.Accessible"),
# not UnknownObject: a dialog's button read just after its click closed the dialog failed so. Every method called here
# is one the object listed in GetInterfaces or that all accessibles have, so UnknownMethod means the object went.
GONE_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.UnknownObject",
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.DBus.Error.UnknownMethod",
    }
)
# The app's whole bus name is gone, not one of its objects: its tree cannot be read on with some elements left out.
APP_GONE_ERRORS = frozenset({"org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"})
NO_POSITION = -(2**31)
EMBEDDED_OBJECT = "\ufffc"
TEXT_LIMIT = 4000

_STATE_NAMES = (
    "invalid",
    "active",
    "armed",
    "busy",
    "checked",
    "collapsed",
    "defunct",
    "editable",
    "enabled",
    "expandable",
    "expanded",
    "focusable",
    "focused",
    "has tooltip",
    "horizontal",
    "iconified",
    "modal",
    "multi line",
    "multi selectable",
    "opaque",
    "pressed",
    "resizable",
    "selectable",
    "selected",
    "sensitive",
    "showing",
    "single line",
    "stale",
    "transient",
    "vertical",
    "visible",
    "manages descendants",
    "indeterminate",
    "required",
    "truncated",
    "animated",
    "invalid entry",
    "supports autocompletion",
    "selectable text",
    "is default",
    "visited",
    "checkable",
    "has popup",
    "read only",
)


class A11yError(RuntimeError):
    """An accessibility operation failed with an actionable message. `code` is its kind: no_tree (the app exposes
    none), ambiguous (several toplevels could be the window, or several popups the element's), stale (the element or
    its popup is gone, changed, or from an older snapshot), gone (the window's toplevel closed while its tree was
    read), offscreen (it is not showing), refused (it has no position) or unavailable (the accessibility bus
    failed)."""

    def __init__(self, message: str, code: str = "unavailable"):
        super().__init__(message)
        self.code = code


class _Bus(Protocol):
    def call(
        self,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        signature: str,
        body: tuple[object, ...],
    ) -> object: ...


class _JeepneyBus:
    """Small blocking Jeepney adapter used by the tree reader."""

    def __init__(self, connection: Any):
        self._connection = connection

    def call(
        self,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        signature: str,
        body: tuple[object, ...],
    ) -> object:
        address = DBusAddress(object_path, bus_name=destination, interface=interface)
        message = new_method_call(address, method, signature or None, body)
        reply = self._connection.send_and_get_reply(message, timeout=CALL_TIMEOUT)
        return unwrap_msg(reply)

    def close(self) -> None:
        self._connection.close()


@dataclass(frozen=True)
class Element:
    id: str
    role: str
    name: str
    states: tuple[str, ...]
    text: str | None
    x: int
    y: int
    width: int
    height: int
    children: tuple[Element, ...]
    truncated: bool = False  # on the root: the node budget ended the read before the whole tree was read
    snapshot: str = ""  # on the root: the id of the snapshot whose element ids these are
    parent: str = ""  # at the top of a later batch: the id of the element, read in an earlier batch, this hangs under

    def _own(self) -> dict:
        result = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "states": list(self.states),
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "children": [],
        }
        if self.parent:
            result["parent"] = self.parent
        if self.truncated:
            result["truncated"] = True
        if self.snapshot:
            result["snapshot"] = self.snapshot
        if self.truncated and self.snapshot:
            # The rest of the tree is kept with the snapshot: `more` reads its next batch.
            result["more"] = self.snapshot
        return result

    def to_dict(self) -> dict:
        """Return this element and its descendants as plain JSON data. A loop, not recursion: a tree may be deeper
        than Python's call stack."""
        result = self._own()
        pending: list[tuple[Element, dict]] = [(self, result)]
        while pending:
            element, data = pending.pop()
            for child in element.children:
                own = child._own()
                data["children"].append(own)
                pending.append((child, own))
        return result


@dataclass(frozen=True)
class Batch:
    """A later batch of a snapshot's tree: the subtrees read, each naming the `parent` it hangs under."""

    snapshot: str
    elements: tuple[Element, ...]
    truncated: bool = False  # more of the tree is still unread

    def to_dict(self) -> dict:
        result: dict = {"snapshot": self.snapshot, "elements": [element.to_dict() for element in self.elements]}
        if self.truncated:
            result["truncated"] = True
            result["more"] = self.snapshot
        return result


def narrow(data: dict, query: str = "", showing: bool = False) -> dict:
    """What `Element.to_dict` or `Batch.to_dict` gave, cut down to the elements a caller asked for and the ancestors
    that hold them, ids unchanged: those whose role, name or text contains `query`, whatever its case, and with
    `showing` only those the app says are on screen. `read` counts the elements looked through and `matched` those
    that fit, so a short answer is never taken for a small tree. A loop, not recursion, as `to_dict` is."""
    if not query and not showing:
        return data
    wanted = query.casefold()
    read = matched = 0

    def fits(element: dict) -> bool:
        if showing and "showing" not in element["states"]:
            return False
        return any(wanted in str(element.get(key) or "").casefold() for key in ("role", "name", "text"))

    def kept(top: dict) -> dict | None:
        nonlocal read, matched
        # Children are settled before their parent: each open entry is (element, its copy, whether it or one below
        # it fits, the index of its next child).
        done: dict | None = None
        open_: list[list] = [[top, {**top, "children": []}, False, 0]]
        while open_:
            entry = open_[-1]
            element, copy, _, index = entry
            if index == 0:
                read += 1
                if fits(element):
                    matched += 1
                    entry[2] = True
            if index < len(element["children"]):
                entry[3] += 1
                child = element["children"][index]
                open_.append([child, {**child, "children": []}, False, 0])
                continue
            open_.pop()
            if open_:
                if entry[2]:
                    open_[-1][1]["children"].append(copy)
                    open_[-1][2] = True
            else:
                done = copy if entry[2] else None
        return done

    if "elements" in data:
        tops = [found for found in map(kept, data["elements"]) if found is not None]
        return {**data, "elements": tops, "read": read, "matched": matched}
    # The root stays whatever fits: it carries the snapshot.
    found = kept(data)
    return {**(found or {**data, "children": []}), "read": read, "matched": matched}


@dataclass(frozen=True)
class Popup:
    """A mapped popup of a window, as the compositor places it: its id, its parent's (-1 on the window itself), `x`,`y`
    its window geometry's position and `sx`,`sy` its surface's origin, both in window pixels, and its size."""

    id: int
    parent: int
    x: float
    y: float
    sx: float
    sy: float
    width: float
    height: float


# A popup's position as GTK 3 reports it: the first-level key of an element on that popup.
Key = tuple[int, int]
Popups = Callable[[], list[Popup]]


@dataclass(frozen=True)
class Located:
    """Where an element is now. `x`,`y` are relative to the surface it is on: the window's, or for an element of a
    GTK 3 popup, that popup's. `keys` then gives the popup: the position GTK reports for each popup level from the
    first, which `resolve` maps to the compositor's. `known` is whether the surface was told at all (GTK 3 only)."""

    x: int
    y: int
    width: int
    height: int
    keys: tuple[Key, ...] = ()
    known: bool = False


def resolve(popups: list[Popup], keys: tuple[Key, ...]) -> Popup | None:
    """The popup an element with these keys is on, matched exactly, or None for the window's own surface. Its first
    level is the window's popup at the position GTK reports for it, which is the popup's window geometry position. Each
    deeper level is the single mapped child of the one before: GTK's own position for a nested popup misses the
    compositor's by its positioner offset, so it is not used. No keys means the element reports the toplevel's own
    offset (`_popup_key`), as one on a popup at the window's origin would too, so such a popup is refused."""
    if not keys:
        if any(popup.parent == -1 and (popup.x, popup.y) == (0, 0) for popup in popups):
            raise A11yError(
                "A popup of the window is at its origin, where the app places the window's own elements too: cannot "
                "tell which the element is on.",
                "ambiguous",
            )
        return None
    first = [popup for popup in popups if popup.parent == -1 and (popup.x, popup.y) == keys[0]]
    if not first:
        raise A11yError(
            f"No popup of the window is at {keys[0][0]},{keys[0][1]}, where the app places the element's: it closed or "
            "moved; take a new snapshot.",
            "stale",
        )
    if len(first) > 1:
        raise A11yError(
            f"{len(first)} popups of the window are at {keys[0][0]},{keys[0][1]}: cannot tell which.", "ambiguous"
        )
    popup = first[0]
    for level in range(2, len(keys) + 1):
        children = [child for child in popups if child.parent == popup.id]
        if not children:
            raise A11yError(f"The element's level {level} popup is not open; take a new snapshot.", "stale")
        if len(children) > 1:
            raise A11yError(
                f"{len(children)} popups are open at the element's level {level}: cannot tell which.", "ambiguous"
            )
        popup = children[0]
    return popup


def _elements_path(window: Window) -> Path:
    return screen.state_dir() / "elements" / f"{window.address}.json"


@contextmanager
def _bus_context(bus: _Bus | None):
    if bus is not None:
        yield bus
        return

    connection: _JeepneyBus | None = None
    try:
        session_connection = open_dbus_connection(bus="SESSION", auth_timeout=CALL_TIMEOUT)
        session = _JeepneyBus(session_connection)
        try:
            address_reply = session.call(
                SESSION_BUS_NAME,
                SESSION_BUS_PATH,
                SESSION_BUS_INTERFACE,
                "GetAddress",
                "",
                (),
            )
        finally:
            session.close()
        address = str(_reply_value(address_reply))
        if not address:
            raise A11yError("The accessibility bus returned an empty address.")
        connection = _JeepneyBus(open_dbus_connection(bus=address, auth_timeout=CALL_TIMEOUT))
        yield connection
    except A11yError:
        raise
    except Exception as exc:
        raise A11yError(f"Could not connect to the accessibility bus: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _reply_value(reply: object) -> object:
    if isinstance(reply, (tuple, list)) and len(reply) == 1:
        return reply[0]
    return reply


def _variant_value(value: object) -> object:
    if isinstance(value, (tuple, list)) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


def _call(
    bus: _Bus,
    destination: str,
    object_path: str,
    interface: str,
    method: str,
    signature: str = "",
    body: tuple[object, ...] = (),
) -> object:
    try:
        return bus.call(destination, object_path, interface, method, signature, body)
    except A11yError:
        raise
    except Exception as exc:
        # Only an app's own object can be gone; a registry that is not there is the bus being unavailable.
        gone = isinstance(exc, DBusErrorResponse) and exc.name in GONE_ERRORS and destination != A11Y_BUS_NAME
        code = "stale" if gone else "unavailable"
        raise A11yError(f"Could not call {interface}.{method} on {object_path}: {exc}", code) from exc


def _property(bus: _Bus, destination: str, object_path: str, interface: str, name: str) -> object:
    reply = _call(
        bus,
        destination,
        object_path,
        PROPERTIES_INTERFACE,
        "Get",
        "ss",
        (interface, name),
    )
    return _variant_value(_reply_value(reply))


def _as_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if value is None:
        return ()
    return tuple(str(item) for item in cast(Any, value))


def _children(bus: _Bus, destination: str, object_path: str) -> list[tuple[str, str]]:
    reply = _reply_value(_call(bus, destination, object_path, ACCESSIBLE_INTERFACE, "GetChildren"))
    if reply is None:
        return []
    try:
        return [(str(child_bus), str(child_path)) for child_bus, child_path in cast(Any, reply)]
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned invalid child references: {exc}") from exc


def _selected(bus: _Bus, destination: str, object_path: str, interfaces: tuple[str, ...]) -> set[tuple[str, str]]:
    """The objects a container says are chosen, through its Selection interface. A GTK combo box reports its active
    item only here: the item itself carries no selected state, and its name may be the combo box's label."""
    if SELECTION_INTERFACE not in interfaces:
        return set()
    count = _property(bus, destination, object_path, SELECTION_INTERFACE, "NSelectedChildren")
    try:
        count = int(cast(Any, count))
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned an invalid selection count: {count!r}") from exc
    chosen = set()
    for index in range(max(count, 0)):
        reply = _reply_value(
            _call(bus, destination, object_path, SELECTION_INTERFACE, "GetSelectedChild", "i", (index,))
        )
        try:
            child_bus, child_path = cast(Any, reply)
        except (TypeError, ValueError) as exc:
            raise A11yError(f"Accessibility object {object_path} returned an invalid selected child: {exc}") from exc
        chosen.add((str(child_bus), str(child_path)))
    return chosen


def _states(value: object) -> tuple[str, ...]:
    """GetState's `au`: a 64-bit StateType bitset as two 32-bit words, low word first."""
    try:
        low, high = (int(word) for word in cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object returned invalid states: {value!r}") from exc
    bits = low | high << 32
    return tuple(
        _STATE_NAMES[index] if index < len(_STATE_NAMES) else f"state-{index}"
        for index in range(64)
        if bits >> index & 1
    )


def _role(bus: _Bus, destination: str, object_path: str) -> str:
    return str(_reply_value(_call(bus, destination, object_path, ACCESSIBLE_INTERFACE, "GetRoleName")))


def _name(bus: _Bus, destination: str, object_path: str) -> str:
    name = str(_property(bus, destination, object_path, ACCESSIBLE_INTERFACE, "Name") or "")
    if name:
        return name
    # An element with no name of its own is named by what it says labels it, as a GTK 4 menu item is by the label
    # inside it. The label is read as it is now, so a label that went leaves the element unnamed.
    relations = _reply_value(_call(bus, destination, object_path, ACCESSIBLE_INTERFACE, "GetRelationSet"))
    labels: list[str] = []
    for relation in cast(Any, relations) or ():
        if int(relation[0]) != RELATION_LABELLED_BY:
            continue
        for label_bus, label_path in relation[1]:
            try:
                labels.append(_label(bus, str(label_bus), str(label_path)))
            except A11yError as exc:
                if not _object_gone(exc):
                    raise
    return " ".join(label for label in labels if label)


def _label(bus: _Bus, destination: str, object_path: str) -> str:
    name = str(_property(bus, destination, object_path, ACCESSIBLE_INTERFACE, "Name") or "")
    return name or (_text(bus, destination, object_path, _interfaces(bus, destination, object_path)) or "")


def _interfaces(bus: _Bus, destination: str, object_path: str) -> tuple[str, ...]:
    return _as_strings(_reply_value(_call(bus, destination, object_path, ACCESSIBLE_INTERFACE, "GetInterfaces")))


def _bounds(
    bus: _Bus, destination: str, object_path: str, coordinates: int = COORDINATE_WINDOW
) -> tuple[int, int, int, int]:
    reply = cast(
        tuple[Any, Any, Any, Any],
        _reply_value(
            _call(
                bus,
                destination,
                object_path,
                COMPONENT_INTERFACE,
                "GetExtents",
                "u",
                (coordinates,),
            )
        ),
    )
    try:
        x, y, width, height = (int(value) for value in reply)
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned invalid bounds: {reply!r}") from exc
    # A negative size is no size: ATK reports extents it cannot obtain as -1, and GTK 4 returns whatever was on its
    # stack for an accessible with no bounds, such as a stack page whose widget is not realized. Either has no place.
    # GTK 3 puts a tree view cell that is scrolled out of view at the smallest int for x and y, with its real size
    # and the showing state still set: that is its way of saying the cell has no place either.
    if width < 0 or height < 0 or NO_POSITION in (x, y):
        return 0, 0, 0, 0
    return x, y, width, height


def _gtk3(bus: _Bus, app_bus: str, app_path: str) -> bool:
    """Whether the app is GTK 3, the toolkit whose popup elements are placed through their popup. GTK 3 reports an
    element's WINDOW extents relative to the popup surface it is on, and its SCREEN extents shifted by that popup's
    position; GTK 4 reports popover content relative to the toplevel, so it and an app that does not say are taken as
    they report."""
    try:
        name = _property(bus, app_bus, app_path, APPLICATION_INTERFACE, "ToolkitName")
        version = _property(bus, app_bus, app_path, APPLICATION_INTERFACE, "Version")
    except A11yError:
        return False
    return str(name).casefold() == "gtk" and str(version).startswith("3.")


def _offset(bus: _Bus, destination: str, object_path: str, window: tuple[int, int, int, int]) -> Key:
    """SCREEN minus WINDOW origin: in GTK 3, the position of the popup an element is on, and the toplevel's own
    offset for one on the toplevel."""
    screen_x, screen_y, _, _ = _bounds(bus, destination, object_path, COORDINATE_SCREEN)
    return screen_x - window[0], screen_y - window[1]


def _root_offset(bus: _Bus, root: tuple[str, str]) -> Key:
    if COMPONENT_INTERFACE not in _interfaces(bus, *root):
        return 0, 0
    return _offset(bus, *root, _bounds(bus, *root))


def _popup_key(bus: _Bus, element: tuple[str, str], bounds: tuple[int, int, int, int], root_offset: Key) -> Key | None:
    """GTK 3: the first-level key of the popup an element is on, its offset over the toplevel's, or None when it has
    the toplevel's own offset: it is on the window's own surface, or on a popup at the window's origin, which reports
    the same. The popup layer distinguishes those when the app supplies it. Snapshot placement and live lookup
    both classify through this."""
    offset = _offset(bus, *element, bounds)
    key = (offset[0] - root_offset[0], offset[1] - root_offset[1])
    if key != (0, 0):
        return key
    # AT-SPI Layer.POPUP is 5. GTK 3 reports it for menus and their items, including a popup whose position is
    # identical to the toplevel's. Without that explicit distinction the origin stays ambiguous.
    try:
        layer = _reply_value(_call(bus, *element, COMPONENT_INTERFACE, "GetLayer"))
    except A11yError:
        return None
    return key if layer == 5 else None


def _text(bus: _Bus, destination: str, object_path: str, interfaces: tuple[str, ...]) -> str | None:
    if TEXT_INTERFACE not in interfaces:
        return None
    count = _property(bus, destination, object_path, TEXT_INTERFACE, "CharacterCount")
    try:
        count = int(cast(Any, count))
    except (TypeError, ValueError) as exc:
        raise A11yError(f"Accessibility object {object_path} returned invalid text length: {count!r}") from exc
    if count < 0:
        raise A11yError(f"Accessibility object {object_path} returned a negative text length.")
    length = min(count, TEXT_LIMIT)
    if length == 0:
        return ""
    reply = _reply_value(
        _call(
            bus,
            destination,
            object_path,
            TEXT_INTERFACE,
            "GetText",
            "ii",
            (0, length),
        )
    )
    # AT-SPI puts U+FFFC in a text where a child element sits. The child is listed as an element of its own, so the
    # mark says nothing: a Chromium page's document was 121 of them and no words.
    text = str(reply).replace(EMBEDDED_OBJECT, "")
    if count > TEXT_LIMIT or len(text) > TEXT_LIMIT:
        return text[:TEXT_LIMIT] + " [truncated]"
    return text


def _app_for_pid(bus: _Bus, pid: int) -> tuple[str, str]:
    app_refs = _children(bus, A11Y_BUS_NAME, A11Y_ROOT_PATH)
    for app_bus, app_path in app_refs:
        try:
            process_id = _reply_value(
                _call(
                    bus,
                    DBUS_INTERFACE,
                    DBUS_PATH,
                    DBUS_INTERFACE,
                    "GetConnectionUnixProcessID",
                    "s",
                    (app_bus,),
                )
            )
            if int(cast(Any, process_id)) == pid:
                return app_bus, app_path
        except (A11yError, TypeError, ValueError):
            continue
    raise A11yError(
        f"Application PID {pid} exposes no accessibility tree. Chromium and Electron join the accessibility bus only "
        "when ACCESSIBILITY_ENABLED=1 is in their environment (launch sets it) or the a11y bus property "
        "org.a11y.Status.IsEnabled is true before they start, and expose their pages only when also started with "
        "--force-renderer-accessibility. Firefox too needs accessibility on when it starts. A Qt app joins the bus "
        "only when QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 is in its environment (launch sets it) or a screen reader is "
        "on as it starts, and a Qt Quick app may still expose none. Wine and XWayland games expose none.",
        "no_tree",
    )


def _object_gone(exc: A11yError) -> bool:
    """Whether the app removed one accessible, rather than the app's bus name or anything else failing."""
    cause = exc.__cause__
    return exc.code == "stale" and not (isinstance(cause, DBusErrorResponse) and cause.name in APP_GONE_ERRORS)


def _choose_toplevel(bus: _Bus, app_bus: str, app_path: str, window: Window) -> tuple[str, str]:
    candidates = []
    names = []
    refs = []
    skipped = False
    for ref in _children(bus, app_bus, app_path):
        try:
            role = _role(bus, *ref)
            name = _name(bus, *ref)
        except A11yError as exc:
            if not _object_gone(exc):
                raise
            # A toplevel of the app, such as a dialog that just closed, went while the list was read.
            skipped = True
            continue
        names.append(name)
        refs.append(ref)
        if role.casefold() in {"frame", "window", "dialog"}:
            candidates.append((ref, role, name))

    # A toplevel that went while the list was read may have been this window, and its title is unknown, so even one
    # left with the window's title may be a sibling sharing it: the match cannot be proven.
    if skipped:
        raise A11yError(
            f"A toplevel of the app closed while {window.title!r} was looked for, so which one is this window "
            "cannot be told.",
            "gone",
        )
    # Every child of the application object is one of its toplevels whatever role it gives: Qt reports a message box
    # as an "alert message" and Qt 5 a plain QWidget window, such as qt5ct's, as a "filler". The one toplevel carrying
    # the window's exact title is it, before a lone toplevel in a window role, which may be the dialog's parent.
    titled = [ref for ref, name in zip(refs, names, strict=True) if name == window.title]
    if len(titled) == 1:
        return titled[0]
    if len(candidates) == 1:
        return candidates[0][0]
    exact = [candidate for candidate in candidates if candidate[2] == window.title]
    if len(exact) == 1:
        return exact[0][0]
    listed = ", ".join(repr(name) for name in names) or "none"
    raise A11yError(
        f"Could not choose an accessibility toplevel for {window.title!r}: candidates are {listed}. "
        "The title must match exactly and uniquely.",
        "ambiguous",
    )


@dataclass
class _Walk:
    """What a snapshot's read carries from one batch to the next."""

    records: dict[str, dict[str, str]]
    # Every accessible reached so far: one seen again is left out, so a tree that loops back on itself ends.
    seen: set[tuple[str, str]]
    # What each container with a Selection reports as chosen, as it is reached: a chosen item lies below it.
    chosen: set[tuple[str, str]]
    count: int = 0  # ids given out: the next element is e{count + 1}
    # The unread part of the tree in document order: (parent id, bus, path, the parent's popup keys).
    frontier: list[tuple[str, str, str, tuple[Key, ...]]] = field(default_factory=list)


def _write_records(window: Window, snapshot_id: str, state: _Walk, gtk3: bool, root: tuple[str, str]) -> None:
    path = _elements_path(window)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "address": window.address,
            "instance": window.instance,
            "pid": window.pid,
            "stable_id": window.stable_id,
            "snapshot": snapshot_id,
            "elements": state.records,
            "gtk3": gtk3,
            "root": list(root),
            "count": state.count,
            "chosen": sorted(state.chosen),
            "frontier": [
                {"parent": parent, "bus": bus, "path": object_path, "keys": keys}
                for parent, bus, object_path, keys in state.frontier
            ],
        }
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        raise A11yError(f"Could not persist accessibility elements for {window.address}: {exc}") from exc


class _Placer:
    """Places a GTK 3 snapshot's elements in window pixels, asking for the window's popups once, when the first showing
    element is reached: a popup at the window's origin makes the window's own elements ambiguous too."""

    def __init__(self, bus: _Bus, popups: Popups | None, root_offset: Key):
        self._bus = bus
        self._popups = popups
        self._mapped: list[Popup] | None = None
        self._root_offset = root_offset

    def place(
        self,
        destination: str,
        object_path: str,
        bounds: tuple[int, int, int, int],
        showing: bool,
        keys: tuple[Key, ...],
    ) -> tuple[tuple[int, int, int, int], tuple[Key, ...]]:
        """The element's bounds in window pixels, and the popup keys of its path for its children. An element on a
        popup that is not showing, or whose surface cannot be told exactly, has zero bounds."""
        key = _popup_key(self._bus, (destination, object_path), bounds, self._root_offset)
        if key is not None and (not keys or keys[-1] != key):
            keys = (*keys, key)
        if not showing:
            return (0, 0, 0, 0) if key is not None else bounds, keys
        if self._mapped is None:
            self._mapped = self._popups() if self._popups is not None else []
        try:
            popup = resolve(self._mapped, keys if key is not None else ())
        except A11yError:
            return (0, 0, 0, 0), keys
        if popup is None:
            return bounds, keys
        x, y, width, height = bounds
        return (round(popup.sx + x), round(popup.sy + y), width, height), keys


def _leave_out(exc: A11yError, records: dict[str, dict[str, str]], recorded: int) -> None:
    """Drop the records a walk of an element added after the first `recorded`, when the app removed that element while
    the tree was read, as a dialog's button goes once a click on it closes the dialog: it is no longer in the tree.
    Any other failure, the app's bus name going among them, is raised."""
    if not _object_gone(exc):
        raise exc
    for gone in list(records)[recorded:]:
        del records[gone]


@contextmanager
def _closed_is_gone():
    """A snapshot reads only the app's root, the window's toplevel and what the walk keeps, so any of those found
    closed, from choosing the toplevel to the last element, is the window or its app gone, not a stale element id."""
    try:
        yield
    except A11yError as exc:
        if exc.code != "stale":
            raise
        raise A11yError(f"The window's accessible closed while its tree was read: {exc}", "gone") from exc


class _FrontierGone(Exception):
    """An accessible a snapshot kept to read in a later batch is no longer there."""


@dataclass
class _Node:
    """An element whose own fields are read and whose children are being read."""

    id: str
    parent: str
    fields: tuple[str, str, tuple[str, ...], str | None, int, int, int, int]
    refs: list[tuple[str, str]]
    keys: tuple[Key, ...]
    children: list[Element] = field(default_factory=list)
    index: int = 0

    def element(self) -> Element:
        role, name, states, text, x, y, width, height = self.fields
        return Element(self.id, role, name, states, text, x, y, width, height, tuple(self.children), parent=self.parent)


def _read_batch(
    connection: _Bus, state: _Walk, max_nodes: int, placer: _Placer | None, kept: bool = False
) -> list[Element]:
    """Read up to `max_nodes` elements from the front of the frontier, depth first in document order, and leave
    what is still unread as the new frontier. Nothing is dropped: the read stops at the budget, and every child not
    yet read stays on the frontier under its parent's id. A batch nests at most MAX_DEPTH levels: the children of an
    element at that depth stay on the frontier too. A loop with its own stack, so no depth is too deep. `kept`
    says the frontier was kept from an earlier batch: one of its accessibles gone is then the tree having changed."""
    entries = state.frontier
    position = 0
    tops: list[Element] = []
    stack: list[_Node] = []
    below: list[tuple[str, str, str, tuple[Key, ...]]] = []
    read = 0
    while True:
        if stack:
            node = stack[-1]
            if len(stack) == MAX_DEPTH:
                below.extend((node.id, *ref, node.keys) for ref in node.refs if ref not in state.seen)
                node.index = len(node.refs)
            if node.index == len(node.refs):
                stack.pop()
                (stack[-1].children if stack else tops).append(node.element())
                continue
            parent, ref, keys = node.id, node.refs[node.index], node.keys
        elif position < len(entries):
            parent, *found, keys = entries[position]
            ref = (found[0], found[1])
        else:
            break
        if ref not in state.seen and read >= max_nodes:
            break
        if stack:
            stack[-1].index += 1
        else:
            position += 1
        if ref in state.seen:
            continue
        state.seen.add(ref)
        read += 1
        recorded = len(state.records)
        try:
            stack.append(_read_node(connection, state, placer, ref, keys, "" if stack else parent))
        except A11yError as exc:
            if kept and not stack and _object_gone(exc):
                raise _FrontierGone(ref[1]) from exc
            _leave_out(exc, state.records, recorded)
    # What lies below the deepest level read comes first, then, where the budget ended the read, what each open
    # element has left, deepest first, as a walk would go on.
    state.frontier = [
        *below,
        *((node.id, *ref, node.keys) for node in reversed(stack) for ref in node.refs[node.index :]),
        *entries[position:],
    ]
    while stack:
        node = stack.pop()
        (stack[-1].children if stack else tops).append(node.element())
    return tops


def _read_node(
    connection: _Bus, state: _Walk, placer: _Placer | None, ref: tuple[str, str], keys: tuple[Key, ...], parent: str
) -> _Node:
    destination, object_path = ref
    state.count += 1
    element_id = f"e{state.count}"
    role = _role(connection, destination, object_path)
    name = _name(connection, destination, object_path)
    state.records[element_id] = {"bus": destination, "path": object_path, "role": role, "name": name}
    states = _states(_reply_value(_call(connection, destination, object_path, ACCESSIBLE_INTERFACE, "GetState")))
    interfaces = _interfaces(connection, destination, object_path)
    state.chosen.update(_selected(connection, destination, object_path, interfaces))
    if ref in state.chosen and "selected" not in states:
        states = (*states, "selected")
    bounds = _bounds(connection, destination, object_path) if COMPONENT_INTERFACE in interfaces else (0, 0, 0, 0)
    if placer is not None and COMPONENT_INTERFACE in interfaces:
        bounds, keys = placer.place(destination, object_path, bounds, "showing" in states, keys)
    text = _text(connection, destination, object_path, interfaces)
    refs = _children(connection, destination, object_path)
    return _Node(element_id, parent, (role, name, states, text, *bounds), refs, keys)


def _budget(max_nodes: int) -> None:
    if max_nodes < 1:
        raise ValueError(f"max_nodes is {max_nodes}: a batch reads at least one element")


def snapshot(
    window: Window,
    max_nodes: int = MAX_NODES,
    bus: _Bus | None = None,
    peek: bool = False,
    popups: Popups | None = None,
) -> Element:
    """Read and persist the accessibility tree for a Hyprland window, up to `max_nodes` elements. A larger tree is
    not cut: the root says `truncated`, the unread part is kept with the snapshot, and `more` reads it on, batch by
    batch. A peek reads the tree without replacing the latest snapshot, so ids taken from that snapshot stay good; its
    own ids name nothing, it has no snapshot id, and one the budget ends cannot be read on. Bounds are in window
    pixels: a GTK 3 popup element's are placed through its popup from `popups`, asked once and only if an element is
    showing, and are zero when its surface cannot be told exactly."""
    _budget(max_nodes)
    with _bus_context(bus) as connection, _closed_is_gone():
        app_bus, app_path = _app_for_pid(connection, window.pid)
        root = _choose_toplevel(connection, app_bus, app_path, window)
        gtk3 = _gtk3(connection, app_bus, app_path)
        placer = _Placer(connection, popups, _root_offset(connection, root)) if gtk3 else None
        state = _Walk({}, set(), set(), frontier=[("", *root, ())])
        tops = _read_batch(connection, state, max_nodes, placer)
        if not tops:
            # The toplevel went between being chosen and being read.
            raise A11yError(f"The window's accessible closed while its tree was read: {root[1]}", "gone")
        truncated = bool(state.frontier)
        # Ids are numbered afresh by each snapshot, so an id is checked against the snapshot it came from.
        if peek:
            return replace(tops[0], truncated=truncated)
        snapshot_id = f"{window.address.removeprefix('0x')}-{time.time_ns()}"
        _write_records(window, snapshot_id, state, gtk3, root)
        return replace(tops[0], truncated=truncated, snapshot=snapshot_id)


def more(
    window: Window, snapshot_id: str, max_nodes: int = MAX_NODES, bus: _Bus | None = None, popups: Popups | None = None
) -> Batch:
    """Read the next batch of a snapshot whose tree was larger than one read, up to `max_nodes` elements. Ids are
    numbered on from the batches before, which stay valid, and each subtree read names the `parent` it hangs under.
    The last batch is not `truncated`. Only the window's latest snapshot can be read on (stale otherwise), and an
    accessible kept for this batch that the app has since removed is stale too: the tree changed, so it is read
    anew."""
    _budget(max_nodes)
    payload = _load_payload(window, f"Snapshot {snapshot_id!r}")
    _latest(window, payload, snapshot_id, "read on from the new snapshot, or take a new one")
    try:
        records = {
            str(key): {str(name): str(value) for name, value in record.items()}
            for key, record in payload["elements"].items()
        }
        state = _Walk(
            records,
            {(record["bus"], record["path"]) for record in records.values()},
            {(str(bus_name), str(object_path)) for bus_name, object_path in payload["chosen"]},
            int(payload["count"]),
            [
                (
                    str(entry["parent"]),
                    str(entry["bus"]),
                    str(entry["path"]),
                    tuple((int(x), int(y)) for x, y in entry["keys"]),
                )
                for entry in payload["frontier"]
            ],
        )
        root_bus, root_path = payload["root"]
        root = (str(root_bus), str(root_path))
        gtk3 = bool(payload["gtk3"])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise A11yError(
            f"Snapshot {snapshot_id} of window {window.address} cannot be read on; take a new accessibility snapshot.",
            "stale",
        ) from exc
    if not state.frontier:
        return Batch(snapshot_id, ())
    with _bus_context(bus) as connection:
        try:
            with _closed_is_gone():
                placer = _Placer(connection, popups, _root_offset(connection, root)) if gtk3 else None
                tops = _read_batch(connection, state, max_nodes, placer, kept=True)
        except _FrontierGone as exc:
            raise A11yError(
                f"The tree of window {window.address} changed since snapshot {snapshot_id}: an element still to be "
                f"read ({exc}) is gone. Take a new accessibility snapshot.",
                "stale",
            ) from exc
    _write_records(window, snapshot_id, state, gtk3, root)
    return Batch(snapshot_id, tuple(tops), bool(state.frontier))


def _load_payload(window: Window, what: str) -> dict:
    """The window's persisted snapshot. Hyprland gives a closed window's address to a later one: a snapshot of
    another window names nothing here."""
    try:
        payload = json.loads(_elements_path(window).read_text(encoding="utf-8"))
        if [payload.get(key) for key in ("address", "instance", "pid", "stable_id")] != [
            window.address,
            window.instance,
            window.pid,
            window.stable_id,
        ]:
            raise KeyError(what)
        if not isinstance(payload.get("elements"), dict):
            raise KeyError(what)
    except (AttributeError, OSError, ValueError, KeyError, TypeError) as exc:
        raise A11yError(
            f"{what} is unknown for window {window.address}; take a new accessibility snapshot.", "stale"
        ) from exc
    return payload


def _latest(
    window: Window, payload: dict, snapshot_id: str, then: str = "take the element's id from the new snapshot"
) -> None:
    latest = str(payload.get("snapshot", ""))
    if snapshot_id != latest:
        raise A11yError(
            f"Snapshot {snapshot_id} of window {window.address} was replaced by snapshot {latest}, which numbers its "
            f"elements afresh; {then}.",
            "stale",
        )


def _load_record(window: Window, element_id: str, snapshot_id: str | None) -> tuple[dict[str, str], dict]:
    """The element's record and the snapshot it is from."""
    what = f"Element {element_id!r}"
    payload = _load_payload(window, what)
    record = payload["elements"].get(element_id)
    if not isinstance(record, dict) or not {"bus", "path"} <= record.keys():
        raise A11yError(f"{what} is unknown for window {window.address}; take a new accessibility snapshot.", "stale")
    if snapshot_id is not None:
        _latest(window, payload, snapshot_id)
    return {key: str(value) for key, value in record.items()}, payload


def _popup_keys(bus: _Bus, element: tuple[str, str], key: Key, root: tuple[str, str], root_offset: Key):
    """The keys of each popup level an element is on, from the first, read up its ancestors to its toplevel."""
    keys = [key]
    ref = element
    for _ in range(MAX_ANCESTORS):
        parent = cast(Any, _property(bus, *ref, ACCESSIBLE_INTERFACE, "Parent"))
        try:
            ref = (str(parent[0]), str(parent[1]))
        except (TypeError, IndexError) as exc:
            raise A11yError(f"Accessibility object {ref[1]} returned an invalid parent: {parent!r}") from exc
        if ref == root:
            return tuple(reversed(keys))
        if ref[1] == NULL_PATH:
            break
        if COMPONENT_INTERFACE not in _interfaces(bus, *ref):
            continue
        above = _popup_key(bus, ref, _bounds(bus, *ref), root_offset)
        if above is not None and above != keys[-1]:
            keys.append(above)
    raise A11yError(f"Element {element[1]} is no longer in the window's tree; take a new snapshot.", "stale")


def locate(window: Window, element_id: str, bus: _Bus | None = None) -> Located:
    """Read an element's current visible bounds from the application. The id is `e5`, from the window's latest
    snapshot, or `<snapshot>:e5`, which is refused unless that snapshot is still the latest. An element whose role or
    name has changed since its snapshot is stale: the id no longer names what the caller saw."""
    snapshot_id, _, element_id = element_id.rpartition(":")
    record, payload = _load_record(window, element_id, snapshot_id or None)
    destination, object_path = record["bus"], record["path"]
    with _bus_context(bus) as connection:
        try:
            interfaces = _interfaces(connection, destination, object_path)
            states = _states(
                _reply_value(_call(connection, destination, object_path, ACCESSIBLE_INTERFACE, "GetState"))
            )
            role = _role(connection, destination, object_path)
            name = _name(connection, destination, object_path)
        except A11yError as exc:
            if exc.code != "stale":
                raise
            raise A11yError(f"Element {element_id} is gone; take a new snapshot: {exc}", "stale") from exc
        # A record written before records kept role and name has nothing to compare.
        if "role" in record and (role, name) != (record["role"], record["name"]):
            raise A11yError(
                f"Element {element_id} was {record['role']} {record['name']!r} and is now {role} {name!r}; take a "
                "new snapshot.",
                "stale",
            )
        if COMPONENT_INTERFACE not in interfaces:
            raise A11yError(f"Element {element_id} has no position on screen; target another element.", "refused")
        if "showing" not in states:
            raise A11yError(
                f"Element {element_id} is not showing; scroll it into view or take a new snapshot.", "offscreen"
            )
        bounds = _bounds(connection, destination, object_path)
        if bounds[2] <= 0 or bounds[3] <= 0:
            raise A11yError(
                f"Element {element_id} reports no size; scroll it into view or take a new snapshot.", "offscreen"
            )
        root = payload.get("root")
        if payload.get("gtk3") is not True or not isinstance(root, list) or len(root) != 2:
            # WINDOW coordinates are relative to the app's toplevel, which is the Hyprland window's origin.
            return Located(*bounds)
        root = (str(root[0]), str(root[1]))
        root_offset = _root_offset(connection, root)
        key = _popup_key(connection, (destination, object_path), bounds, root_offset)
        if key is None:
            return Located(*bounds, known=True)
        return Located(*bounds, _popup_keys(connection, (destination, object_path), key, root, root_offset), True)
