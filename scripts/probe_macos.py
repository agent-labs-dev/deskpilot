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


subprocess.run(["open", "-a", "TextEdit"], check=True)
time.sleep(5)
app = next(a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications() if a.localizedName() == "TextEdit")
pid = app.processIdentifier()
front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
print("TextEdit", pid, "front", front.localizedName() if front else None, "value", value(pid), flush=True)
key("a", lambda e: q.CGEventPostToPid(pid, e))
print("after PostToPid 'a':", value(pid), flush=True)
app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
time.sleep(2)
front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
print("front after activate", front.localizedName() if front else None, flush=True)
key("b", lambda e: q.CGEventPostToPid(pid, e))
print("after PostToPid 'b' (front):", value(pid), flush=True)
key("c", lambda e: q.CGEventPost(q.kCGHIDEventTap, e))
print("after HID tap 'c':", value(pid), flush=True)
key("d", lambda e: q.CGEventPost(q.kCGSessionEventTap, e))
print("after session tap 'd':", value(pid), flush=True)
source = q.CGEventSourceCreate(q.kCGEventSourceStateHIDSystemState)
for down in (True, False):
    q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateKeyboardEvent(source, 14, down))  # 'e'
    time.sleep(0.05)
time.sleep(1)
print("after HID tap keycode e with source:", value(pid), flush=True)
subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "f"'], check=False)
time.sleep(1)
print("after System Events 'f':", value(pid), flush=True)
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
