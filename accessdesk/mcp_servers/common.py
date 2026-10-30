"""Shared bits for the MCP servers: database, identity check, annotations, transport."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..config import ROOT, secret
from ..security import verify

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
PRIVILEGED = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def data_dir() -> Path:
    return Path(os.getenv("AD_DATA_DIR", ROOT / "data"))


def db() -> sqlite3.Connection:
    con = sqlite3.connect(data_dir() / "company.db", timeout=10)
    con.row_factory = sqlite3.Row
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def who(identity: str) -> dict:
    """The verified caller. The identity token comes from the login, added by the client, never by the model."""
    ok, reason, claims = verify(secret(data_dir(), "identity"), identity, "identity")
    if not ok:
        raise ToolError(f"not authenticated: {reason}")
    with db() as con:
        me = con.execute("SELECT * FROM employees WHERE employee_id=?", (claims["sub"],)).fetchone()
    if not me or me["status"] != "active":
        raise ToolError("not authenticated: inactive account")
    return dict(me)


def serve(server) -> None:
    if "--http" in sys.argv:
        from mcp.server.transport_security import TransportSecuritySettings

        port = int(sys.argv[sys.argv.index("--http") + 1])
        hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*").split(",") if h.strip()]
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)
        server.run("streamable-http", host="0.0.0.0", port=port, transport_security=sec)
    else:
        server.run()
