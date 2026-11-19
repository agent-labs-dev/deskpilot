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
    assert result.returncode != 0 and f"deskpilot: {code}:" in result.stderr, f"expected {code}"


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


def app_smoke() -> None:
    state = work / "state.json"
    win = deskpilot("launch", sys.executable, str(APP), str(state))
    address = win["address"]
    current = until(state, lambda s: "widgets" in s, "its layout")
    # Widget centres in window pixels, from the app's own screen rectangles and the window's origin.
    left, top = win["x"], win["y"]
    at = {name: (str(x + w // 2 - left), str(y + h // 2 - top)) for name, (x, y, w, h) in current["widgets"].items()}
    act = {name: (address, *point, "--scale", "1") for name, point in at.items()}

    before = deskpilot("shot", address)
    deskpilot("click", *act["pad"])
    until(state, lambda s: [c["kind"] for c in s["clicks"]] == ["left"], "one left click")
    deskpilot("wait-for", address, "--changed", before["shot"], "--timeout", "10")
    deskpilot("click", *act["pad"], "--double")
    until(state, lambda s: "double" in [c["kind"] for c in s["clicks"]], "a double click")
    deskpilot("click", *act["pad"], "--button", "right")
    until(state, lambda s: s["clicks"][-1]["kind"] == "right", "a right click")

    deskpilot("scroll", *act["pad"][:3], "3", "--scale", "1")
    until(state, lambda s: s["scroll"] and all(d > 0 for d in s["scroll"]), "a scroll up")
    deskpilot("scroll", *act["pad"][:3], "-2", "--scale", "1")
    until(state, lambda s: s["scroll"][-1] < 0, "a scroll down")

    x, y, w, h = current["widgets"]["track"]
    y1 = str(y + h // 2 - top)
    deskpilot("drag", address, str(x + 20 - left), y1, str(x + w - 20 - left), y1, "--scale", "1")
    moved = until(state, lambda s: s["drag"] and s["drag"][-1][0] == "up", "a drag released")
    assert moved["drag"][0][0] == "down" and moved["drag"][-1][1] - moved["drag"][0][1] > w // 2, moved["drag"]

    deskpilot("hover", *act["spot"], "--wait", "0.5", "--click", *at["button"])
    until(state, lambda s: s["hover"] > 0 and s["pressed"] == 1, "a hover, then a click from it")
    deskpilot("wait-for", address, "--title", "pressed 1", "--timeout", "10")

    deskpilot("click", *act["entry"])
    deskpilot("type", "hello deskpilot", "--window", address)
    until(state, lambda s: s["entry"] == "hello deskpilot", "typed text")
    deskpilot("key", "ctrl+a", "--window", address)
    until(state, lambda s: ["a", True] in s["keys"], "ctrl+a")
    deskpilot("key", "BackSpace", "--window", address)
    until(state, lambda s: s["entry"] == "", "a chord then a key")

    deskpilot("clipboard", "--set", "deskpilot clipboard")
    assert deskpilot("clipboard") == "deskpilot clipboard"

    cover_state = work / "cover.json"
    cover = deskpilot("launch", sys.executable, str(APP), str(cover_state), "--cover", str(state))
    until(cover_state, lambda s: "root" in s, "the cover window")
    pad_x, pad_y, _, _ = current["widgets"]["pad"]
    clicks = len(json.loads(state.read_text())["clicks"])
    refused("covered", "click", address, str(pad_x + 30 - left), str(pad_y + 30 - top), "--scale", "1")
    assert len(json.loads(state.read_text())["clicks"]) == clicks, "a refused click still reached the app"
    os.kill(cover["pid"], signal.SIGTERM)
    deskpilot("wait-for", cover["address"], "--gone", "--timeout", "10")
    os.kill(win["pid"], signal.SIGTERM)
    print("app smoke ok", flush=True)


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
    print(f"{command} smoke ok", flush=True)


if __name__ == "__main__":
    app_smoke()
    if sys.platform == "win32":
        editor_smoke("notepad.exe", ("edit", "document"))
    else:
        editor_smoke("TextEdit", ("TextArea",))
