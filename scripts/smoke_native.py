"""End-to-end check of the Windows or macOS backend through the CLI, on a CI runner's desktop.

It opens scripts/smoke_app.py, a Tk window that writes every input it receives to a state file, drives each pointer,
key, clipboard and wait action at it through `deskpilot`, and checks the app saw exactly that. A second window over the
app checks that a covered point is refused. Then it types into the platform's own editor (Notepad, TextEdit) and
reads the text back from the accessibility tree.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

APP = Path(__file__).with_name("smoke_app.py")
work = Path(tempfile.mkdtemp(prefix="deskpilot-smoke-"))


def run(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["deskpilot", "--json", *args], capture_output=True, text=True, check=False)
    print(
        f"$ deskpilot {' '.join(args)} -> {result.returncode}\n{result.stdout[:400]}{result.stderr[-600:]}", flush=True
    )
    return result


def deskpilot(*args: str):
    result = run(*args)
    if result.returncode != 0:
        raise SystemExit(f"deskpilot {args[0]} failed")
    return json.loads(result.stdout)


def refused(code: str, *args: str) -> None:
    result = run(*args)
    # With --json an error is one JSON object on stderr.
    assert result.returncode != 0 and json.loads(result.stderr)["code"] == code, f"expected {code}"


def until(state: Path, check: Callable[[dict], bool], what: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            current = json.loads(state.read_text())
        except (OSError, ValueError):
            current = {}
        if current and check(current):
            print(f"ok: {what}", flush=True)
            return current
        if time.monotonic() > deadline:
            raise SystemExit(f"the app never saw {what}: {current}")
        time.sleep(0.1)


def walk(node: dict) -> list[dict]:
    return [node] + [n for c in node.get("children", []) for n in walk(c)]


def texts(node: dict) -> list[str]:
    return [node.get("text") or "", node.get("name") or ""] + [t for c in node.get("children", []) for t in texts(c)]


def print_macos_windows() -> None:
    """Every on-screen window Quartz lists, front to back, to explain a covered point on a runner."""
    import Quartz  # ty: ignore[import-not-found]  # pyobjc, installed on macOS only

    options = Quartz.kCGWindowListOptionOnScreenOnly
    for entry in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        bounds = dict(entry.get("kCGWindowBounds") or {})
        print(
            hex(int(entry["kCGWindowNumber"])), entry.get("kCGWindowOwnerName"), repr(entry.get("kCGWindowName")),
            "layer", entry.get("kCGWindowLayer"), "alpha", entry.get("kCGWindowAlpha"), bounds, flush=True,
        )  # fmt: skip


failures: list[str] = []


def step(name: str, action: Callable[[], object]) -> None:
    """Run one check and carry on after a failure, so one run reports every action that does not work."""
    print(f"--- {name}", flush=True)
    try:
        action()
    except (Exception, SystemExit) as error:  # report the step and run the rest
        print(f"FAILED {name}: {error!r}", flush=True)
        failures.append(name)


def app_smoke() -> None:  # noqa: PLR0915 - one check per action, in the order they run
    state = work / "state.json"
    win = deskpilot("launch", sys.executable, str(APP), str(state))
    address = win["address"]
    current = until(state, lambda s: "widgets" in s, "its layout")
    if sys.platform == "darwin":
        print_macos_windows()
    # Widget centres in window pixels, from the app's own screen rectangles and the window's origin.
    left, top = win["x"], win["y"]
    at = {name: (str(x + w // 2 - left), str(y + h // 2 - top)) for name, (x, y, w, h) in current["widgets"].items()}
    act = {name: (address, *point, "--scale", "1") for name, point in at.items()}
    seen = lambda: json.loads(state.read_text())  # noqa: E731

    def click() -> None:
        before = deskpilot("shot", address)
        deskpilot("click", *act["pad"])
        until(state, lambda s: [c["kind"] for c in s["clicks"]] == ["left"], "one left click")
        deskpilot("wait-for", address, "--changed", before["shot"], "--timeout", "10")

    def double() -> None:
        deskpilot("click", *act["pad"], "--double")
        until(state, lambda s: "double" in [c["kind"] for c in s["clicks"]], "a double click")

    def right() -> None:
        deskpilot("click", *act["pad"], "--button", "right")
        until(state, lambda s: s["clicks"] and s["clicks"][-1]["kind"] == "right", "a right click")

    def scroll() -> None:
        deskpilot("scroll", *act["pad"][:3], "3", "--scale", "1")
        until(state, lambda s: s["scroll"] and s["scroll"][-1] > 0, "a scroll up")
        deskpilot("scroll", *act["pad"][:3], "-2", "--scale", "1")
        until(state, lambda s: s["scroll"][-1] < 0, "a scroll down")

    def drag() -> None:
        x, y, w, h = current["widgets"]["track"]
        y1 = str(y + h // 2 - top)
        deskpilot("drag", address, str(x + 20 - left), y1, str(x + w - 20 - left), y1, "--scale", "1")
        moved = until(state, lambda s: s["drag"] and s["drag"][-1][0] == "up", "a drag released")
        assert moved["drag"][0][0] == "down" and moved["drag"][-1][1] - moved["drag"][0][1] > w // 2, moved["drag"]

    def hover() -> None:
        pressed = seen()["pressed"]
        deskpilot("hover", *act["spot"], "--wait", "0.5", "--click", *at["button"])
        until(state, lambda s: s["hover"] > 0 and s["pressed"] == pressed + 1, "a hover, then a click from it")
        deskpilot("wait-for", address, "--title", f"pressed {pressed + 1}", "--timeout", "10")

    def keys() -> None:
        deskpilot("click", *act["entry"])
        deskpilot("type", "hello deskpilot", "--window", address)
        until(state, lambda s: s["entry"] == "hello deskpilot", "typed text")
        deskpilot("key", "ctrl+a", "--window", address)
        until(state, lambda s: ["a", True] in s["keys"], "ctrl+a")
        deskpilot("key", "BackSpace", "--window", address)
        until(state, lambda s: s["entry"] == "", "a chord then a key")

    def clipboard() -> None:
        deskpilot("clipboard", "--set", "deskpilot clipboard")
        assert deskpilot("clipboard") == "deskpilot clipboard"

    def covered() -> None:
        cover_state = work / "cover.json"
        cover = deskpilot("launch", sys.executable, str(APP), str(cover_state), "--cover", str(state))
        try:
            until(cover_state, lambda s: "root" in s, "the cover window")
            pad_x, pad_y, _, _ = current["widgets"]["pad"]
            clicks = len(seen()["clicks"])
            refused("covered", "click", address, str(pad_x + 30 - left), str(pad_y + 30 - top), "--scale", "1")
            assert len(seen()["clicks"]) == clicks, "a refused click still reached the app"
        finally:
            os.kill(cover["pid"], signal.SIGTERM)
        deskpilot("wait-for", cover["address"], "--gone", "--timeout", "10")

    def focused_click() -> None:
        pressed = seen()["pressed"]
        deskpilot("click", *act["button"], "--take-focus")
        until(state, lambda s: s["pressed"] == pressed + 1, "a click that took focus")

    for name, action in [("click", click), ("double click", double), ("right click", right), ("scroll", scroll),
                         ("drag", drag), ("hover", hover), ("keys", keys), ("clipboard", clipboard),
                         ("covered", covered), ("click with take-focus", focused_click)]:  # fmt: skip
        step(name, action)
    os.kill(win["pid"], signal.SIGTERM)


def editor_smoke(command: str, roles: tuple[str, ...]) -> None:
    win = deskpilot("launch", command)
    address = win["address"]
    tree = deskpilot("elements", address)
    edit = next(e for e in walk(tree) if e["role"] in roles)
    deskpilot("click", address, "--element", f"{tree['snapshot']}:{edit['id']}")
    deskpilot("type", "deskpilot smoke", "--window", address)
    deadline = time.monotonic() + 5
    while not any("deskpilot smoke" in t for t in texts(deskpilot("elements", address))):
        assert time.monotonic() < deadline, f"the typed text is not in {command}'s tree"
        time.sleep(0.5)


if __name__ == "__main__":
    app_smoke()
    if sys.platform == "win32":
        step("Notepad", lambda: editor_smoke("notepad.exe", ("edit", "document")))
    else:
        step("TextEdit", lambda: editor_smoke("TextEdit", ("TextArea",)))
    if failures:
        raise SystemExit(f"failed: {', '.join(failures)}")
    print("smoke ok", flush=True)
