"""MCP server: the employee directory (read only). Answers depend on who is asking."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .common import READ, db, rows, serve, who

server = MCPServer("accessdesk-directory", instructions="Employee directory and current access. Read only.")
PUBLIC = "employee_id, name, title, department, role"


@server.tool(annotations=READ)
def whoami(identity: str) -> dict:
    """The logged-in employee: profile, manager and direct reports."""
    me = who(identity)
    with db() as con:
        mgr = con.execute(f"SELECT {PUBLIC} FROM employees WHERE employee_id=?", (me["manager_id"] or "",)).fetchone()
        reports = rows(con.execute(f"SELECT {PUBLIC} FROM employees WHERE manager_id=? AND status='active'", (me["employee_id"],)))
    return {"me": {k: me[k] for k in ("employee_id", "name", "title", "department", "role", "start_date")},
            "manager": dict(mgr) if mgr else None, "direct_reports": reports}


@server.tool(annotations=READ)
def find_employee(identity: str, query: str) -> dict:
    """Find colleagues by name (address-book fields only: id, name, title, department, role)."""
    who(identity)
    with db() as con:
        hits = rows(con.execute(f"SELECT {PUBLIC} FROM employees WHERE name LIKE ? AND status='active' LIMIT 5", (f"%{query.strip()}%",)))
    return {"matches": hits}


@server.tool(annotations=READ)
def get_access(identity: str, employee_id: str | None = None) -> dict:
    """Current access of the caller, or of one of the caller's direct reports."""
    me = who(identity)
    target = employee_id or me["employee_id"]
    with db() as con:
        t = con.execute("SELECT employee_id, name, manager_id FROM employees WHERE employee_id=?", (target,)).fetchone()
        if not t or (target != me["employee_id"] and t["manager_id"] != me["employee_id"]):
            raise ToolError("You can only see your own access or your direct reports' access.")
        g = rows(con.execute(
            "SELECT g.entitlement_id, e.name, e.risk, g.granted_at, g.expires_at FROM grants g JOIN entitlements e "
            "USING(entitlement_id) WHERE g.employee_id=? AND g.status='active' ORDER BY e.risk DESC, e.name", (target,)))
    return {"employee_id": target, "name": t["name"], "access": g}


if __name__ == "__main__":
    serve(server)
