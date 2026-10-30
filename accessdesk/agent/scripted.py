"""A scripted stand-in for Claude, for the free demo and CI. Not AI: keyword rules.

It speaks the same message format and calls the same MCP tools as Claude, so identity injection,
the policy, approvals, memory and the audit log all run exactly as with the real model.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

PROBLEM = re.compile(r"\b(keeps|not working|broken|error|logging me out|look at it|can't log|crash)", re.I)
NAME = re.compile(r"(?=\b([A-ZÀ-Ý][a-zà-ÿ]+ [A-ZÀ-Ý][a-zà-ÿ]+)\b)")  # overlapping: "Give Beatriz Martins"
CATALOG_WORDS = {"power", "bi", "operations", "salesforce", "production", "database", "slack", "jira", "erp", "aws",
                 "payroll", "fleet", "finance", "microsoft", "confluence", "hi", "please", "give", "can", "the"}


def _call(name: str, args: dict, text: str = ""):
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": name, "input": args}]
    return SimpleNamespace(content=content, stop_reason="tool_use",
                           usage=SimpleNamespace(input_tokens=900, output_tokens=len(json.dumps(args)) // 4))


def _results(msgs: list) -> list[tuple[str, dict, dict]]:
    """(tool name, input, result) for every tool call in the current turn."""
    calls, out = {}, []
    for m in msgs:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if b["type"] == "tool_use":
                    calls[b["id"]] = (b["name"], b["input"])
        elif isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and b["tool_use_id"] in calls:
                    try:
                        data = json.loads(b["content"]) if not b.get("is_error") else {"error": b["content"]}
                    except ValueError:
                        data = {"error": b["content"]}
                    out.append((*calls[b["tool_use_id"]], data))
    return out


def _names(text: str) -> list[str]:
    return [n for n in NAME.findall(text) if not ({w.lower() for w in n.split()} & CATALOG_WORDS)]


class _Messages:
    def create(self, **kw):
        msgs = kw["messages"]
        last_user = next(i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i]["content"], str))
        text = msgs[last_user]["content"]
        low = text.lower()
        done = _results(msgs[last_user:])
        got = {name: data for name, _, data in done}
        asked = {(name, json.dumps(a, sort_keys=True)) for name, a, _ in done}

        def once(name: str, args: dict, say: str = ""):
            return None if (name, json.dumps(args, sort_keys=True)) in asked else _call(name, args, say)

        if re.search(r"\b(status|ticket)\b", low):
            if "my_tickets" not in got:
                return _call("my_tickets", {})
            tickets = got["my_tickets"].get("tickets", [])
            if tickets and "get_ticket" not in got:
                return _call("get_ticket", {"ticket_id": tickets[0]["ticket_id"]})
            t = got.get("get_ticket", {})
            if not t or "error" in t:
                return _respond("I couldn't find an open ticket for you.")
            last = t["comments"][-1] if t.get("comments") else None
            note = ""
            if last and re.search(r"(?i)note to the ai|ignore your|grant", last["body"]):
                note = (" The latest comment also contains instructions addressed to me; I don't act on those, "
                        "and I've flagged it for the service desk.")
            return _respond(f"Ticket {t['ticket_id']} ('{t['summary']}') is {t['status']}. Latest update from "
                            f"{last['author'] if last else 'nobody'} on {(last or t)['created_at'][:10]}.{note}")
        if re.search(r"what access|access do i have|my access", low):
            if "get_access" not in got:
                return _call("get_access", {})
            acc = got["get_access"].get("access", [])
            return _respond("You currently have: " + ", ".join(a["name"] for a in acc) + ".")
        if "mfa" in low or "authenticator" in low:
            if "preview_request" not in got:
                return _call("preview_request", {"action": "reset_mfa"})
            return _respond(_say(got["preview_request"], "An MFA reset"),
                            {"action": "reset_mfa", "justification": text[:200]})
        target = None
        for n in _names(text):
            step = once("find_employee", {"query": n})
            if step:
                return step
        for name, a, data in done:
            if name == "find_employee" and data.get("matches"):
                target = data["matches"][0]["employee_id"]
                break
        if PROBLEM.search(text) and not re.search(r"\b(access|need|give|grant)\b", low):
            if "preview_request" not in got:
                return _call("preview_request", {"action": "escalate"})
            return _respond("That sounds like a technical problem rather than an access request. Confirm below and "
                            "I'll open a ticket for the service desk team.", {"action": "escalate", "justification": text[:200]})
        if "search_entitlements" not in got:
            return _call("search_entitlements", {"query": text})
        matches = got["search_entitlements"].get("matches", [])
        if not matches:
            return _respond("I can only request one specific app or permission at a time, and I couldn't find a "
                            "specific one in your message. Which application do you need?")
        ent = matches[0]["entitlement_id"]
        days = int(m.group(1)) if (m := re.search(r"(\d+)\s*days", low)) else None
        args = {"action": "grant", "entitlement_id": ent, "for_employee_id": target, "duration_days": days}
        if "preview_request" not in got:
            return _call("preview_request", args)
        prev = got["preview_request"]
        reply = _say(prev, matches[0]["name"])
        if re.search(r"\b(cto|ceo|i'?m (actually )?the)\b", low):
            reply += " Your access is based on your login, so a role mentioned in chat doesn't change the outcome."
        remember = []
        if "month-end" in low:
            remember.append("Works on month-end accruals.")
        if "just started" in low:
            remember.append("Joined the company in October 2026.")
        refused = prev.get("outcome", "").startswith("refused") or prev.get("outcome") == "already_has"
        proposal = None if refused else {**args, "justification": text[:200]}
        return _respond(reply, proposal, remember)


def _say(prev: dict, what: str) -> str:
    o = prev.get("outcome", "")
    people: dict[str, list[str]] = {}
    for a in prev.get("approvers", []):
        people.setdefault(a.get("name") or "?", []).append(a["kind"])
    who = " and ".join(f"{n} ({' and '.join(k)})" for n, k in people.items())
    if o == "granted":
        return f"{what} can be set up right away ({prev['reason'].rstrip('.').lower()}). Confirm below and it's done."
    if o in ("manager_approval", "owner_approval", "manager_and_owner"):
        extra = f" Access expires after {prev['duration_days']} days." if prev.get("duration_days") else ""
        return f"{what} needs approval from {who}. Confirm below and I'll send the request.{extra}"
    if o == "already_has":
        return f"You already have {what}."
    if o == "escalated":
        return prev["reason"]
    return f"I can't request {what}: {prev.get('reason', 'not allowed')}"


def _respond(reply: str, proposal: dict | None = None, remember: list | None = None):
    return _call("respond", {"reply": reply, "proposal": proposal, "remember": remember or []})


class ScriptedAnthropic:
    def __init__(self):
        self.messages = _Messages()
