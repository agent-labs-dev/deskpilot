"""End-to-end check of the Windows or macOS backend through the MCP server, on a CI runner's desktop.

It starts `deskpilot mcp` as an MCP client would, opens the platform's own editor (Notepad, TextEdit), and drives it
through the tools: list windows, read the accessibility tree, click the text area, type, press a chord, run a batch,
and take screenshots. Each result is read back from the tree. Screenshots go to $SMOKE_SHOTS (default: a temp dir).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MAC = sys.platform == "darwin"
EDITOR, ROLES = (["TextEdit"], ("TextArea",)) if MAC else (["notepad.exe"], ("edit", "document"))
SELECT_ALL = "cmd+a" if MAC else "ctrl+a"
SHOTS = Path(os.environ.get("SMOKE_SHOTS") or tempfile.mkdtemp(prefix="deskpilot-mcp-"))
SHOTS.mkdir(parents=True, exist_ok=True)


def walk(node: dict) -> list[dict]:
    return [node] + [n for c in node.get("children", []) for n in walk(c)]


def texts(tree: dict) -> list[str]:
    return [t for e in walk(tree) for t in (e.get("text"), e.get("name")) if t]


async def main() -> None:
    params = StdioServerParameters(command="deskpilot", args=["mcp"], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(tool: str, **arguments):
            result = await session.call_tool(tool, arguments)
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:  # a tool that returns a list wraps it
                body = body["result"]
            print(
                f"mcp {tool} -> {'error' if result.is_error else 'ok'}: {str(body or result.content)[:300]}", flush=True
            )
            assert not result.is_error, f"{tool} failed"
            return body

        async def contains(window: str, text: str) -> None:
            deadline = time.monotonic() + 8
            while True:
                found = texts(await call("elements", window=window))
                if any(text in t for t in found):
                    return
                assert time.monotonic() < deadline, f"{text!r} never reached the tree: {found}"
                await asyncio.sleep(0.5)

        tools = {t.name for t in (await session.list_tools()).tools}
        assert {"windows", "launch", "elements", "click", "type_text", "key", "batch", "shot"} <= tools, tools
        win = await call("launch", command=EDITOR)
        address = win["address"]
        assert any(w["address"] == address for w in await call("windows")), "the launched window is not listed"
        tree = await call("elements", window=address)
        area = next(e for e in walk(tree) if e["role"] in ROLES)
        await call("click", window=address, element=f"{tree['snapshot']}:{area['id']}")
        await call("type_text", text="typed over mcp", window=address)
        await contains(address, "typed over mcp")
        await call("shot", window=address, out=str(SHOTS / "mcp-typed.png"))
        await call("key", combo=SELECT_ALL, window=address)
        await call("key", combo="BackSpace", window=address)
        await asyncio.sleep(0.5)
        assert not any("typed over mcp" in t for t in texts(await call("elements", window=address))), "not cleared"
        tree = await call("elements", window=address)
        area = next(e for e in walk(tree) if e["role"] in ROLES)
        await call(
            "batch",
            window=address,
            actions=[
                {"action": "click", "element": f"{tree['snapshot']}:{area['id']}"},
                {"action": "type", "text": "typed by batch"},
            ],
            final_shot=True,
        )
        await contains(address, "typed by batch")
        await call("shot", window=address, out=str(SHOTS / "mcp-batch.png"))
        failed = await session.call_tool("click", {"window": address, "element": "e99999"})
        assert failed.is_error, "a click on an element that is not there was not refused"
        print("mcp smoke ok", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
