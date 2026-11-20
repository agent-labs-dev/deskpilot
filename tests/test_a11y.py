import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jeepney.low_level import HeaderFields
from jeepney.wrappers import DBusErrorResponse

from deskpilot import a11y
from deskpilot.hypr import Window


def window(pid=42, title="Editor"):
    return Window("0xabc", "editor", title, 100, 200, 800, 600, 1, "1", False, pid)


class FakeBus:
    def __init__(self, pid=42, title="Editor", duplicate_title=False):
        self.calls = []
        self.bounds = {"/frame": (0, 0, 800, 600), "/dialog": (0, 0, 800, 600), "/button": (10, 20, 80, 20)}
        self.refs = {
            a11y.A11Y_ROOT_PATH: [("app.bus", "/app")],
            "/app": [("app.bus", "/frame"), ("app.bus", "/dialog")] if duplicate_title else [("app.bus", "/frame")],
            "/frame": [("app.bus", "/button")],
            "/dialog": [],
            "/button": [],
        }
        self.names = {"/frame": title, "/dialog": title, "/button": "Save"}
        self.roles = {"/frame": (23, "frame"), "/dialog": (16, "dialog"), "/button": (43, "push button")}
        # GetState's two 32-bit words of the StateType bitset, low first: enabled (8), focusable (11), showing (25),
        # visible (30), and for the button checkable (41) in the high word.
        visible = 1 << 8 | 1 << 25 | 1 << 30
        self.states = {
            "/frame": [visible, 0],
            "/dialog": [visible, 0],
            "/button": [visible | 1 << 11, 1 << (41 - 32)],
        }
        self.interfaces = {path: ["org.a11y.atspi.Component"] for path in self.names}
        self.interfaces["/button"].append("org.a11y.atspi.Text")
        self.text = {"/button": "Save"}
        self.pid = pid
        self.failure: Exception | None = None
        # What a container with a Selection interface reports as chosen, by its path.
        self.selected: dict[str, list[tuple[str, str]]] = {}
        # The app's org.a11y.atspi.Application ToolkitName and Version, SCREEN extents where they differ from WINDOW
        # ones, and Parent references.
        self.toolkit: tuple[str, str] | None = None
        self.screen: dict[str, tuple[int, int, int, int]] = {}
        self.parents: dict[str, str] = {}
        self.layers: dict[str, int] = {}
        # GetRelationSet's reply by path: (relation type, [(bus, path), ...]) pairs.
        self.relations: dict[str, list[tuple[int, list[tuple[str, str]]]]] = {}
        # The names an element with the Action interface gives its actions, by path.
        self.actions: dict[str, list[str | None]] = {}

    def call(self, destination, object_path, interface, method, signature, body):
        self.calls.append((destination, object_path, interface, method, signature, body))
        if self.failure is not None and object_path == "/button":
            raise self.failure
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetChildren"):
            return (self.refs.get(object_path, []),)
        if (interface, method) == (a11y.DBUS_INTERFACE, "GetConnectionUnixProcessID"):
            return (self.pid if body[0] == "app.bus" else 99,)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetRole"):
            return (self.roles[object_path][0],)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetRoleName"):
            return (self.roles[object_path][1],)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetRelationSet"):
            return (self.relations.get(object_path, []),)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetState"):
            return (self.states[object_path],)
        if (interface, method) == (a11y.ACCESSIBLE_INTERFACE, "GetInterfaces"):
            return (self.interfaces[object_path],)
        if (interface, method) == (a11y.PROPERTIES_INTERFACE, "Get"):
            name = body[1]
            if name == "Name":
                return (("s", self.names[object_path]),)
            if name == "CharacterCount":
                return (("i", len(self.text[object_path])),)
            if name == "NActions":
                return (("i", len(self.actions[object_path])),)
            if name == "NSelectedChildren":
                return (("i", len(self.selected[object_path])),)
            if body[0] == a11y.APPLICATION_INTERFACE and self.toolkit is not None:
                return (("s", self.toolkit[0] if name == "ToolkitName" else self.toolkit[1]),)
            if name == "Parent":
                return (("(so)", ("app.bus", self.parents[object_path])),)
        if (interface, method) == (a11y.SELECTION_INTERFACE, "GetSelectedChild"):
            return (self.selected[object_path][body[0]],)
        if (interface, method) == (a11y.ACTION_INTERFACE, "GetName"):
            name = self.actions[object_path][body[0]]
            if name is None:
                raise _dbus_error(a11y.UNNAMED_ACTION_ERROR)
            if isinstance(name, DBusErrorResponse):
                raise name
            return (name,)
        if (interface, method) == (a11y.COMPONENT_INTERFACE, "GetLayer"):
            return (self.layers.get(object_path, 3),)
        if (interface, method) == (a11y.COMPONENT_INTERFACE, "GetExtents"):
            if body == (a11y.COORDINATE_SCREEN,):
                return (self.screen.get(object_path, self.bounds[object_path]),)
            return (self.bounds[object_path],)
        if (interface, method) == (a11y.TEXT_INTERFACE, "GetText"):
            return (self.text[object_path],)
        raise AssertionError((destination, object_path, interface, method, signature, body))


def test_snapshot_matches_pid_and_chooses_one_toplevel_by_exact_title(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Editor", duplicate_title=True)
    fake.names["/dialog"] = "Other"
    tree = a11y.snapshot(window(), bus=fake)

    assert tree.role == "frame"
    assert tree.children[0].name == "Save"
    assert tree.children[0].text == "Save"
    assert tree.children[0].states == ("enabled", "focusable", "showing", "visible", "checkable")
    assert (tmp_path / "deskpilot" / "elements" / "0xabc.json").exists()
    assert json.loads((tmp_path / "deskpilot" / "elements" / "0xabc.json").read_text())["elements"]["e2"] == {
        "bus": "app.bus",
        "path": "/button",
        "role": "push button",
        "name": "Save",
    }


def test_a_child_a_selection_reports_as_chosen_is_selected(tmp_path, monkeypatch):
    # A GTK combo box reports its active item only through its Selection: the item has no selected state itself.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.interfaces["/frame"].append(a11y.SELECTION_INTERFACE)
    fake.selected["/frame"] = [("app.bus", "/button")]
    tree = a11y.snapshot(window(), bus=fake)
    assert "selected" in tree.children[0].states
    assert "selected" not in tree.states


def test_showing_is_dropped_for_a_box_on_no_surface_of_the_window(tmp_path, monkeypatch):
    # Qt calls the items of a closed combo box showing at x=-10000, and GTK 4 rows it has not laid out, with no size.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    boxes = {
        "/far": (-10000, 0, 80, 20),
        "/flat": (10, 20, 0, 0),
        "/over": (790, 590, 80, 20),
        "/menu": (750, 650, 80, 20),
    }
    for path, bounds in boxes.items():
        add(fake, "/frame", path, path)
        fake.bounds[path] = bounds
    asked = []

    def popups():
        asked.append(1)
        return [a11y.Popup(1, -1, 700, 600, 700, 600, 200, 100)]

    tree = a11y.snapshot(window(), bus=fake, popups=popups)
    showing = {child.name: "showing" in child.states for child in tree.children}
    assert showing == {"Save": True, "/far": False, "/flat": False, "/over": True, "/menu": True}
    assert len(asked) == 1
    first = a11y.snapshot(window(), max_nodes=2, bus=fake, popups=popups)
    later = a11y.more(window(), first.snapshot, bus=fake, popups=popups)
    assert {top.name: "showing" in top.states for top in later.elements} == {key: showing[key] for key in boxes}
    # With no compositor to ask for popups, a box that misses the window is left as the app states it.
    bare = a11y.snapshot(window(), bus=fake)
    far = next(child for child in bare.children if child.name == "/far")
    assert "showing" in far.states
    # An action on such an element is refused the same way, before any input is sent.
    with pytest.raises(a11y.A11yError) as refused:
        a11y.locate(window(), far.id, bus=fake, popups=popups)
    assert refused.value.code == "offscreen"
    assert a11y.locate(window(), tree.children[0].id, bus=fake, popups=popups).width > 0


def test_an_element_says_what_it_can_do(tmp_path, monkeypatch):
    # A GTK 4 switch and a Qt spin box have roles of their own; what they implement says how to use them.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.interfaces["/button"] += [a11y.ACTION_INTERFACE, "org.a11y.atspi.Value"]
    fake.actions["/button"] = ["toggle", "", None]  # None: GTK 4 counts an action it then will not name
    tree = a11y.snapshot(window(), bus=fake)
    button = tree.to_dict()["children"][0]
    assert button["interfaces"] == ["Component", "Text", "Action", "Value"]
    assert button["actions"] == ["toggle"]
    assert (tree.interfaces, tree.actions) == (("Component",), ())


def test_snapshot_reports_ambiguous_toplevel_names(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Same", duplicate_title=True)
    with pytest.raises(a11y.A11yError, match=r"'Same'.*'Same'.*800x600 as the window is: 2 are") as error:
        a11y.snapshot(window(title="Different"), bus=fake)
    assert error.value.code == "ambiguous"


def test_same_titled_toplevels_are_told_apart_by_the_window_size(tmp_path, monkeypatch):
    # Two Text Editor windows on new documents share a title; a GTK 3 window and its open combo's popup share none.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Same", duplicate_title=True)
    fake.bounds["/dialog"] = (0, 0, 400, 300)
    add(fake, "/dialog", "/dialog/ok", "OK")
    big = window(title="Same")
    assert a11y.snapshot(big, bus=fake, size=lambda: (big.width, big.height)).children[0].name == "Save"
    small = Window("0xabd", "editor", "Same", 0, 0, 400, 300, 1, "1", False, 42)
    assert a11y.snapshot(small, bus=fake, size=lambda: (400, 300)).children[0].name == "OK"
    # Resized since it was looked up, as two tiled windows swapping places are: the sibling alone at the recorded
    # size may not be this window.
    for size in (lambda: (640, 480), None):
        with pytest.raises(a11y.A11yError) as error:
            a11y.snapshot(small, bus=fake, size=size)
        assert error.value.code == "ambiguous"


def test_snapshot_takes_a_lone_titled_toplevel_that_reports_no_window_role(tmp_path, monkeypatch):
    # Qt 5 reports a plain QWidget toplevel, such as qt5ct's window, as a "filler".
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Qt5 Configuration Tool")
    fake.roles["/frame"] = (22, "filler")
    tree = a11y.snapshot(window(title="Qt5 Configuration Tool"), bus=fake)
    assert (tree.role, tree.name) == ("filler", "Qt5 Configuration Tool")


def test_snapshot_of_a_qt_message_box_reads_the_box_not_its_parent_window(tmp_path, monkeypatch):
    # Qt reports a QMessageBox toplevel as an "alert message" beside the main window's frame.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="CMake", duplicate_title=True)
    fake.roles["/dialog"] = (2, "alert message")
    fake.names["/dialog"] = "Delete Cache"
    tree = a11y.snapshot(window(title="Delete Cache"), bus=fake)
    assert (tree.role, tree.name) == ("alert message", "Delete Cache")


def test_snapshot_refuses_untitled_toplevels_that_report_no_window_role(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(title="Same", duplicate_title=True)
    fake.roles["/frame"] = fake.roles["/dialog"] = (22, "filler")
    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(title="Same"), bus=fake)
    assert error.value.code == "ambiguous"


def test_snapshot_reports_an_app_without_accessibility(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus(pid=99)
    with pytest.raises(a11y.A11yError, match="no accessibility tree") as error:
        a11y.snapshot(window(), bus=fake)
    assert error.value.code == "no_tree"
    assert "force-renderer-accessibility" in str(error.value)
    assert "ACCESSIBILITY_ENABLED=1" in str(error.value)
    assert "Wine and XWayland games" in str(error.value)
    assert "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1" in str(error.value)


def test_locate_reads_live_bounds_and_unknown_ids_are_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    fake.bounds["/button"] = (30, 40, 90, 25)

    assert a11y.locate(window(), "e2", bus=fake) == a11y.Located(30, 40, 90, 25)
    with pytest.raises(a11y.A11yError, match=r"unknown.*take a new accessibility snapshot") as error:
        a11y.locate(window(), "e99", bus=fake)
    assert error.value.code == "stale"


def test_an_id_from_a_replaced_snapshot_is_stale_and_its_own_snapshot_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    first = a11y.snapshot(window(), bus=fake).snapshot
    monkeypatch.setattr(a11y.time, "time_ns", lambda: 10**18)
    second = a11y.snapshot(window(), bus=fake).snapshot

    assert first != second
    assert a11y.locate(window(), f"{second}:e2", bus=fake) == a11y.Located(10, 20, 80, 20)
    with pytest.raises(a11y.A11yError, match="replaced by snapshot") as error:
        a11y.locate(window(), f"{first}:e2", bus=fake)
    assert error.value.code == "stale"


def test_a_peek_leaves_the_latest_snapshot_and_its_ids_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    taken = a11y.snapshot(window(), bus=fake).snapshot
    peeked = a11y.snapshot(window(), bus=fake, peek=True)

    assert peeked.snapshot == "" and "snapshot" not in peeked.to_dict()
    assert a11y.locate(window(), f"{taken}:e2", bus=fake) == a11y.Located(10, 20, 80, 20)


def test_an_action_name_refused_for_any_other_reason_is_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.interfaces["/button"] += [a11y.ACTION_INTERFACE]
    fake.actions["/button"] = ["toggle", _dbus_error("org.freedesktop.DBus.Error.AccessDenied")]
    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(), bus=fake)
    assert error.value.code == "unavailable"


def test_an_unnamed_element_is_named_by_the_label_it_points_to(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    # A GTK 4 menu item: no name of its own, labelled by a nameless label whose text is the caption.
    fake.refs["/frame"] = [("app.bus", "/item")]
    fake.refs["/item"] = []
    fake.names |= {"/item": "", "/label": ""}
    fake.roles["/item"] = (35, "menu item")
    fake.states["/item"] = fake.states["/frame"]
    fake.bounds["/item"] = (10, 20, 80, 20)
    fake.interfaces["/item"] = ["org.a11y.atspi.Component"]
    fake.interfaces["/label"] = ["org.a11y.atspi.Text"]
    fake.text["/label"] = "Save"
    fake.relations["/item"] = [(3, [("app.bus", "/frame")]), (2, [("app.bus", "/label")])]

    root = a11y.snapshot(window(), bus=fake)

    assert (root.children[0].role, root.children[0].name) == ("menu item", "Save")
    # The same naming holds when the element is found again, so its id is not taken for stale.
    assert a11y.locate(window(), "e2", bus=fake).width == 80


def test_an_element_whose_name_changed_is_stale_and_one_not_showing_is_offscreen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    fake.names["/button"] = "Delete"
    with pytest.raises(a11y.A11yError, match="was push button 'Save' and is now push button 'Delete'") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "stale"

    fake.names["/button"] = "Save"
    fake.states["/button"] = [1 << 8, 0]
    with pytest.raises(a11y.A11yError, match="not showing") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "offscreen"


def test_a_vanished_element_is_stale_and_a_failing_bus_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    gone = SimpleNamespace(
        header=SimpleNamespace(fields={HeaderFields.error_name: "org.freedesktop.DBus.Error.UnknownObject"}), body=()
    )
    fake.failure = DBusErrorResponse(gone)
    with pytest.raises(a11y.A11yError, match="is gone") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "stale"

    fake.failure = TimeoutError("no reply")
    with pytest.raises(a11y.A11yError) as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "unavailable"


def test_a_missing_registry_is_unavailable_not_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    missing = SimpleNamespace(
        header=SimpleNamespace(fields={HeaderFields.error_name: "org.freedesktop.DBus.Error.ServiceUnknown"}), body=()
    )

    class NoRegistry(FakeBus):
        def call(self, destination, object_path, interface, method, signature, body):
            if destination == a11y.A11Y_BUS_NAME:
                raise DBusErrorResponse(missing)
            return super().call(destination, object_path, interface, method, signature, body)

    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(), bus=NoRegistry())
    assert error.value.code == "unavailable"


def test_a_malformed_record_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    a11y.snapshot(window(), bus=FakeBus())
    path = tmp_path / "deskpilot" / "elements" / "0xabc.json"
    payload = json.loads(path.read_text())
    del payload["elements"]["e2"]["path"]
    path.write_text(json.dumps(payload))
    with pytest.raises(a11y.A11yError, match="unknown") as error:
        a11y.locate(window(), "e2", bus=FakeBus())
    assert error.value.code == "stale"


def add(fake, parent, path, name):
    """Hang a button named `name` at `path` below `parent` in a FakeBus tree."""
    fake.refs.setdefault(parent, []).append(("app.bus", path))
    fake.refs[path] = []
    fake.names[path] = name
    fake.roles[path] = (43, "push button")
    fake.states[path] = fake.states["/button"]
    fake.interfaces[path] = ["org.a11y.atspi.Component"]
    fake.bounds[path] = (10, 20, 80, 20)


def wide_bus(rows=250, cells=2):
    """A window of `rows` rows of `cells` cells each below its one button: 2 + rows * (1 + cells) elements."""
    fake = FakeBus()
    for row in range(rows):
        add(fake, "/frame", f"/row{row}", f"row {row}")
        for cell in range(cells):
            add(fake, f"/row{row}", f"/row{row}/cell{cell}", f"cell {row}.{cell}")
    return fake


def flat(element, parent=""):
    """Every element of a tree as (id, name, the id it hangs under), in document order."""
    rows, pending = [], [(element, element.parent or parent)]
    while pending:
        element, above = pending.pop()
        rows.append((element.id, element.name, above))
        pending.extend((child, element.id) for child in reversed(element.children))
    return rows


def read_in_batches(fake, size):
    """A snapshot read `size` elements at a time until nothing is left: its batches as flat rows, and its id."""
    first = a11y.snapshot(window(), max_nodes=size, bus=fake)
    batches, truncated = [flat(first)], first.truncated
    while truncated:
        batch = a11y.more(window(), first.snapshot, max_nodes=size, bus=fake)
        batches.append([row for top in batch.elements for row in flat(top)])
        truncated = batch.truncated
    return batches, first.snapshot


def test_a_snapshot_the_budget_ends_says_how_to_read_on(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    tree = a11y.snapshot(window(), max_nodes=1, bus=FakeBus())

    assert tree.children == ()
    assert tree.to_dict()["truncated"] is True
    assert tree.to_dict()["more"] == tree.snapshot


def test_a_tree_deeper_than_one_read_nests_is_read_on_in_batches(tmp_path, monkeypatch):
    # Nautilus puts the items of its folder menu 31 levels below the window, which one read holds. A read nests no
    # deeper than MAX_DEPTH, so what carries it to the caller can serialise it; the rest is read on, nothing cut.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    parent = "/button"
    for level in range(500):
        add(fake, parent, f"/deep{level}", f"level {level}")
        parent = f"/deep{level}"
    monkeypatch.setattr(sys, "setrecursionlimit", lambda limit: pytest.fail("the read raised the limit"))

    def depth(element):
        levels = 1
        while element.children:
            element, levels = element.children[0], levels + 1
        return levels, element

    tree = a11y.snapshot(window(), bus=fake)
    levels, last = depth(tree)
    assert levels == a11y.MAX_DEPTH and tree.truncated
    names = [last.name]
    read_on = tree.to_dict()["more"]
    while read_on:
        batch = a11y.more(window(), read_on, bus=fake)
        (top,) = batch.elements
        assert top.parent == last.id
        levels, last = depth(top)
        assert levels <= a11y.MAX_DEPTH
        names.append(last.name)
        read_on = batch.to_dict().get("more")
    assert names[-1] == "level 499" and len(names) == 11


def test_a_tree_that_loops_back_on_itself_is_read_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    add(fake, "/button", "/inner", "inner")
    fake.refs["/inner"] = [("app.bus", "/frame"), ("app.bus", "/button")]

    tree = a11y.snapshot(window(), bus=fake)

    assert [row[1] for row in flat(tree)] == ["Editor", "Save", "inner"]
    assert not tree.truncated


def test_batches_of_100_together_are_the_whole_tree_with_the_same_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    whole = flat(a11y.snapshot(window(), bus=wide_bus()))
    assert len(whole) == 752

    batches, _ = read_in_batches(wide_bus(), 100)

    assert [len(batch) for batch in batches] == [100] * 7 + [52]
    # The same elements, under the same ids and the same parents, as one read of the whole tree gives.
    assert [row for batch in batches for row in batch] == whole


def test_a_later_batch_names_the_parent_each_subtree_hangs_under(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    first = a11y.snapshot(window(), max_nodes=4, bus=wide_bus(rows=3))
    # e1 frame, e2 Save, e3 row 0, e4 cell 0.0: row 0's second cell and the other rows are still to read.
    batch = a11y.more(window(), first.snapshot, max_nodes=100, bus=wide_bus(rows=3))

    assert [(top.id, top.name, top.parent) for top in batch.elements] == [
        ("e5", "cell 0.1", "e3"),
        ("e6", "row 1", "e1"),
        ("e9", "row 2", "e1"),
    ]
    assert batch.to_dict()["elements"][1]["children"][0]["id"] == "e7"
    assert "parent" not in batch.to_dict()["elements"][1]["children"][0]
    assert not batch.truncated and "more" not in batch.to_dict()
    assert a11y.more(window(), first.snapshot, bus=wide_bus(rows=3)) == a11y.Batch(first.snapshot, ())


def test_ids_from_every_batch_resolve_while_the_snapshot_is_the_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = wide_bus()
    fake.bounds["/row40/cell1"] = (300, 400, 50, 20)
    batches, snapshot_id = read_in_batches(fake, 100)
    first, later = batches[0][1], next(row for row in batches[1] if row[1] == "cell 40.1")

    assert a11y.locate(window(), f"{snapshot_id}:{later[0]}", bus=fake) == a11y.Located(300, 400, 50, 20)
    assert a11y.locate(window(), f"{snapshot_id}:{first[0]}", bus=fake) == a11y.Located(10, 20, 80, 20)


def test_a_later_batch_keeps_what_a_selection_chose_and_reads_no_element_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = wide_bus(rows=3)
    fake.interfaces["/frame"].append(a11y.SELECTION_INTERFACE)
    fake.selected["/frame"] = [("app.bus", "/row2")]
    first = a11y.snapshot(window(), max_nodes=4, bus=fake)
    fake.calls.clear()

    batch = a11y.more(window(), first.snapshot, bus=fake)

    assert "selected" in batch.elements[2].states
    assert not {call[1] for call in fake.calls} & {"/frame", "/button", "/row0", "/row0/cell0"}


def test_reading_on_from_a_replaced_snapshot_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = wide_bus(rows=3)
    old = a11y.snapshot(window(), max_nodes=4, bus=fake)
    a11y.snapshot(window(), max_nodes=4, bus=fake)

    with pytest.raises(a11y.A11yError, match="was replaced") as error:
        a11y.more(window(), old.snapshot, bus=fake)
    assert error.value.code == "stale"
    with pytest.raises(a11y.A11yError) as error:
        a11y.more(replace(window(), pid=7), old.snapshot, bus=fake)
    assert error.value.code == "stale"


def test_reading_on_when_an_element_kept_for_later_is_gone_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    first = a11y.snapshot(window(), max_nodes=4, bus=wide_bus(rows=3))
    unknown = SimpleNamespace(
        header=SimpleNamespace(fields={HeaderFields.error_name: "org.freedesktop.DBus.Error.UnknownObject"}), body=()
    )

    class RowGone(FakeBus):
        def call(self, destination, object_path, interface, method, signature, body):
            if object_path == "/row1":
                raise DBusErrorResponse(unknown)
            return super().call(destination, object_path, interface, method, signature, body)

    changed = RowGone()
    changed.__dict__.update(wide_bus(rows=3).__dict__)
    with pytest.raises(a11y.A11yError, match="changed since snapshot") as error:
        a11y.more(window(), first.snapshot, bus=changed)
    assert error.value.code == "stale"
    # Nothing was half read: the snapshot still reads on once the tree is as it was.
    assert len(a11y.more(window(), first.snapshot, bus=wide_bus(rows=3)).elements) == 3


def test_a_peek_the_budget_ends_keeps_nothing_to_read_on(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    peek = a11y.snapshot(window(), max_nodes=1, bus=FakeBus(), peek=True).to_dict()

    assert peek["truncated"] is True and "more" not in peek and "snapshot" not in peek
    with pytest.raises(ValueError, match="at least one"):
        a11y.snapshot(window(), max_nodes=0, bus=FakeBus())


def test_narrow_keeps_what_fits_and_the_elements_above_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = wide_bus(rows=3)
    fake.states["/row1/cell1"] = [1 << 8, 0]  # enabled, not showing
    tree = a11y.snapshot(window(), bus=fake).to_dict()

    found = a11y.narrow(tree, query="CELL 1.")

    assert (found["read"], found["matched"], found["snapshot"]) == (11, 2, tree["snapshot"])
    assert [(row["id"], [cell["id"] for cell in row["children"]]) for row in found["children"]] == [
        ("e6", ["e7", "e8"])
    ]
    shown = a11y.narrow(tree, query="cell 1.", showing=True)
    assert [cell["id"] for cell in shown["children"][0]["children"]] == ["e7"]
    assert a11y.narrow(tree, query="nothing like it")["children"] == []
    assert a11y.narrow(tree) is tree


def gtk3_bus(toolkit=("gtk", "3.24.52")):
    """A GTK 3 window with a button on the toplevel, an open menu whose item has an open submenu. GTK 3 reports each
    popup element relative to its popup's surface, and its SCREEN extents shifted by the popup's position: the menu's
    geometry is at 20,188 and the submenu's GDK puts at 162,124."""
    fake = FakeBus()
    fake.toolkit = toolkit
    fake.refs.update({"/frame": [("app.bus", "/button"), ("app.bus", "/menu")], "/menu": [("app.bus", "/item")]})
    fake.refs.update({"/item": [("app.bus", "/submenu")], "/submenu": [("app.bus", "/subitem")], "/subitem": []})
    fake.bounds.update({"/menu": (0, 0, 100, 200), "/item": (6, 9, 80, 20)})
    fake.bounds.update({"/submenu": (0, 0, 100, 100), "/subitem": (6, 30, 80, 20)})
    fake.screen.update({"/menu": (20, 188, 100, 200), "/item": (26, 197, 80, 20)})
    fake.screen.update({"/submenu": (162, 124, 100, 100), "/subitem": (168, 154, 80, 20)})
    fake.parents.update({"/subitem": "/submenu", "/submenu": "/item", "/item": "/menu", "/menu": "/frame"})
    fake.parents["/button"] = "/frame"
    for path, name, role in (
        ("/menu", "", "menu"),
        ("/item", "Germany", "menu item"),
        ("/submenu", "", "menu"),
        ("/subitem", "Sub three", "menu item"),
    ):
        fake.names[path], fake.roles[path] = name, (0, role)
        fake.states[path] = fake.states["/frame"]
        fake.interfaces[path] = [a11y.COMPONENT_INTERFACE]
    return fake


POPUPS = [
    a11y.Popup(0, -1, 20, 188, 14, 183, 100, 200),
    a11y.Popup(1, 0, 162, 128, 156, 123, 100, 100),
]


def test_a_gtk3_popup_element_is_placed_through_its_popup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    asked = []
    tree = a11y.snapshot(window(), bus=gtk3_bus(), popups=lambda: asked.append(1) or POPUPS)
    button, menu = tree.children
    item = menu.children[0]
    subitem = item.children[0].children[0]
    assert (button.x, button.y) == (10, 20)
    assert (item.x, item.y, item.width, item.height) == (14 + 6, 183 + 9, 80, 20)
    assert (subitem.x, subitem.y) == (156 + 6, 123 + 30)
    assert asked == [1]

    fake = gtk3_bus()
    assert a11y.locate(window(), "e2", bus=fake) == a11y.Located(10, 20, 80, 20, known=True)
    assert a11y.locate(window(), "e4", bus=fake) == a11y.Located(6, 9, 80, 20, ((20, 188),), True)
    assert a11y.locate(window(), "e6", bus=fake) == a11y.Located(6, 30, 80, 20, ((20, 188), (162, 124)), True)


def test_a_popup_element_whose_popup_is_not_found_has_zero_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    tree = a11y.snapshot(window(), bus=gtk3_bus(), popups=lambda: POPUPS[1:])
    item = tree.children[1].children[0]
    assert (item.x, item.y, item.width, item.height) == (0, 0, 0, 0)
    assert (tree.children[0].x, tree.children[0].y) == (10, 20)


def test_a_popup_at_the_window_origin_is_not_taken_for_the_window(tmp_path, monkeypatch):
    """A menu at the window's origin reports the toplevel's own offset, so its elements and the window's cannot be
    told apart: both get zero bounds, never the window's surface."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = gtk3_bus()
    fake.screen.update({"/menu": (0, 0, 100, 200), "/item": (6, 9, 80, 20)})
    tree = a11y.snapshot(window(), bus=fake, popups=lambda: [a11y.Popup(0, -1, 0, 0, -6, -5, 100, 200)])
    button, menu = tree.children
    assert (button.x, button.y, button.width) == (0, 0, 0)
    assert (menu.children[0].x, menu.children[0].width) == (0, 0)
    assert a11y.locate(window(), "e4", bus=fake) == a11y.Located(6, 9, 80, 20, known=True)


def test_gtk4_and_other_toolkits_keep_their_reported_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = gtk3_bus(toolkit=("GTK", "4.24.1"))
    tree = a11y.snapshot(window(), bus=fake, popups=lambda: pytest.fail("GTK 4 needs no popups"))
    assert (tree.children[1].children[0].x, tree.children[1].children[0].y) == (6, 9)
    assert a11y.locate(window(), "e4", bus=fake) == a11y.Located(6, 9, 80, 20)
    assert not any(call[-1] == (a11y.COORDINATE_SCREEN,) for call in fake.calls)


def test_a_popup_is_resolved_exactly_or_refused():
    assert a11y.resolve(POPUPS, ()) is None
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve([*POPUPS, a11y.Popup(2, -1, 0, 0, -6, -5, 10, 10)], ())
    assert error.value.code == "ambiguous"
    assert a11y.resolve(POPUPS, ((20, 188),)) == POPUPS[0]
    assert a11y.resolve(POPUPS, ((20, 188), (162, 124))) == POPUPS[1]
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve(POPUPS, ((21, 188),))
    assert error.value.code == "stale"
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve([*POPUPS, a11y.Popup(2, -1, 20, 188, 14, 183, 10, 10)], ((20, 188),))
    assert error.value.code == "ambiguous"
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve([*POPUPS, a11y.Popup(2, 0, 0, 0, 0, 0, 10, 10)], ((20, 188), (162, 124)))
    assert error.value.code == "ambiguous"
    with pytest.raises(a11y.A11yError) as error:
        a11y.resolve(POPUPS[:1], ((20, 188), (162, 124)))
    assert error.value.code == "stale"


def test_popup_layer_distinguishes_a_menu_at_the_window_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = gtk3_bus()
    fake.screen.update({"/menu": (0, 0, 100, 200), "/item": (6, 9, 80, 20)})
    fake.layers.update({"/menu": 5, "/item": 5})
    popup = a11y.Popup(0, -1, 0, 0, -6, -5, 100, 200)
    tree = a11y.snapshot(window(), bus=fake, popups=lambda: [popup])
    item = tree.children[1].children[0]
    assert (item.x, item.y, item.width, item.height) == (0, 4, 80, 20)
    located = a11y.locate(window(), item.id, bus=fake)
    assert located == a11y.Located(6, 9, 80, 20, ((0, 0),), True)
    assert a11y.resolve([popup], located.keys) == popup


def test_a_new_window_at_a_reused_address_has_no_prior_elements(tmp_path, monkeypatch):
    # Hyprland hands a closed window's address to a later window: the closed one's element ids name nothing there.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    for reused in (window(pid=43), replace(window(), stable_id="8")):
        with pytest.raises(a11y.A11yError, match="unknown for window 0xabc") as error:
            a11y.locate(reused, "e2", bus=fake)
        assert error.value.code == "stale"
    assert a11y.locate(window(), "e2", bus=fake) == a11y.Located(10, 20, 80, 20)


def test_an_element_with_a_negative_size_has_no_bounds(tmp_path, monkeypatch):
    # GTK 4 leaves GetExtents' width and height unset for a stack page whose widget is not realized.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.bounds["/button"] = (0, 0, -554081264, 32650)
    tree = a11y.snapshot(window(), bus=fake)
    button = tree.children[0]
    assert (button.x, button.y, button.width, button.height) == (0, 0, 0, 0)
    with pytest.raises(a11y.A11yError) as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "offscreen"


def test_text_leaves_out_the_marks_where_child_elements_sit(tmp_path, monkeypatch):
    # Chromium gives a paragraph holding a link the text "See \ufffc for more", and a document only the marks.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.text["/button"] = "See \ufffc for more"
    assert a11y.snapshot(window(), bus=fake).children[0].text == "See  for more"
    fake.text["/button"] = "\ufffc\ufffc"
    assert a11y.snapshot(window(), bus=fake).children[0].text == ""


def test_a_gtk3_cell_scrolled_out_of_view_has_no_bounds(tmp_path, monkeypatch):
    # yad --list with 200 rows gave row 173 the extents (-2147483648, -2147483648, 1184, 23), still "showing".
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.bounds["/button"] = (-(2**31), -(2**31), 1184, 23)
    button = a11y.snapshot(window(), bus=fake).children[0]
    assert (button.x, button.y, button.width, button.height) == (0, 0, 0, 0)
    with pytest.raises(a11y.A11yError, match="scroll it into view") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "offscreen"


def _dbus_error(name):
    return DBusErrorResponse(SimpleNamespace(header=SimpleNamespace(fields={HeaderFields.error_name: name}), body=()))


def test_an_element_gdbus_no_longer_serves_is_stale(tmp_path, monkeypatch):
    # GDBus answers a call on an unregistered accessible's path with UnknownMethod, "No such interface
    # org.a11y.atspi.Accessible", as GNOME Text Editor's Save button did once its click closed the dialog (#128).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    a11y.snapshot(window(), bus=fake)
    fake.failure = _dbus_error("org.freedesktop.DBus.Error.UnknownMethod")
    with pytest.raises(a11y.A11yError, match="is gone") as error:
        a11y.locate(window(), "e2", bus=fake)
    assert error.value.code == "stale"


def test_an_element_that_vanishes_while_the_tree_is_read_is_left_out(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.failure = _dbus_error("org.freedesktop.DBus.Error.UnknownMethod")
    tree = a11y.snapshot(window(), bus=fake)
    assert tree.children == ()
    records = json.loads((tmp_path / "deskpilot" / "elements" / "0xabc.json").read_text())["elements"]
    assert list(records) == ["e1"]


def test_a_toplevel_that_vanishes_while_the_tree_is_read_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    class Closing(FakeBus):
        def call(self, destination, object_path, interface, method, signature, body):
            if object_path == "/frame" and method == "GetState":
                raise _dbus_error("org.freedesktop.DBus.Error.UnknownMethod")
            return super().call(destination, object_path, interface, method, signature, body)

    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(), bus=Closing())
    assert error.value.code == "gone"


def test_an_app_that_leaves_the_bus_while_its_tree_is_read_is_gone(tmp_path, monkeypatch):
    # A child read failing because the app's bus name went is the whole tree gone, not one element left out.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake = FakeBus()
    fake.failure = _dbus_error("org.freedesktop.DBus.Error.NameHasNoOwner")
    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(), bus=fake)
    assert error.value.code == "gone"


def test_a_toplevel_that_closes_while_toplevels_are_read_leaves_no_match_proven(tmp_path, monkeypatch):
    # Two windows of one process share a title and the requested one goes after GetChildren: the one left has the
    # title, but may be the sibling, so its tree must never be saved under the requested window.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    class Closing(FakeBus):
        def __init__(self, title):
            super().__init__(duplicate_title=True)
            self.names["/dialog"] = title

        def call(self, destination, object_path, interface, method, signature, body):
            if object_path == "/frame":
                raise _dbus_error("org.freedesktop.DBus.Error.UnknownMethod")
            return super().call(destination, object_path, interface, method, signature, body)

    for title in ("Preferences", "Editor"):
        with pytest.raises(a11y.A11yError) as error:
            a11y.snapshot(window(), bus=Closing(title))
        assert error.value.code == "gone"
    assert not (tmp_path / "deskpilot" / "elements" / "0xabc.json").exists()


@pytest.mark.parametrize(
    "failing",
    [("/frame", "GetInterfaces"), ("/frame", "GetExtents")],
)
def test_a_gtk3_toplevel_that_closes_while_its_offset_is_read_is_gone(tmp_path, monkeypatch, failing):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    class Closing(FakeBus):
        def call(self, destination, object_path, interface, method, signature, body):
            if (object_path, method) == failing:
                raise _dbus_error("org.freedesktop.DBus.Error.UnknownMethod")
            return super().call(destination, object_path, interface, method, signature, body)

    fake = Closing()
    fake.toolkit = ("gtk", "3.24.52")
    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(), bus=fake)
    assert error.value.code == "gone"


@pytest.mark.parametrize(
    "failing",
    [("/app", "GetChildren"), ("/frame", "GetRoleName"), ("/frame", "Get")],
)
def test_an_app_that_leaves_the_bus_while_its_toplevel_is_chosen_is_gone(tmp_path, monkeypatch, failing):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    class Leaving(FakeBus):
        def call(self, destination, object_path, interface, method, signature, body):
            if (object_path, method) == failing:
                raise _dbus_error("org.freedesktop.DBus.Error.NameHasNoOwner")
            return super().call(destination, object_path, interface, method, signature, body)

    with pytest.raises(a11y.A11yError) as error:
        a11y.snapshot(window(), bus=Leaving())
    assert error.value.code == "gone"


class PagedDesk:
    """A Desk whose window's tree is larger than one read: the first page, then with `more` the rest."""

    def __init__(self):
        self.asked = []

    def elements(self, query, show=False, hidden=False, peek=False, max_nodes=a11y.MAX_NODES, more=""):
        self.asked.append((query, hidden, peek, max_nodes, more))
        root = a11y.Element("e1", "frame", "Editor", (), None, 0, 0, 800, 600, ())
        if peek:
            return replace(root, truncated=True)
        if not more:
            return replace(root, truncated=True, snapshot="abc-1")
        cell = a11y.Element("e3", "push button", "Save", ("showing",), None, 1, 2, 3, 4, ())
        row = a11y.Element("e2", "panel", "", (), None, 0, 0, 5, 5, (cell,), parent="e1")
        return a11y.Batch("abc-1", (row, replace(cell, id="e4", name="Open", parent="e1")))


def test_elements_text_names_the_command_that_reads_the_rest_of_a_larger_tree():
    from deskpilot import cli

    desk = PagedDesk()
    args = cli.build_parser().parse_args(["elements", "My Editor", "--max-nodes", "100", "--hidden"])
    lines = cli.run(args, desk)  # ty: ignore[invalid-argument-type]

    assert isinstance(lines, list)
    assert lines[0] == "snapshot abc-1"
    assert lines[-1] == (
        "more: the tree is larger than this batch; read on with: "
        "deskpilot elements 'My Editor' --more abc-1 --max-nodes 100 --hidden"
    )
    assert desk.asked == [("My Editor", True, False, 100, "")]


def test_elements_more_prints_each_subtree_under_the_id_it_hangs_from():
    from deskpilot import cli

    desk = PagedDesk()
    args = cli.build_parser().parse_args(["elements", "Editor", "--more", "abc-1"])
    lines = cli.run(args, desk)  # ty: ignore[invalid-argument-type]

    assert lines == [
        "snapshot abc-1",
        "under e1:",
        "  e2 panel 0,0 5x5",
        "    e3 push button 'Save' 1,2 3x4 [showing]",
        "  e4 push button 'Open' 1,2 3x4 [showing]",
    ]
    assert desk.asked == [("Editor", False, False, a11y.MAX_NODES, "abc-1")]
    args = cli.build_parser().parse_args(["--json", "elements", "Editor", "--more", "abc-1", "--query", "open"])
    found = cli.run(args, desk)  # ty: ignore[invalid-argument-type]
    assert isinstance(found, dict)
    assert ([top["id"] for top in found["elements"]], found["read"], found["matched"]) == (["e4"], 3, 1)


def test_elements_text_of_a_peek_the_budget_ended_says_it_cannot_be_read_on():
    from deskpilot import cli

    args = cli.build_parser().parse_args(["elements", "Editor", "--peek"])
    lines = cli.run(args, PagedDesk())  # ty: ignore[invalid-argument-type]

    assert isinstance(lines, list)
    assert lines[-1].startswith("truncated: the tree is larger than was read") and "--max-nodes" in lines[-1]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["elements", "Editor", "--max-nodes", "0"])
