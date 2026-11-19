import json

from deskpilot import session


def test_exported_environment_does_not_reuse_another_agents_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DESKPILOT_AGENT", "tester-browser")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "parent")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-parent")
    for name in ("DESKPILOT_ISOLATED", session.STANDALONE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(session, "current_instance", lambda: "parent")
    monkeypatch.setattr(session, "live", lambda record: record.get("signature") == "browser")
    browser_session = session._directory()
    (browser_session / "ready.json").write_text(json.dumps({"signature": "browser"}))

    # A shell has exported tester-browser's desktop, then names a different agent.
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "browser")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-browser")
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    monkeypatch.setattr(session, "current_instance", lambda: "browser")

    assert session._directory() != browser_session
