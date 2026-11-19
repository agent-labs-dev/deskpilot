"""The macOS driver: Quartz events posted to the target app's process, so the user's pointer and focus stay theirs.

Coordinates are global points with the origin at the top left of the main display, as Quartz reports windows. A shot is
in image pixels, so on a Retina display its scale is 2 and points resolve back through it like any other shot.
Accessibility (for events and the AX tree) and Screen Recording (for window titles and shots) are permissions the user
grants the app running deskpilot; without them actions fail as `unavailable`, naming the System Settings pane.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .errors import Unavailable
from .hypr import Window
from .native import Node, descendants

INSTANCE = "macos"

# kVK_ codes from HIToolbox/Events.h for keys.NAMED and the modifiers.
KEYCODES = {
    "esc": 53, "escape": 53, "backspace": 51, "tab": 48, "return": 36, "enter": 36, "space": 49,
    "ctrl": 59, "control": 59, "shift": 56, "alt": 58, "super": 55, "meta": 55, "win": 55, "cmd": 55, "command": 55,
    "capslock": 57, "delete": 117, "del": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "up": 126, "down": 125, "left": 123, "right": 124, "insert": 114,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
}  # fmt: skip
# kCGEventFlagMask* for each modifier.
FLAGS = {
    "shift": 1 << 17, "ctrl": 1 << 18, "control": 1 << 18, "alt": 1 << 19,
    "super": 1 << 20, "meta": 1 << 20, "win": 1 << 20, "cmd": 1 << 20, "command": 1 << 20,
}  # fmt: skip
# Mouse event fields from CGEventTypes.h: the window under the pointer, so an app that is not frontmost routes the
# event to the right window.
WINDOW_UNDER_POINTER = 91
WINDOW_THAT_CAN_HANDLE = 92
CLICK_STATE = 1
SETTINGS = "System Settings > Privacy & Security"


def modifier_flags(held: frozenset[str]) -> int:
    flags = 0
    for key in held:
        flags |= FLAGS.get(key, 0)
    return flags


class MacDriver:
    instance = INSTANCE

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise Unavailable("the macOS driver runs only on macOS")
        import AppKit
        import ApplicationServices
        import Quartz

        self.q: Any = Quartz
        self.ax: Any = ApplicationServices
        self.appkit: Any = AppKit

    def _trusted(self) -> None:
        if not self.ax.AXIsProcessTrusted():
            raise Unavailable(
                f"macOS has not given this app Accessibility access: allow it in {SETTINGS} > Accessibility, then run "
                "deskpilot again"
            )

    # Windows

    def windows(self) -> list[Window]:
        """Normal app windows, front to back, as Quartz lists them; a minimized window is listed as not visible."""
        q = self.q
        info = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll | q.kCGWindowListExcludeDesktopElements,
                                            q.kCGNullWindowID) or []  # fmt: skip
        front = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        front_pid = front.processIdentifier() if front else -1
        found: list[Window] = []
        focused_done = False
        for entry in info:
            if entry.get(q.kCGWindowLayer, 1) != 0 or not entry.get(q.kCGWindowAlpha, 1):
                continue
            bounds = entry.get(q.kCGWindowBounds) or {}
            width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
            if width < 2 or height < 2:
                continue
            onscreen = bool(entry.get(q.kCGWindowIsOnscreen, False))
            title = entry.get(q.kCGWindowName) or ""
            if not onscreen and not title:
                continue
            pid = int(entry.get(q.kCGWindowOwnerPID, 0))
            number = int(entry[q.kCGWindowNumber])
            focused = onscreen and pid == front_pid and not focused_done
            focused_done = focused_done or focused
            found.append(
                Window(
                    address=hex(number),
                    cls=entry.get(q.kCGWindowOwnerName) or "",
                    title=title,
                    x=int(bounds.get("X", 0)),
                    y=int(bounds.get("Y", 0)),
                    width=width,
                    height=height,
                    monitor=0,
                    workspace="",
                    xwayland=False,
                    pid=pid,
                    focused=focused,
                    instance=INSTANCE,
                    stable_id=str(number),
                    visible=onscreen,
                )
            )
        return found

    def locked(self) -> bool:
        session = self.q.CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("CGSSessionScreenIsLocked", False))

    def focus(self, window: Window) -> None:
        app = self.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(window.pid)
        if app is None:
            raise RuntimeError(f"{window.cls} ({window.address}) has quit")
        app.activateWithOptions_(self.appkit.NSApplicationActivateIgnoringOtherApps)
        ax = self._ax_window(window)
        if ax is not None:
            self.ax.AXUIElementSetAttributeValue(ax, "AXMinimized", False)
            self.ax.AXUIElementPerformAction(ax, "AXRaise")

    def cursor(self) -> tuple[int, int]:
        point = self.q.CGEventGetLocation(self.q.CGEventCreate(None))
        return round(point.x), round(point.y)

    def top_at(self, x: int, y: int) -> str | None:
        q = self.q
        for entry in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or []:
            bounds = entry.get(q.kCGWindowBounds) or {}
            left, top = bounds.get("X", 0), bounds.get("Y", 0)
            if not entry.get(q.kCGWindowAlpha, 1) or entry.get(q.kCGWindowLayer, 0) >= 1000:
                continue
            if left <= x < left + bounds.get("Width", 0) and top <= y < top + bounds.get("Height", 0):
                return hex(int(entry[q.kCGWindowNumber]))
        return None

    # Capture

    def grab(self, window: Window, path: Path) -> None:
        number = str(int(window.address, 16))
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-o", f"-l{number}", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            raise Unavailable(
                f"macOS gave no image of {window.cls} ({window.address}): allow Screen Recording for this app in "
                f"{SETTINGS} > Screen & System Audio Recording. {result.stderr.strip()}"
            )

    # Input

    def _post(self, window: Window, event: Any, seat: bool) -> None:
        if seat:
            self.q.CGEventPost(self.q.kCGHIDEventTap, event)
        else:
            self.q.CGEventPostToPid(window.pid, event)

    def mouse(
        self,
        window: Window,
        event: str,
        x: int,
        y: int,
        *,
        button: str = "left",
        held: frozenset[str] = frozenset(),
        clicks: int = 1,
        dy: int = 0,
        seat: bool = False,
    ) -> None:
        self._trusted()
        q = self.q
        number = int(window.address, 16)
        if event == "wheel":
            made = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, dy)
            q.CGEventSetLocation(made, (x, y))
        else:
            kinds = {
                "left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGEventLeftMouseDragged,
                         q.kCGMouseButtonLeft),
                "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, q.kCGEventRightMouseDragged,
                          q.kCGMouseButtonRight),
                "middle": (q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, q.kCGEventOtherMouseDragged,
                           q.kCGMouseButtonCenter),
            }  # fmt: skip
            pressed = next(iter(held), None)
            if event == "move":
                kind = kinds[pressed][2] if pressed else q.kCGEventMouseMoved
                code = kinds[pressed][3] if pressed else q.kCGMouseButtonLeft
            else:
                down, up, _, code = kinds[button]
                kind = down if event == "down" else up
            made = q.CGEventCreateMouseEvent(None, kind, (x, y), code)
            if event in ("down", "up"):
                q.CGEventSetIntegerValueField(made, CLICK_STATE, clicks)
        q.CGEventSetIntegerValueField(made, WINDOW_UNDER_POINTER, number)
        q.CGEventSetIntegerValueField(made, WINDOW_THAT_CAN_HANDLE, number)
        self._post(window, made, seat)

    def key(self, window: Window, key: str, down: bool, *, held: frozenset[str], seat: bool = False) -> None:
        self._trusted()
        q = self.q
        code = KEYCODES.get(key)
        if code is None:
            made = q.CGEventCreateKeyboardEvent(None, 0, down)
            q.CGEventKeyboardSetUnicodeString(made, len(key.encode("utf-16-le")) // 2, key)
        else:
            made = q.CGEventCreateKeyboardEvent(None, code, down)
        q.CGEventSetFlags(made, modifier_flags(held | ({key} if down and key in FLAGS else set())))
        self._post(window, made, seat)

    def text(self, window: Window, char: str, *, seat: bool = False) -> None:
        self._trusted()
        q = self.q
        if char in ("\n", "\r"):
            for down in (True, False):
                self._post(window, q.CGEventCreateKeyboardEvent(None, KEYCODES["return"], down), seat)
            return
        units = len(char.encode("utf-16-le")) // 2
        for down in (True, False):
            made = q.CGEventCreateKeyboardEvent(None, 0, down)
            q.CGEventKeyboardSetUnicodeString(made, units, char)
            q.CGEventSetFlags(made, 0)
            self._post(window, made, seat)

    # Accessibility

    def _attr(self, element: Any, name: str) -> Any:
        error, value = self.ax.AXUIElementCopyAttributeValue(element, name, None)
        return value if error == 0 else None

    def _ax_window(self, window: Window) -> Any:
        """The app's AX window with this window's frame: AX has no window number, so the frame is the join."""
        app = self.ax.AXUIElementCreateApplication(window.pid)
        for candidate in self._attr(app, "AXWindows") or []:
            node = self.describe(candidate)
            if (node.x, node.y, node.width, node.height) == (window.x, window.y, window.width, window.height):
                return candidate
        return None

    def root(self, window: Window) -> object:
        self._trusted()
        found = self._ax_window(window)
        if found is None:
            raise LookupError(f"{window.cls} ({window.address}) exposes no accessibility window with its frame")
        return found

    def children(self, node: object) -> list[object]:
        return list(self._attr(node, "AXChildren") or [])

    def describe(self, node: Any) -> Node:
        ax = self.ax
        position = self._attr(node, "AXPosition")
        size = self._attr(node, "AXSize")
        x = y = width = height = 0
        if position is not None:
            _, point = ax.AXValueGetValue(position, ax.kAXValueCGPointType, None)
            x, y = round(point.x), round(point.y)
        if size is not None:
            _, extent = ax.AXValueGetValue(size, ax.kAXValueCGSizeType, None)
            width, height = round(extent.width), round(extent.height)
        states = []
        if self._attr(node, "AXEnabled") is False:
            states.append("disabled")
        if self._attr(node, "AXFocused"):
            states.append("focused")
        value = self._attr(node, "AXValue")
        role = self._attr(node, "AXRoleDescription") or self._attr(node, "AXRole") or ""
        name = self._attr(node, "AXTitle") or self._attr(node, "AXDescription") or ""
        return Node(str(role), str(name), tuple(states), value if isinstance(value, str) else None, x, y, width, height)

    # Clipboard

    def read_clipboard(self) -> str:
        return subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True, check=True).stdout

    def write_clipboard(self, text: str) -> None:
        subprocess.run(["/usr/bin/pbcopy"], input=text, text=True, check=True)

    # Processes

    def launch(self, command: list[str]) -> int:
        """Start the app in the background, so the user's focus stays where it is: an app bundle or an app name that
        is not a command on PATH opens with `open -g`, anything else runs as a process."""
        if not (command[0].endswith(".app") or shutil.which(command[0]) is None):
            return subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).pid
        workspace = self.appkit.NSWorkspace.sharedWorkspace()
        before = {app.processIdentifier() for app in workspace.runningApplications()}
        args = ["--args", *command[1:]] if command[1:] else []
        subprocess.run(["/usr/bin/open", "-g", "-a", command[0], *args], check=True, capture_output=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for app in workspace.runningApplications():
                if app.processIdentifier() not in before:
                    return int(app.processIdentifier())
            time.sleep(0.1)
        raise RuntimeError(f"{command[0]} was already running or did not start; deskpilot waits for a new process")

    def family(self, pid: int) -> set[int]:
        out = subprocess.run(["/bin/ps", "-Ao", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
        parents = {int(a): int(b) for a, b in (line.split() for line in out.splitlines() if line.strip())}
        return descendants(pid, parents)
