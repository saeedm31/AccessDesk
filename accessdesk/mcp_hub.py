"""MCP client: starts the five MCP servers as child processes (stdio) and calls their tools.

Identity: tools that act for a person take an `identity` argument. The hub fills it with the signed
token of the logged-in employee and hides it from the model's tool definitions, so the model can
neither see it nor claim to be someone else.

The SDK is async; Streamlit and the agent graph are not. One background event loop owns the
sessions, and `call()` is a normal blocking function that is safe to use from any thread.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .config import ROOT, secret

SERVERS = {
    "directory": "accessdesk.mcp_servers.directory",
    "catalog": "accessdesk.mcp_servers.catalog",
    "itsm": "accessdesk.mcp_servers.itsm",
    "iam": "accessdesk.mcp_servers.iam",
    "memory": "accessdesk.mcp_servers.memory",
}
# The only tools the model may call. Each must also be marked read-only by its server.
AGENT_TOOLS = {
    ("directory", "whoami"),
    ("directory", "find_employee"),
    ("directory", "get_access"),
    ("catalog", "search_entitlements"),
    ("catalog", "team_patterns"),
    ("catalog", "preview_request"),
    ("itsm", "my_tickets"),
    ("itsm", "get_ticket"),
    ("memory", "recall"),
}


@dataclass
class ToolResult:
    ok: bool
    data: dict | None
    error: str | None
    ms: int


class MCPHub:
    def __init__(self, data_dir: Path, timeout_s: float = 30):
        self.data_dir = Path(data_dir)
        self.timeout_s = timeout_s
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, dict] = {}  # tool name -> {server, description, input_schema, read_only}
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="mcp-hub", daemon=True).start()
        self._owner = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        if not self._ready.wait(timeout_s):
            err = self._owner.exception() if self._owner.done() else "timeout"
            raise RuntimeError(f"MCP servers did not start: {err}")
        atexit.register(self.close)

    async def _run(self):
        self._stop = asyncio.Event()
        env = {
            **os.environ,
            "AD_DATA_DIR": str(self.data_dir),
            "IDENTITY_SECRET": secret(self.data_dir, "identity"),
            "GRANT_SECRET": secret(self.data_dir, "grant"),
            "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        errlog = open(self.data_dir / "mcp_servers.log", "a")  # noqa: SIM115 - lives as long as the hub
        # MCP_URLS="directory=http://directory:8201/mcp,...": connect over Streamable HTTP (docker-compose)
        urls = dict(x.split("=", 1) for x in os.getenv("MCP_URLS", "").split(",") if "=" in x)
        async with AsyncExitStack() as stack:
            for name, module in SERVERS.items():
                if name in urls:
                    from mcp.client.streamable_http import streamable_http_client

                    read, write = await stack.enter_async_context(streamable_http_client(urls[name]))
                else:
                    params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env, cwd=str(ROOT))
                    read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session
                for t in (await session.list_tools()).tools:
                    ann = t.annotations
                    self.tools[t.name] = dict(
                        server=name,
                        description=t.description or "",
                        input_schema=t.input_schema,
                        read_only=bool(ann and ann.read_only_hint),
                    )
            self._ready.set()
            await self._stop.wait()
        errlog.close()

    def call(self, tool: str, args: dict | None = None, identity: str | None = None) -> ToolResult:
        t0 = time.time()
        meta = self.tools.get(tool)
        if not meta:
            return ToolResult(False, None, f"unknown tool {tool}", 0)
        args = dict(args or {})
        args.pop("identity", None)  # never taken from the caller's arguments (e.g. the model's)
        if "identity" in meta["input_schema"].get("properties", {}):
            if not identity:
                return ToolResult(False, None, "not authenticated", 0)
            args["identity"] = identity
        fut = asyncio.run_coroutine_threadsafe(self.sessions[meta["server"]].call_tool(tool, args), self._loop)
        try:
            res = fut.result(self.timeout_s)
        except Exception as exc:  # timeout or transport error
            return ToolResult(False, None, f"{type(exc).__name__}: {exc}", int(1000 * (time.time() - t0)))
        text = "".join(getattr(c, "text", "") for c in res.content)
        ms = int(1000 * (time.time() - t0))
        if res.is_error:
            return ToolResult(False, None, text or "tool error", ms)
        data = res.structured_content if isinstance(res.structured_content, dict) else None
        if data is None:
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text}
        return ToolResult(True, data, None, ms)

    def agent_tool_specs(self) -> list[dict]:
        """Tool definitions for the model: allow-listed AND marked read-only by the server."""
        out = []
        for name, m in sorted(self.tools.items()):
            if (m["server"], name) in AGENT_TOOLS and m["read_only"]:
                schema = json.loads(json.dumps(m["input_schema"]))
                schema.get("properties", {}).pop("identity", None)  # the hub adds it; the model never sees it
                schema["required"] = [r for r in schema.get("required", []) if r != "identity"]
                out.append({"name": name, "description": m["description"], "input_schema": schema})
        return out

    def agent_may_call(self, tool: str) -> bool:
        m = self.tools.get(tool)
        return bool(m and (m["server"], tool) in AGENT_TOOLS and m["read_only"])

    def close(self):
        if self._loop.is_running() and hasattr(self, "_stop"):
            self._loop.call_soon_threadsafe(self._stop.set)
            try:
                self._owner.result(10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
