"""MCP server: the app catalog, segregation-of-duties rules, reviewed team patterns, and a policy preview."""

from __future__ import annotations

import re

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import policy
from .common import READ, db, rows, serve, who

server = MCPServer("accessdesk-catalog", instructions="What can be requested, how risky it is, and who approves.")
STOP = set("a an the i me my to of for and or in on at can could please need want access get give have with".split())


@server.tool(annotations=READ)
def search_entitlements(query: str) -> dict:
    """Find requestable apps and permissions by name or keywords. Best matches first."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOP]
    with db() as con:
        ents = rows(con.execute("SELECT entitlement_id, name, risk, owner_role, eligible_roles, aliases FROM entitlements"))
    scored = []
    for e in ents:
        hay = set(re.findall(r"[a-z0-9]+", (e["name"] + " " + e["aliases"] + " " + e["entitlement_id"]).lower()))
        score = sum(w in hay for w in words)
        if "admin" in hay and "admin" not in words:
            score -= 1  # never suggest admin rights unless asked for
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    out = [{k: v for k, v in e.items() if k != "aliases"} | {"match_score": s} for s, e in scored[:5]]
    return {"matches": out}


@server.tool(annotations=READ)
def team_patterns(role: str) -> dict:
    """Apps people in a role usually have. Only patterns an IT admin has reviewed and approved."""
    with db() as con:
        p = rows(con.execute("SELECT t.entitlement_id, e.name, t.share FROM team_patterns t JOIN entitlements e "
                             "USING(entitlement_id) WHERE t.role=? AND t.status='approved'", (role,)))
    return {"role": role, "reviewed_patterns": p}


@server.tool(annotations=READ)
def preview_request(identity: str, action: str, entitlement_id: str | None = None, for_employee_id: str | None = None,
                    duration_days: int | None = None) -> dict:
    """Dry run of the access policy: what would happen to this request and who would approve. Changes nothing.
    action: grant | reset_mfa | info | escalate."""
    me = who(identity)
    if action not in ("grant", "reset_mfa", "info", "escalate"):
        raise ToolError("action must be grant, reset_mfa, info or escalate")
    with db() as con:
        o = policy.evaluate(con, me["employee_id"], action, entitlement_id, for_employee_id, duration_days)
    d = o.to_dict()
    d["entitlement"] = {k: d["entitlement"][k] for k in ("entitlement_id", "name", "risk")} if d["entitlement"] else None
    d["target"] = {k: d["target"][k] for k in ("employee_id", "name", "role")} if d["target"] else None
    return d


if __name__ == "__main__":
    serve(server)
