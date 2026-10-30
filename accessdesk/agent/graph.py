"""One chat turn as a LangGraph state machine:

    agent (LLM) <-> tools (read-only MCP, identity added by the hub) -> policy (code) -> END

The model answers and may propose one request. Code then evaluates the proposal with the access policy
(through the catalog server's preview, which reads the company database) and the app shows the result
as a card the employee confirms. Nothing changes access during the turn itself.

Memory: short-term = the last turns of this chat plus a code-written summary of earlier requests;
long-term = notes the employee confirmed in earlier chats (memory server), passed in as context only.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date
from typing import Any, TypedDict

from ..audit import AuditLog
from ..config import MAX_AGENT_STEPS
from ..llm import MeteredClient, total
from ..mcp_hub import MCPHub
from ..security import scan
from . import prompts

try:
    from langgraph.graph import END, StateGraph

    HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover
    END, HAS_LANGGRAPH = "__end__", False


class State(TypedDict, total=False):
    turn_id: str
    messages: list
    pending: bool
    steps: int
    tool_calls: list
    flags: list
    response: dict | None
    outcome: dict | None


def _block(b: Any) -> dict:
    if isinstance(b, dict):
        return b
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": b.type}


class AccessAgent:
    def __init__(self, llm: MeteredClient, hub: MCPHub, audit: AuditLog, model: str, employee_id: str, identity: str):
        self.llm, self.hub, self.audit, self.model = llm, hub, audit, model
        self.employee_id, self.identity = employee_id, identity  # identity token: passed to the hub, never to the model
        self._system = ""
        self.graph = self._compile() if HAS_LANGGRAPH else None

    def _log(self, state: State, kind: str, payload: dict) -> None:
        self.audit.write(kind, payload, run_id=state.get("turn_id"), actor=self.employee_id)

    def system_prompt(self, summary: str) -> str:
        me = self.hub.call("whoami", {}, self.identity).data or {}
        mem = self.hub.call("recall", {}, self.identity).data or {"memories": []}
        notes = "\n".join(f"- {m['text']}" for m in mem["memories"]) or "(none)"
        p, mgr = me.get("me", {}), me.get("manager") or {}
        return prompts.SYSTEM.format(name=p.get("name"), title=p.get("title"), department=p.get("department"),
                                     employee_id=p.get("employee_id"), manager=mgr.get("name", "none"),
                                     today=date.today().isoformat(), memories=notes, summary=summary or "nothing yet")

    # ---------- nodes ----------
    def agent(self, state: State) -> State:
        kw: dict = {}
        if state.get("steps", 0) >= MAX_AGENT_STEPS:
            kw["tool_choice"] = {"type": "tool", "name": "respond"}
        mark, t0 = len(self.llm.meter.calls), time.time()
        resp = self.llm.create(step="agent", model=self.model, max_tokens=1200, system=self._system,
                               messages=state["messages"], tools=self.hub.agent_tool_specs() + [prompts.RESPOND], **kw)
        content = [_block(b) for b in resp.content]
        self._log(state, "llm_call", dict(model=self.model, tools=[b["name"] for b in content if b["type"] == "tool_use"],
                                          usage=[u.to_dict() for u in self.llm.meter.since(mark)], ms=int(1000 * (time.time() - t0))))
        messages = state["messages"] + [{"role": "assistant", "content": content}]
        final = next((b for b in content if b["type"] == "tool_use" and b["name"] == "respond"), None)
        if final:
            return {"messages": messages, "pending": False, "response": dict(final["input"])}
        if not any(b["type"] == "tool_use" for b in content):  # plain text answer
            text = " ".join(b.get("text", "") for b in content if b["type"] == "text").strip()
            return {"messages": messages, "pending": False, "response": {"reply": text, "proposal": None, "remember": []}}
        return {"messages": messages, "pending": True}

    def tools(self, state: State) -> State:
        last = state["messages"][-1]["content"]
        results, calls, flags = [], list(state.get("tool_calls", [])), list(state.get("flags", []))
        for b in (x for x in last if x["type"] == "tool_use"):
            if b["name"] == "respond":
                continue
            if not self.hub.agent_may_call(b["name"]):
                self._log(state, "tool_blocked", dict(tool=b["name"], args=b.get("input")))
                results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                "content": f"Tool {b['name']} is not available to you."})
                continue
            r = self.hub.call(b["name"], b.get("input") or {}, self.identity)
            body = json.dumps(r.data, default=str, ensure_ascii=False)[:10_000] if r.ok else (r.error or "error")
            hits = scan(body) if r.ok else []
            if hits:
                flags += [{"tool": b["name"], **h} for h in hits]
                self._log(state, "security_flag", dict(tool=b["name"], hits=hits))
            self._log(state, "tool_call", dict(by="model", tool=b["name"], args=b.get("input"), ok=r.ok, ms=r.ms,
                                               error=r.error, result=body[:500]))
            calls.append(dict(name=b["name"], input=b.get("input") or {}, ok=r.ok, ms=r.ms,
                              outcome=(r.data or {}).get("outcome") if b["name"] == "preview_request" else None))
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": body, "is_error": not r.ok})
        return {"messages": state["messages"] + [{"role": "user", "content": results}], "tool_calls": calls,
                "flags": flags, "steps": state.get("steps", 0) + 1, "pending": False}

    def policy(self, state: State) -> State:
        """Code re-evaluates the proposal with the policy; the model's own preview is not trusted."""
        p = (state.get("response") or {}).get("proposal")
        if not p or not isinstance(p, dict) or p.get("action") not in ("grant", "reset_mfa", "escalate"):
            return {"outcome": None}
        r = self.hub.call("preview_request", {k: p.get(k) for k in ("action", "entitlement_id", "for_employee_id", "duration_days")},
                          self.identity)
        out = r.data if r.ok else {"outcome": "refused_unclear", "reason": r.error, "approvers": []}
        self._log(state, "policy", dict(proposal=p, outcome=out.get("outcome"), approvers=out.get("approvers")))
        return {"outcome": out}

    # ---------- wiring ----------
    @staticmethod
    def _after_agent(state: State) -> str:
        return "tools" if state.get("pending") else "policy"

    def _compile(self):
        g = StateGraph(State)
        g.add_node("agent", self.agent)
        g.add_node("tools", self.tools)
        g.add_node("policy", self.policy)
        g.set_entry_point("agent")
        g.add_conditional_edges("agent", self._after_agent, {"tools": "tools", "policy": "policy"})
        g.add_edge("tools", "agent")
        g.add_edge("policy", END)
        return g.compile()

    def _run_plain(self, state: State) -> State:
        node = "agent"
        for _ in range(3 * MAX_AGENT_STEPS + 6):
            state = {**state, **getattr(self, node)(state)}
            node = {"agent": self._after_agent, "tools": lambda s: "agent", "policy": lambda s: END}[node](state)
            if node == END:
                return state
        raise RuntimeError("turn did not finish")

    def turn(self, history: list[dict], message: str, summary: str = "") -> dict:
        """history: earlier turns as plain {role, content} text messages (short-term memory)."""
        turn_id = "TURN-" + uuid.uuid4().hex[:8].upper()
        t0, mark = time.time(), len(self.llm.meter.calls)
        self._system = self.system_prompt(summary)
        state: State = {"turn_id": turn_id, "messages": history + [{"role": "user", "content": message}], "steps": 0,
                        "tool_calls": [], "flags": []}
        self._log(state, "turn_start", dict(message=message, model=self.model, engine="langgraph" if self.graph else "plain"))
        state = self.graph.invoke(state, config={"recursion_limit": 3 * MAX_AGENT_STEPS + 8}) if self.graph else self._run_plain(state)
        resp = state.get("response") or {"reply": "", "proposal": None, "remember": []}
        cost = total(self.llm.meter.since(mark))
        out = dict(turn_id=turn_id, reply=resp.get("reply", ""), proposal=resp.get("proposal"), remember=resp.get("remember") or [],
                   outcome=state.get("outcome"), tool_calls=state.get("tool_calls", []), flags=state.get("flags", []),
                   cost=cost, seconds=round(time.time() - t0, 2), steps=state.get("steps", 0))
        self._log(state, "turn_end", dict(reply=out["reply"][:500], outcome=(out["outcome"] or {}).get("outcome"),
                                          cost_eur=cost["eur"], seconds=out["seconds"]))
        return out
