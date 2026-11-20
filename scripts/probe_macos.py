"""Temporary CI probe: why input never reaches the Tk smoke app on a macOS runner, and typed text never shows in
TextEdit's tree."""

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
    return f"{found.localizedName()} ({found.processIdentifier()})" if found else "none"


def attr(element, name: str):
    error, value = ax.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else f"<error {error}>"


def hit(x: float, y: float) -> str:
    error, element = ax.AXUIElementCopyElementAtPosition(ax.AXUIElementCreateSystemWide(), x, y, None)
    if error != 0 or element is None:
        return f"<hit error {error}>"
    _, pid = ax.AXUIElementGetPid(element, None)
    return f"pid {pid} role {attr(element, 'AXRole')}"


def hid_click(x: float, y: float) -> None:
    for kind in (q.kCGEventMouseMoved, q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp):
        q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateMouseEvent(None, kind, (x, y), q.kCGMouseButtonLeft))
        time.sleep(0.1)
    time.sleep(1)


def tk_probe() -> None:
    import tkinter

    print("tk", tkinter.TkVersion, flush=True)
    state = work / "state.json"
    app = subprocess.Popen([sys.executable, "scripts/smoke_app.py", str(state)])
    time.sleep(5)
    pad = json.loads(state.read_text())["widgets"]["pad"]
    x, y = pad[0] + pad[2] / 2, pad[1] + pad[3] / 2
    running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
    print("tk app", running, "policy", running.activationPolicy() if running else None, "front", front(), flush=True)
    print("hit at pad", hit(x, y), flush=True)
    for entry in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or []:
        bounds = dict(entry.get(q.kCGWindowBounds) or {})
        left, top = bounds.get("X", 0), bounds.get("Y", 0)
        if left <= x <= left + bounds.get("Width", 0) and top <= y <= top + bounds.get("Height", 0):
            owner = (entry.get(q.kCGWindowOwnerName), entry.get(q.kCGWindowOwnerPID), entry.get(q.kCGWindowName))
            print("  window at pad:", owner, "layer", entry.get(q.kCGWindowLayer), bounds, flush=True)
    if running:
        print("activate ->", running.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps), flush=True)
        time.sleep(2)
        print("front after activate", front(), "active", running.isActive(), flush=True)
    hid_click(x, y)
    print("after HID click: front", front(), "state", json.loads(state.read_text())["clicks"], flush=True)
    print("cursor now", q.CGEventGetLocation(q.CGEventCreate(None)), flush=True)
    os.kill(app.pid, signal.SIGKILL)


def focused_value(pid: int) -> str:
    focused = attr(ax.AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    if isinstance(focused, str):
        return focused
    return f"role {attr(focused, 'AXRole')} value {attr(focused, 'AXValue')!r}"


def deskpilot(*args: str) -> str:
    result = subprocess.run(["deskpilot", *args], capture_output=True, text=True, check=False)
    return f"{result.returncode} {result.stdout.strip()[:200]} {result.stderr.strip()[-300:]}"


def textedit_probe() -> None:
    command = ["deskpilot", "--json", "launch", "TextEdit"]
    launched = subprocess.run(command, capture_output=True, text=True, check=False)
    print("launch", launched.returncode, launched.stderr.strip(), flush=True)
    win = json.loads(launched.stdout)
    pid, address = win["pid"], win["address"]
    print("focused", focused_value(pid), "front", front(), flush=True)
    tree = json.loads(subprocess.run(["deskpilot", "--json", "elements", address], capture_output=True, text=True,
                                     check=False).stdout)  # fmt: skip
    print(json.dumps(tree)[:1500], flush=True)
    print("type a:", deskpilot("type", "a", "--window", address), focused_value(pid), flush=True)
    print("type slow bc:", deskpilot("type", "bc", "--window", address, "--delay", "0.05"), focused_value(pid),
          flush=True)  # fmt: skip
    print("click e3:", deskpilot("click", address, "--element", f"{tree['snapshot']}:e3"), focused_value(pid),
          flush=True)  # fmt: skip
    print("type d:", deskpilot("type", "d", "--window", address), focused_value(pid), flush=True)
    os.kill(pid, signal.SIGKILL)


tk_probe()
finder.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
time.sleep(1)
textedit_probe()
