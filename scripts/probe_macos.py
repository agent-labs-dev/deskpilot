"""Temporary CI probe: which macOS event routes reach an app on a hosted runner."""

import subprocess
import sys
import time

import AppKit  # ty: ignore[import-not-found]
import ApplicationServices as ax  # ty: ignore[import-not-found]
import Quartz as q  # ty: ignore[import-not-found]


def value(pid: int) -> str:
    app = ax.AXUIElementCreateApplication(pid)
    _, focused = ax.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
    if focused is None:
        return "<no focused element>"
    _, text = ax.AXUIElementCopyAttributeValue(focused, "AXValue", None)
    return repr(text)


def key(char: str, post) -> None:
    for down in (True, False):
        made = q.CGEventCreateKeyboardEvent(None, 0, down)
        q.CGEventKeyboardSetUnicodeString(made, 1, char)
        post(made)
        time.sleep(0.05)
    time.sleep(1)


finder = next(a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications() if a.localizedName() == "Finder")
subprocess.run(["open", "-g", "-a", "TextEdit"], check=True)
time.sleep(5)
app = next(a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications() if a.localizedName() == "TextEdit")
pid = app.processIdentifier()


def front() -> str:
    found = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return found.localizedName() if found else "none"


finder.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
time.sleep(2)
print("background: front", front(), "value", value(pid), flush=True)
key("a", lambda e: q.CGEventPostToPid(pid, e))
print("after PostToPid 'a' (background):", value(pid), flush=True)
windows = [
    w
    for w in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID)
    if w.get(q.kCGWindowOwnerPID) == pid and w.get(q.kCGWindowLayer) == 0
]
address = hex(int(windows[0][q.kCGWindowNumber])) if windows else None
print("TextEdit window", address, flush=True)
result = subprocess.run(
    ["deskpilot", "type", "b", "--window", str(address)], capture_output=True, text=True, check=False
)
print(
    "deskpilot type 'b' (background):",
    result.returncode,
    result.stderr.strip(),
    value(pid),
    "front",
    front(),
    flush=True,
)
app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
time.sleep(2)
print("front after activate", front(), flush=True)
result = subprocess.run(
    ["deskpilot", "type", "c", "--window", str(address)], capture_output=True, text=True, check=False
)
print("deskpilot type 'c' (front):", result.returncode, result.stderr.strip(), value(pid), flush=True)
key("d", lambda e: q.CGEventPostToPid(pid, e))
print("after PostToPid 'd' (front):", value(pid), flush=True)
app.terminate()

cover = subprocess.Popen([sys.executable, "scripts/smoke_app.py", "/tmp/probe-state.json"])
time.sleep(4)
top = subprocess.Popen(
    [sys.executable, "scripts/smoke_app.py", "/tmp/probe-cover.json", "--cover", "/tmp/probe-state.json"]
)
time.sleep(6)
for entry in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll, q.kCGNullWindowID) or []:
    if entry.get(q.kCGWindowOwnerPID) in (cover.pid, top.pid):
        print(
            "tk window",
            entry.get(q.kCGWindowOwnerPID),
            repr(entry.get(q.kCGWindowName)),
            "layer",
            entry.get(q.kCGWindowLayer),
            "onscreen",
            entry.get(q.kCGWindowIsOnscreen),
            dict(entry.get(q.kCGWindowBounds) or {}),
            flush=True,
        )
print("modal panel level constant", q.kCGModalPanelWindowLevel, flush=True)
cover.terminate()
top.terminate()
