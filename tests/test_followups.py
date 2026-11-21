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

    changed = a11y.advertise_accessibility(Session(), desktop)
    assert changed == bool(sets)
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


def test_a_failed_take_pointer_still_gives_focus_and_pointer_back(tmp_path, monkeypatch):
    desk, fake, inp, _ = make_desk(tmp_path, monkeypatch)
    other = {**CLIENT, "address": "0xdef", "class": "editor", "title": "Editor", "at": [0, 0]}
    fake.clients.append(other)
    Cursor(fake, other)

    def fail():
        raise OSError("no uinput")

    inp.nudge = fail
    with pytest.raises(OSError):
        desk.click("notepad", 100, 50, take_pointer=True)
    assert fake.calls[-1] == ["dispatch", "hl.dsp.cursor.move({x=11,y=22})"]
    assert ["dispatch", 'hl.dsp.focus({window="address:0xdef"})'] in fake.calls[-2:]


def test_launch_adds_chromium_page_accessibility_once():
    from deskpilot.desk import _with_page_accessibility as add

    assert add(["google-chrome-stable", "x"]) == ["google-chrome-stable", "--force-renderer-accessibility", "x"]
    assert add(["/opt/brave/brave", "--force-renderer-accessibility"]) == [
        "/opt/brave/brave",
        "--force-renderer-accessibility",
    ]
    assert add(["gnome-calculator"]) == ["gnome-calculator"]


def test_a_press_whose_write_failed_is_released_on_close(monkeypatch):
    device = virtual(monkeypatch)
    real = device._emit
    fail = {"on": True}

    def emit(etype, code, value):
        real(etype, code, value)
        if fail["on"]:
            raise OSError("write failed after the press was delivered")

    monkeypatch.setattr(device, "_emit", emit)
    with pytest.raises(OSError):
        device.click("left", 1)
    assert device._down == {"left"}, "the failed release is kept to try again"
    fail["on"] = False
    device.close()
    assert not device._down and device._ui.written[-1][1] == 0


def test_batch_click_results_carry_the_route(tmp_path, monkeypatch):
    from test_batch import CLICK, request

    desk, _, _, plugin_ = make_desk(tmp_path, monkeypatch)
    plugin_.path = lambda *args: {"ok": True, "ticket": 9, "route": "client"}
    result = desk.batch(request([CLICK]))
    assert result.results[0]["route"] == "client"


def test_an_unknown_button_is_never_recorded_as_held(monkeypatch):
    device = virtual(monkeypatch)
    with pytest.raises(KeyError):
        device.button("back", True)
    assert not device._down


def test_each_thread_reports_its_own_route(tmp_path, monkeypatch):
    import threading

    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    desk.route = "agent"
    seen = []
    thread = threading.Thread(target=lambda: seen.append(desk.route))
    thread.start()
    thread.join()
    assert seen == [None] and desk.route == "agent"
