import asyncio
import json
from contextlib import contextmanager

import pytest
from mcp.types import CallToolResult
from test_a11y import FakeBus, add
from test_core import make_desk

from deskpilot import a11y, cli, mcp_server


@pytest.mark.parametrize("transport", ["cli", "mcp"])
def test_a_deep_tree_reaches_the_caller_whole_over_batches(tmp_path, monkeypatch, capsys, transport):
    # Each transport's serialiser stops at a fixed depth, so a 600-level tree arrives as batches it can carry.
    desk, *_ = make_desk(tmp_path, monkeypatch)
    fake = FakeBus(pid=7, title="Notepad")
    parent = "/button"
    for level in range(600):
        path = f"/deep{level}"
        add(fake, parent, path, f"level {level}")
        parent = path

    @contextmanager
    def connection(bus):
        yield fake

    monkeypatch.setattr(a11y, "_bus_context", connection)
    monkeypatch.setattr(cli, "Desk", lambda: desk)
    monkeypatch.setattr(mcp_server, "desk", desk)

    def read(more):
        if transport == "cli":
            assert cli.main(["--json", "elements", "notepad", *(["--more", more] if more else [])]) == 0
            return capsys.readouterr().out
        result = asyncio.run(mcp_server.mcp.call_tool("elements", {"window": "notepad", "more": more}))
        assert isinstance(result, CallToolResult) and not result.is_error
        return "\n".join(block.text for block in result.content if block.type == "text")

    output = read("")
    batches = 1
    while more := json.loads(output).get("more"):
        output = read(more)
        batches += 1
    assert '"name": "level 599"' in output
    assert batches == 13
