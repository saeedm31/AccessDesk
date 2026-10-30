"""Access policy. Plain code over facts from the company database: the same request always gets the same path.

Order of checks (first match wins):
  already has it -> segregation-of-duties conflict -> role not eligible -> asking for someone who is not
  you or your direct report -> risk-based path (standard for the role: granted; medium or non-standard:
  manager; high: manager + app owner, max 30 days). A manager asking for a direct report counts as the
  manager's approval.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field

from .config import HIGH_RISK_MAX_DAYS

REFUSED = {"refused_sod", "refused_not_eligible", "refused_not_allowed", "refused_unclear", "refused_inactive"}


@dataclass
class Outcome:
    outcome: str  # granted | manager_approval | owner_approval | manager_and_owner | already_has | refused_* | info | escalated
    reason: str
    approvers: list[dict] = field(default_factory=list)  # [{"kind": "manager", "employee_id": "E003", "name": ...}]
    duration_days: int | None = None
    entitlement: dict | None = None
    target: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _one(con, sql, *args):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def evaluate(con: sqlite3.Connection, requester_id: str, action: str, entitlement_id: str | None = None,
             for_employee_id: str | None = None, duration_days: int | None = None) -> Outcome:
    con.row_factory = sqlite3.Row
    me = _one(con, "SELECT * FROM employees WHERE employee_id=?", requester_id)
    target_id = for_employee_id or requester_id
    target = _one(con, "SELECT * FROM employees WHERE employee_id=?", target_id)
    if action == "info":
        return Outcome("info", "Information only, nothing changes.")
    if action == "escalate":
        return Outcome("escalated", "Not an access request: a ticket goes to the IT service desk team.")
    if not me or not target or target["status"] != "active":
        return Outcome("refused_inactive", "Unknown or inactive employee.")
    is_self = target_id == requester_id
    is_manager = target["manager_id"] == requester_id
    if not (is_self or is_manager):
        return Outcome("refused_not_allowed", "You can only request access for yourself or for your direct reports.",
                       target=target)
    mgr = _one(con, "SELECT employee_id, name FROM employees WHERE employee_id=?", target["manager_id"] or "")
    if action == "reset_mfa":
        if is_manager:
            return Outcome("granted", "Requested by the manager, who confirms the person's identity.", target=target)
        return Outcome("manager_approval", "MFA resets need the manager to confirm it is really you (protects "
                       "against phone-based impersonation).", [{"kind": "manager", **(mgr or {})}], target=target)
    if action != "grant" or not entitlement_id:
        return Outcome("refused_unclear", "No single, specific application was requested.")
    ent = _one(con, "SELECT * FROM entitlements WHERE entitlement_id=?", entitlement_id)
    if not ent:
        return Outcome("refused_unclear", f"Unknown entitlement {entitlement_id}.")
    has = {r[0] for r in con.execute("SELECT entitlement_id FROM grants WHERE employee_id=? AND status='active'", (target_id,))}
    if entitlement_id in has:
        return Outcome("already_has", f"{target['name']} already has {ent['name']}.", entitlement=ent, target=target)
    for a, b, why in con.execute("SELECT a, b, reason FROM sod_rules"):
        other = b if a == entitlement_id else a if b == entitlement_id else None
        if other and other in has:
            return Outcome("refused_sod", f"Segregation of duties: {why} must be done by different people, and "
                           f"{target['name']} already has {other}.", entitlement=ent, target=target)
    eligible = (ent["eligible_roles"] or "").split()
    if eligible and target["role"] not in eligible:
        return Outcome("refused_not_eligible", f"{ent['name']} is limited to these roles: {', '.join(eligible)}. "
                       f"{target['name']} is {target['role']}.", entitlement=ent, target=target)
    standard = ent["standard_roles"] == "*" or target["role"] in (ent["standard_roles"] or "").split()
    owner = _one(con, "SELECT employee_id, name FROM employees WHERE role=? ORDER BY start_date LIMIT 1", ent["owner_role"])
    days = None
    if ent["risk"] == "high":
        days = min(duration_days or HIGH_RISK_MAX_DAYS, HIGH_RISK_MAX_DAYS)
        need = ([] if is_manager else [{"kind": "manager", **(mgr or {})}]) + [{"kind": "owner", **(owner or {})}]
        return Outcome("owner_approval" if is_manager else "manager_and_owner",
                       f"High-risk access needs the manager and the app owner, and expires after {days} days"
                       + (f" (asked for {duration_days})." if duration_days and duration_days > days else "."),
                       need, days, ent, target)
    if standard or is_manager:
        return Outcome("granted", "Standard access for this role." if standard else "Requested by the manager.",
                       duration_days=None, entitlement=ent, target=target)
    return Outcome("manager_approval", f"{ent['name']} is not standard for {target['role']}: the manager approves.",
                   [{"kind": "manager", **(mgr or {})}], None, ent, target)
