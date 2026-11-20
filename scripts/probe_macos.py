"""Temporary CI probe: which macOS mouse event routes reach a background app on a hosted runner."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import AppKit  # ty: ignore[import-not-found]
import ApplicationServices as ax  # ty: ignore[import-not-found]
import Quartz as q  # ty: ignore[import-not-found]

work = Path(tempfile.mkdtemp(prefix="probe-"))
finder = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.finder")[0]


def front() -> str:
    found = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return found.localizedName() if found else "none"


def to_back() -> None:
    finder.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(1.5)


def window_of(pid: int) -> int:
    for entry in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or []:
        if entry.get(q.kCGWindowOwnerPID) == pid and entry.get(q.kCGWindowLayer) == 0:
            return int(entry[q.kCGWindowNumber])
    return 0


serial = [100]


def click(pid: int, x: float, y: float, *, fields: bool, source=None, number: bool = False, hid: bool = False,
          move: bool = False, window: int = 0) -> None:  # fmt: skip
    serial[0] += 1
    kinds = [q.kCGEventMouseMoved] if move else []
    for kind in [*kinds, q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp]:
        made = q.CGEventCreateMouseEvent(source, kind, (x, y), q.kCGMouseButtonLeft)
        if kind != q.kCGEventMouseMoved:
            q.CGEventSetIntegerValueField(made, 1, 1)
        if number:
            q.CGEventSetIntegerValueField(made, 0, serial[0])
        if fields:
            q.CGEventSetIntegerValueField(made, 91, window)
            q.CGEventSetIntegerValueField(made, 92, window)
        if hid:
            q.CGEventPost(q.kCGHIDEventTap, made)
        else:
            q.CGEventPostToPid(pid, made)
        time.sleep(0.05)
    time.sleep(1)


def tk_probe() -> None:
    state = work / "state.json"
    app = subprocess.Popen([sys.executable, "scripts/smoke_app.py", str(state)])
    time.sleep(5)
    pad = json.loads(state.read_text())["widgets"]["pad"]
    x, y = pad[0] + pad[2] / 2, pad[1] + pad[3] / 2
    window = window_of(app.pid)
    print("tk window", window, "pad centre", x, y, flush=True)
    hid_source = q.CGEventSourceCreate(q.kCGEventSourceStateHIDSystemState)
    private_source = q.CGEventSourceCreate(q.kCGEventSourceStatePrivate)
    variants = {
        "bg pid fields": {"fields": True},
        "bg pid no fields": {"fields": False},
        "bg pid fields + move": {"fields": True, "move": True},
        "bg pid fields + hid source": {"fields": True, "source": hid_source},
        "bg pid fields + private source": {"fields": True, "source": private_source},
        "bg pid fields + event number": {"fields": True, "number": True},
        "bg pid fields + number + hid source": {"fields": True, "number": True, "source": hid_source},
    }
    for name, options in variants.items():
        to_back()
        before = len(json.loads(state.read_text())["clicks"])
        click(app.pid, x, y, window=window, **options)
        after = len(json.loads(state.read_text())["clicks"])
        print(f"{name}: front {front()} clicks +{after - before}", flush=True)
    to_back()
    bounds = next(dict(e[q.kCGWindowBounds]) for e in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll, 0)
                  if int(e[q.kCGWindowNumber]) == window)  # fmt: skip
    point = [str(int(x - bounds["X"])), str(int(y - bounds["Y"]))]
    result = subprocess.run(["deskpilot", "click", hex(window), *point, "--scale", "1"],
                            capture_output=True, text=True, check=False)  # fmt: skip
    seen = json.loads(state.read_text())["clicks"]
    print("deskpilot click (bg):", result.returncode, result.stderr.strip()[-300:], seen, flush=True)
    running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
    running.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(1.5)
    for name, options in {"front pid fields": {"fields": True}, "front hid": {"fields": False, "hid": True}}.items():
        before = len(json.loads(state.read_text())["clicks"])
        click(app.pid, x, y, window=window, **options)
        after = len(json.loads(state.read_text())["clicks"])
        print(f"{name}: front {front()} clicks +{after - before}", flush=True)
    os.kill(app.pid, signal.SIGKILL)


def value(pid: int) -> str:
    app = ax.AXUIElementCreateApplication(pid)
    _, focused = ax.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
    if focused is None:
        return "<no focused element>"
    _, text = ax.AXUIElementCopyAttributeValue(focused, "AXValue", None)
    return repr(text)


def key(pid: int, char: str) -> None:
    for down in (True, False):
        made = q.CGEventCreateKeyboardEvent(None, 0, down)
        q.CGEventKeyboardSetUnicodeString(made, 1, char)
        q.CGEventPostToPid(pid, made)
        time.sleep(0.05)
    time.sleep(1)


def textedit_probe() -> None:
    subprocess.run(["open", "-g", "-n", "-a", "TextEdit"], check=True)
    time.sleep(5)
    app = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.TextEdit")[-1]
    pid = app.processIdentifier()
    to_back()
    key(pid, "a")
    print("TextEdit bg key 'a':", value(pid), flush=True)
    window = window_of(pid)
    bounds = next(dict(e[q.kCGWindowBounds]) for e in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll, 0)
                  if int(e[q.kCGWindowNumber]) == window)  # fmt: skip
    x, y = bounds["X"] + bounds["Width"] / 2, bounds["Y"] + bounds["Height"] / 2
    click(pid, x, y, fields=True, window=window)
    print("TextEdit bg click: front", front(), flush=True)
    key(pid, "b")
    print("TextEdit bg key 'b' after click:", value(pid), flush=True)
    os.kill(pid, signal.SIGKILL)


tk_probe()
textedit_probe()
