"""Pointer borrowing, held buttons, plugin build pruning, the route and accessibility advertising."""

import json

import pytest
from test_core import AGENT_CLIENT, CLIENT, GATE, gated_launch, make_desk

from deskpilot import a11y, plugin
from deskpilot.uinput import VirtualInput


class FakeUInput:
    def __init__(self):
        self.written, self.closed = [], False

    def write(self, etype, code, value):
        self.written.append((code, value))

    def syn(self):
        pass

    def close(self):
        self.closed = True


def virtual(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    device = VirtualInput.__new__(VirtualInput)
    from evdev import ecodes

    device._e, device._down = ecodes, set()
    device._ui = FakeUInput()  # ty: ignore[invalid-assignment]
    return device


def test_a_click_interrupted_mid_press_still_releases_the_button(monkeypatch):
    device = virtual(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))  # during the hold
    with pytest.raises(KeyboardInterrupt):
        device.click("left", 1)
    assert not device._down and device._ui.written[-1][1] == 0, "the release was sent"


def test_closing_the_device_releases_a_held_button(monkeypatch):
    device = virtual(monkeypatch)
    device.button("left", True)
    device.close()
    assert device._ui.written[-1][1] == 0 and not device._down and device._ui.closed


class Cursor:
    """A hyprctl that reports a focused window and a cursor position, as the user left them."""

    def __init__(self, fake, other):
        self.fake, self.other = fake, other
        fake_call = fake.__call__

        def call(args):
            if args[:2] == ["-j", "cursorpos"]:
                return json.dumps({"x": 11, "y": 22})
            if args[:2] == ["-j", "activewindow"]:
                return json.dumps(other)
            return fake_call(args)

        fake.__class__ = type("Wrapped", (fake.__class__,), {"__call__": lambda self, args: call(args)})


def test_take_pointer_gives_back_the_users_focus_and_pointer(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    other = {**CLIENT, "address": "0xdef", "class": "editor", "title": "Editor", "at": [0, 0]}
    fake.clients.append(other)
    Cursor(fake, other)
    desk.click("notepad", 100, 50, take_pointer=True)
    assert ["dispatch", "hl.dsp.cursor.move({x=11,y=22})"] in fake.calls
    assert fake.calls[-2:] == [
        ["dispatch", 'hl.dsp.focus({window="address:0xdef"})'],
        ["dispatch", "hl.dsp.cursor.move({x=11,y=22})"],
    ]
    assert desk.route == "pointer"


def test_prune_builds_keeps_only_the_named_builds(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    folder = plugin.build_dir()
    folder.mkdir(parents=True, exist_ok=True)
    old, keep, other = folder / "deskpilot-old-1.so", folder / "deskpilot-new-2.so", folder / "notes.txt"
    for path in (old, keep, other):
        path.write_text("x")
    assert plugin.prune_builds({keep}) == [old]
    assert not old.exists() and keep.exists() and other.exists()


def test_a_pointer_action_reports_its_route(tmp_path, monkeypatch):
    desk, _, _, plugin_ = make_desk(tmp_path, monkeypatch)
    plugin_.path = lambda *args: {"ok": True, "ticket": 9, "route": "agent"}
    gx, gy = desk.click("notepad", 100, 50)
    assert desk.landed(gx, gy) == {"x": gx, "y": gy, "route": "agent"}
    desk.move("notepad", 100, 50)
    assert "route" not in desk.landed(gx, gy)


def advertise(statuses, desktop=""):
    """The properties set on a status object that starts with `statuses`."""
    sets = []

    class Session:
        def call(self, destination, object_path, interface, method, signature, body):
            if method == "Get":
                return (("b", statuses[body[1]]),)
            sets.append(body[1])

    a11y.advertise_accessibility(Session(), desktop)
    return sets


def test_advertising_sets_only_flags_that_are_false():
    assert advertise({"IsEnabled": False, "ScreenReaderEnabled": False}) == ["IsEnabled", "ScreenReaderEnabled"]
    assert advertise({"IsEnabled": True, "ScreenReaderEnabled": True}) == []


def test_advertising_never_claims_a_screen_reader_on_gnome():
    assert advertise({"IsEnabled": False, "ScreenReaderEnabled": False}, "ubuntu:GNOME") == ["IsEnabled"]


def test_launch_asks_every_toolkit_for_its_accessibility_tree(tmp_path, monkeypatch):
    desk, fake, _ = gated_launch(tmp_path, monkeypatch)
    GATE.on_start = lambda: fake.clients.append({**AGENT_CLIENT, "stableId": "2"})
    desk.launch(["gnome-calculator"])
    env = GATE.made[0].env
    assert (env["ACCESSIBILITY_ENABLED"], env["NO_AT_BRIDGE"], env["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"]) == (
        "1",
        "0",
        "1",
    )
