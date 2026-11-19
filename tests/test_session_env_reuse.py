import json
import os
import shlex

from deskpilot import cli, session


def test_exported_nested_environment_reuses_the_same_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "parent")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-parent")
    for name in ("DESKPILOT_ISOLATED", session.STANDALONE_ENV, session.TOKEN_ENV, "DESKPILOT_OWNER_PID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(session, "current_instance", lambda: os.environ["HYPRLAND_INSTANCE_SIGNATURE"])
    monkeypatch.setattr(session, "live", lambda record: record.get("signature") == "nested")
    original = session._directory()
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": "nested",
        "WAYLAND_DISPLAY": "wayland-nested",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/dbus",
        "AT_SPI_BUS_ADDRESS": "unix:path=/private/a11y",
        "DESKPILOT_ISOLATED": "nested",
    }
    (original / "ready.json").write_text(json.dumps({"signature": "nested", "parent": "parent", "env": env}))
    # The CLI runs in a child process: only its printed exports reach the shell.
    with monkeypatch.context() as child:
        child.setattr(os, "environ", dict(os.environ))
        assert cli.main(["--session", "env"]) == 0
    exports = capsys.readouterr().out
    for line in exports.splitlines():
        _, assignment = shlex.split(line)
        key, value = assignment.split("=", 1)
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DESKPILOT_ISOLATED", raising=False)
    assert session._directory() == original
