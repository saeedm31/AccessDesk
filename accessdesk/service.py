"""The workflow around the agent: confirming a request, approvals, grants, memory and team patterns.

A request is acted on only after the employee confirms it. Code evaluates the policy again at that
moment, opens the ticket, and either grants at once (standard access) or asks the approvers. When the
last approver says yes, code mints a grant token for exactly that ticket; the IAM server checks it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .agent.graph import AccessAgent
from .agent.scripted import ScriptedAnthropic
from .audit import AuditLog
from .config import TOKEN_TTL_S, Settings, secret
from .llm import Meter, MeteredClient, make_client
from .mcp_hub import MCPHub
from .security import grant_token, identity_token


class Service:
    def __init__(self, settings: Settings, hub: MCPHub):
        self.s, self.hub = settings, hub
        self.audit = AuditLog(settings.audit_db)

    def _con(self):
        con = sqlite3.connect(self.s.db, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def identity(self, employee_id: str) -> str:
        """What SSO would hand over after login. In the demo the sidebar picks who is logged in."""
        return identity_token(secret(self.s.data_dir, "identity"), employee_id)

    def employees(self) -> list[dict]:
        with self._con() as con:
            return [dict(r) for r in con.execute("SELECT * FROM employees ORDER BY employee_id")]

    def agent(self, employee_id: str, mode: str = "scripted", api_key: str | None = None, model: str | None = None,
              meter: Meter | None = None) -> AccessAgent:
        if mode == "scripted":
            llm, model = MeteredClient(ScriptedAnthropic(), meter), "scripted"
        else:
            llm = make_client(self.s.llm_provider, api_key=api_key, region=self.s.aws_region, meter=meter)
            model = model or self.s.agent_model
        return AccessAgent(llm, self.hub, self.audit, model, employee_id, self.identity(employee_id))

    def chat(self, employee_id: str, message: str, history: list | None = None, summary: str = "", **kw) -> dict:
        return self.agent(employee_id, **kw).turn(history or [], message, summary)

    def submit(self, employee_id: str, proposal: dict) -> dict:
        """The employee confirmed the proposal. Policy again, then ticket, then grant or approvals."""
        me = self.identity(employee_id)
        args = {k: proposal.get(k) for k in ("action", "entitlement_id", "for_employee_id", "duration_days")}
        prev = self.hub.call("preview_request", args, me)
        if not prev.ok:
            return {"ok": False, "outcome": "refused_unclear", "message": prev.error}
        o = prev.data
        self.audit.write("submit", dict(proposal=proposal, outcome=o["outcome"]), actor=employee_id)
        if o["outcome"].startswith("refused") or o["outcome"] in ("already_has", "info"):
            return {"ok": False, "outcome": o["outcome"], "message": o["reason"]}
        kind = {"grant": "access_request", "reset_mfa": "mfa_reset", "escalate": "incident"}[args["action"]]
        ent = (o.get("entitlement") or {}).get("name") or ""
        target = (o.get("target") or {}).get("employee_id") or employee_id
        t = self.hub.call("create_ticket", dict(
            kind=kind, summary=f"{kind.replace('_', ' ')}: {ent}".strip(": "), for_employee_id=target,
            entitlement_id=args.get("entitlement_id"), justification=proposal.get("justification", ""),
            duration_days=o.get("duration_days"), path=o["outcome"],
            status="open" if o["outcome"] == "escalated" else "in_progress"), me)
        if not t.ok:
            return {"ok": False, "outcome": "error", "message": t.error}
        tid = t.data["ticket_id"]
        if o["outcome"] == "escalated":
            return {"ok": True, "outcome": "escalated", "ticket_id": tid, "message": f"Ticket {tid} is with the service desk."}
        if o["outcome"] == "granted":
            return self._execute(tid, target, args["action"], args.get("entitlement_id"), o.get("duration_days"),
                                 [f"policy:{o['outcome']}", f"requested:{employee_id}"])
        self.hub.call("request_approvals", {"ticket_id": tid, "approvers": o["approvers"]})
        names = ", ".join(sorted({a["name"] for a in o["approvers"]}))
        return {"ok": True, "outcome": o["outcome"], "ticket_id": tid, "message": f"Ticket {tid} is waiting for {names}."}

    def _execute(self, ticket_id: str, employee_id: str, action: str, entitlement_id: str | None, days: int | None,
                 approvers: list[str]) -> dict:
        tok = grant_token(secret(self.s.data_dir, "grant"), ticket_id, employee_id, action, entitlement_id, days,
                          approvers, TOKEN_TTL_S)
        tool = "grant_access" if action == "grant" else "reset_mfa"
        r = self.hub.call(tool, {"ticket_id": ticket_id, "grant_token": tok})
        self.audit.write("access_change", dict(ticket=ticket_id, tool=tool, ok=r.ok, result=r.data, error=r.error,
                                               approvers=approvers), actor="workflow")
        if not r.ok:
            return {"ok": False, "outcome": "error", "ticket_id": ticket_id, "message": r.error}
        exp = f", expires {r.data['expires']}" if r.data.get("expires") else ""
        return {"ok": True, "outcome": "granted", "ticket_id": ticket_id, "message": f"Done: ticket {ticket_id}{exp}.",
                "result": r.data}

    def pending(self, approver_id: str) -> list[dict]:
        r = self.hub.call("pending_approvals", {}, self.identity(approver_id))
        return r.data["pending"] if r.ok else []

    def decide(self, approver_id: str, ticket_id: str, decision: str, comment: str = "") -> dict:
        r = self.hub.call("decide", {"ticket_id": ticket_id, "decision": decision, "comment": comment}, self.identity(approver_id))
        self.audit.write("approval", dict(ticket=ticket_id, decision=decision, ok=r.ok, error=r.error), actor=approver_id)
        if not r.ok:
            return {"ok": False, "message": r.error}
        if r.data["rejected"]:
            return {"ok": True, "message": f"Ticket {ticket_id} rejected."}
        if not r.data["all_approved"]:
            return {"ok": True, "message": "Approved. Waiting for the other approver."}
        with self._con() as con:
            t = dict(con.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone())
        action = "reset_mfa" if t["kind"] == "mfa_reset" else "grant"
        return self._execute(ticket_id, t["for_employee_id"], action, t["entitlement_id"], t["duration_days"],
                             [f"human:{a}" for a in r.data["approved_by"]])

    def remember(self, employee_id: str, text: str) -> dict:
        r = self.hub.call("save_memory", {"text": text}, self.identity(employee_id))
        return {"ok": r.ok, "message": r.error if not r.ok else "Saved."}

    def memories(self, employee_id: str) -> list[dict]:
        r = self.hub.call("recall", {}, self.identity(employee_id))
        return r.data["memories"] if r.ok else []

    def forget(self, employee_id: str, memory_id: str | None = None) -> dict:
        r = self.hub.call("forget", {"memory_id": memory_id}, self.identity(employee_id))
        self.audit.write("forget", dict(memory_id=memory_id or "all", ok=r.ok), actor=employee_id)
        return r.data or {}

    def patterns(self) -> list[dict]:
        with self._con() as con:
            return [dict(r) for r in con.execute(
                "SELECT t.role, t.entitlement_id, e.name, e.risk, t.share, t.status, t.reviewed_by FROM team_patterns t "
                "JOIN entitlements e USING(entitlement_id) ORDER BY t.role, e.risk")]

    def review_pattern(self, admin_id: str, role: str, entitlement_id: str, approve: bool) -> dict:
        with self._con() as con:
            r = con.execute("SELECT role FROM employees WHERE employee_id=?", (admin_id,)).fetchone()
            if not r or r["role"] not in ("it_manager", "it_engineer"):
                return {"ok": False, "message": "Only IT admins review team patterns."}
            con.execute("UPDATE team_patterns SET status=?, reviewed_by=? WHERE role=? AND entitlement_id=?",
                        ("approved" if approve else "rejected", admin_id, role, entitlement_id))
        self.audit.write("pattern_review", dict(role=role, entitlement=entitlement_id, approve=approve), actor=admin_id)
        return {"ok": True}

    def reset(self) -> None:
        from .simulate import build

        build(self.s.data_dir)
        self.audit = AuditLog(self.s.audit_db)


def open_service(data_dir: Path | None = None) -> Service:
    from .simulate import ensure_built

    s = Settings() if data_dir is None else Settings(data_dir=Path(data_dir))
    ensure_built(s.data_dir)
    return Service(s, MCPHub(s.data_dir))
