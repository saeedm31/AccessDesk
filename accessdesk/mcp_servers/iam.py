"""MCP server: identity and access management. The only place access changes.

Every change needs a grant token (signed by the workflow after the policy or the approvers said yes),
and the server re-checks the policy facts itself: segregation of duties and role eligibility.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..config import secret
from ..security import verify
from .common import PRIVILEGED, data_dir, db, now, serve

server = MCPServer("accessdesk-iam", instructions="Grants access and resets MFA. Needs a signed grant token.")


def _check(token: str, ticket_id: str, action: str) -> dict:
    ok, reason, c = verify(secret(data_dir(), "grant"), token, "grant")
    if not ok:
        raise ToolError(f"refused: {reason}")
    if c["tck"] != ticket_id or c["act"] != action:
        raise ToolError("refused: token is for another ticket or action")
    with db() as con:
        if con.execute("SELECT 1 FROM grants WHERE token_id=?", (c["jti"],)).fetchone():
            raise ToolError("refused: token already used")
    return c


@server.tool(annotations=PRIVILEGED)
def grant_access(ticket_id: str, grant_token: str) -> dict:
    """Give the employee on the ticket the entitlement on the ticket, with expiry for high-risk access."""
    c = _check(grant_token, ticket_id, "grant")
    with db() as con:
        t = con.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if not t or t["for_employee_id"] != c["emp"] or t["entitlement_id"] != c["ent"]:
            raise ToolError("refused: ticket does not match the token")
        emp = con.execute("SELECT role, status FROM employees WHERE employee_id=?", (c["emp"],)).fetchone()
        ent = con.execute("SELECT * FROM entitlements WHERE entitlement_id=?", (c["ent"],)).fetchone()
        has = {r[0] for r in con.execute("SELECT entitlement_id FROM grants WHERE employee_id=? AND status='active'", (c["emp"],))}
        for a, b, why in con.execute("SELECT a, b, reason FROM sod_rules"):  # defence in depth
            if (c["ent"] == a and b in has) or (c["ent"] == b and a in has):
                raise ToolError(f"refused: segregation of duties ({why})")
        eligible = (ent["eligible_roles"] or "").split()
        if emp["status"] != "active" or (eligible and emp["role"] not in eligible):
            raise ToolError("refused: employee not eligible")
        expires = (datetime.now(UTC) + timedelta(days=c["days"])).date().isoformat() if c["days"] else None
        con.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,?,?)",
                    (c["emp"], c["ent"], now(), expires, ",".join(c["by"]), ticket_id, c["jti"], "active"))
        con.execute("UPDATE tickets SET status='done', updated_at=? WHERE ticket_id=?", (now(), ticket_id))
    return {"ticket_id": ticket_id, "employee_id": c["emp"], "entitlement_id": c["ent"], "expires": expires,
            "approved_by": c["by"]}


@server.tool(annotations=PRIVILEGED)
def reset_mfa(ticket_id: str, grant_token: str) -> dict:
    """Reset MFA and send an enrolment link to the employee's company email."""
    c = _check(grant_token, ticket_id, "reset_mfa")
    with db() as con:
        con.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,?,?)", (c["emp"], "MFA-RESET", now(), now(), ",".join(c["by"]),
                                                                    ticket_id, c["jti"], "event"))
        con.execute("UPDATE tickets SET status='done', updated_at=? WHERE ticket_id=?", (now(), ticket_id))
    return {"ticket_id": ticket_id, "employee_id": c["emp"], "result": "MFA reset; enrolment link sent to company email"}


if __name__ == "__main__":
    serve(server)
