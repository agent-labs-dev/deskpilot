"""End-to-end check of the Windows or macOS backend through the CLI, on a CI runner's desktop.

Windows: launch Notepad, shoot it, type into it without focusing it, and read the text back from its UI Automation
tree. macOS: list windows, and shoot one when the runner has granted Screen Recording; without the grant the shot must
fail as `unavailable`, never crash.
"""

from __future__ import annotations

import json
import subprocess
import sys


def deskpilot(*args: str, ok: bool = True) -> dict | list:
    result = subprocess.run(["deskpilot", "--json", *args], capture_output=True, text=True, check=False)
    print(f"$ deskpilot {' '.join(args)} -> {result.returncode}\n{result.stdout[-800:]}{result.stderr[-800:]}")
    if ok and result.returncode != 0:
        raise SystemExit(f"deskpilot {args[0]} failed")
    return json.loads(result.stdout) if result.returncode == 0 else {"stderr": result.stderr}


def texts(node: dict) -> list[str]:
    return [node.get("text") or "", node.get("name") or ""] + [t for c in node.get("children", []) for t in texts(c)]


def windows_smoke() -> None:
    win = deskpilot("launch", "notepad.exe")
    assert isinstance(win, dict)
    address = win["address"]
    shot = deskpilot("shot", address)
    assert isinstance(shot, dict) and shot["width"] > 0
    deskpilot("type", "deskpilot smoke", "--window", address)
    tree = deskpilot("elements", address)
    assert isinstance(tree, dict)
    assert any("deskpilot smoke" in t for t in texts(tree)), "the typed text is not in Notepad's tree"
    print("windows smoke ok")


def macos_smoke() -> None:
    found = deskpilot("windows")
    assert isinstance(found, list)
    on_screen = [w for w in found if w["visible"]]
    if on_screen:
        result = subprocess.run(
            ["deskpilot", "--json", "shot", on_screen[0]["address"]],
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout[-400:], result.stderr[-400:])
        assert result.returncode == 0 or "unavailable" in result.stderr, "a shot failed without a code"
    print("macos smoke ok")


if __name__ == "__main__":
    windows_smoke() if sys.platform == "win32" else macos_smoke()
