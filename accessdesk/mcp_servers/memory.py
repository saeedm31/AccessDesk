"""MCP server: long-term memory per employee.

The model can read the caller's memories. Writing happens only after the employee confirms a
suggested memory in the app, and employees can see and delete everything stored about them.
Memories are notes for context, never permissions: the policy ignores them.
"""

from __future__ import annotations

import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..security import scan
from .common import READ, WRITE, db, now, rows, serve, who

server = MCPServer("accessdesk-memory", instructions="What the assistant remembers about the caller, with consent.")


@server.tool(annotations=READ)
def recall(identity: str) -> dict:
    """Everything remembered about the caller, newest first."""
    me = who(identity)
    with db() as con:
        m = rows(con.execute("SELECT memory_id, text, source, created_at FROM memories WHERE employee_id=? ORDER BY created_at DESC LIMIT 20",
                             (me["employee_id"],)))
    return {"memories": m}


@server.tool(annotations=WRITE)
def save_memory(identity: str, text: str) -> dict:
    """Store a note the caller confirmed. Rejects instructions and secrets."""
    me = who(identity)
    text = " ".join(text.split())[:300]
    low = text.lower()
    if scan(text) or any(w in low for w in ("password", "token", "approved", "admin rights", "permission to")):
        raise ToolError("Not stored: memories are context only, not instructions or permissions.")
    mid = "MEM-" + uuid.uuid4().hex[:6].upper()
    with db() as con:
        con.execute("INSERT INTO memories VALUES (?,?,?,?,?)", (mid, me["employee_id"], text, "confirmed by employee", now()))
    return {"memory_id": mid, "text": text}


@server.tool(annotations=WRITE)
def forget(identity: str, memory_id: str | None = None) -> dict:
    """Delete one memory, or all of the caller's memories when memory_id is empty (right to erasure)."""
    me = who(identity)
    with db() as con:
        if memory_id:
            n = con.execute("DELETE FROM memories WHERE employee_id=? AND memory_id=?", (me["employee_id"], memory_id)).rowcount
        else:
            n = con.execute("DELETE FROM memories WHERE employee_id=?", (me["employee_id"],)).rowcount
    return {"deleted": n}


if __name__ == "__main__":
    serve(server)
