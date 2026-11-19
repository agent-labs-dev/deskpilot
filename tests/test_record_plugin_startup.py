import io
import threading

import pytest
from test_core import make_desk

from deskpilot import screen
from deskpilot.plugin import Plugin, PluginError, source_hash


@pytest.mark.parametrize("version", [None, "older-build"])
def test_record_loads_the_recording_plugin_before_its_first_frame(tmp_path, monkeypatch, version):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake.clients[0] = {**fake.clients[0], "xwayland": False, "stableId": "18000001"}
    loaded = {"version": version}
    calls = []

    def run(args):
        assert args == ["deskpilot", "status"]
        if loaded["version"] is None:
            return "unknown request"
        return '{"ok":true,"version":"' + loaded["version"] + '"}'

    def load():
        calls.append("load")
        loaded["version"] = source_hash()
        return tmp_path / "plugin.so"

    desk.plugin = Plugin(run=run)
    monkeypatch.setattr(desk.plugin, "load", load)

    def frame(window):
        assert loaded["version"] == source_hash(), "record captured before a plugin with recording support was active"
        calls.append("frame")
        return b"P6\n2 2\n255\n" + bytes(12)

    monkeypatch.setattr(screen, "frame", frame)
    desk.record("battle", io.BytesIO(), threading.Event(), seconds=0.01)
    assert calls[:2] == ["load", "frame"]


def test_record_reports_a_plugin_load_failure_before_capturing(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake.clients[0] = {**fake.clients[0], "xwayland": False, "stableId": "18000001"}
    desk.plugin = Plugin(run=lambda args: "unknown request")

    def load():
        raise PluginError("install Hyprland headers before recording", "unavailable")

    monkeypatch.setattr(desk.plugin, "load", load)
    frames = []
    monkeypatch.setattr(screen, "frame", lambda window: frames.append(window) or b"frame")
    with pytest.raises(PluginError, match="install Hyprland headers"):
        desk.record("battle", io.BytesIO(), threading.Event(), seconds=0.01)
    assert frames == []
