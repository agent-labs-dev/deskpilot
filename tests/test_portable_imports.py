"""The cross-platform commands import no Hyprland-only module (the plugin needs fcntl, which Windows lacks)."""

from __future__ import annotations

import subprocess
import sys


def test_recipe_and_mcp_import_without_fcntl():
    code = (
        "import sys; sys.modules['fcntl'] = None\n"
        "import deskpilot.recipe, deskpilot.native, deskpilot.batch, deskpilot.keys\n"
        "assert 'deskpilot.plugin' not in sys.modules and 'deskpilot.desk' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_record_start_is_refused_where_there_is_no_recorder(monkeypatch):
    from deskpilot import mcp_server

    monkeypatch.setattr(sys, "platform", "win32")
    result = mcp_server.record_start("notes", "/tmp/x.mp4")
    assert result.is_error and result.structured_content["code"] == "unavailable"
