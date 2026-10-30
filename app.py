"""AccessDesk: an IT service-desk agent that grants access the way security would want it:
least privilege, the employee's own identity, approvals in code, memory with consent."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from accessdesk import config as C
from accessdesk.llm import AGENT_MODELS, PRICES_CHECKED, Meter, eur
from accessdesk.service import open_service

st.set_page_config(page_title="AccessDesk", page_icon="🔑", layout="wide")
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] { font-family: 'IBM Plex Sans', sans-serif; }
h1, h2, h3, [data-testid="stMetricValue"] { font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.01em; }
[data-testid="stAppViewContainer"] {
  background: radial-gradient(1100px 520px at 88% -8%, #1B3358 0%, rgba(11,20,36,0) 60%),
              linear-gradient(135deg, #0B1424 0%, #13233D 100%);
}
[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 2.2rem; max-width: 1400px; }
#MainMenu, footer { visibility: hidden; }
[data-testid="stSidebar"] { border-right: 1px solid #2E4566; }
[data-testid="stMetric"] { background: rgba(22,37,61,0.85); border: 1px solid #2E4566; border-radius: 14px; padding: 14px 18px; }
[data-testid="stMetricLabel"] p { color: #8FA3BF; font-size: 0.9rem; }
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid #2E4566; }
.stTabs [data-baseweb="tab"] { padding: 10px 16px; border-radius: 10px 10px 0 0; color: #B8C2D1; }
.stTabs [aria-selected="true"] { background: #16253D; color: #F3F1EA; }
.stTabs [data-baseweb="tab-highlight"] { background-color: #F2A65A; }
[data-testid="stDataFrame"] { border: 1px solid #2E4566; border-radius: 12px; overflow: hidden; }
.ad-kicker { color: #F2A65A; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; font-size: 0.8rem; }
.ad-title { font-family: 'Space Grotesk', sans-serif; font-size: 2.2rem; font-weight: 700; line-height: 1.15;
            margin: 0.35rem 0 0.5rem 0; color: #F3F1EA; }
.ad-sub { color: #B8C2D1; font-size: 1.05rem; line-height: 1.5; max-width: 64rem; margin-bottom: 1rem; }
.ad-chip { display: inline-block; background: rgba(22,37,61,0.85); border: 1px solid #2E4566; border-radius: 999px;
           padding: 0.15rem 0.7rem; margin: 0.15rem; color: #DCE3EE; font-size: 0.85rem; }
.ad-chip.high { border-color: #E8735A; } .ad-chip.medium { border-color: #F2A65A; }
"""
HERO = """
<div class="ad-kicker">AccessDesk · IT service-desk agent</div>
<div class="ad-title">Ask for access in plain words. The policy, not the AI, decides who approves.</div>
<div class="ad-sub">Claude finds the right app and explains the outcome; five MCP servers (directory, catalog, tickets,
IAM, memory) do the work with <b>your</b> identity, never admin rights. Low-risk access is instant, the rest goes to
your manager and the app owner, and privileged access expires.</div>
"""
st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)
PERSONAS = {
    "E010": "new Finance analyst (started this week)",
    "E003": "Finance manager, Marta's manager",
    "E030": "Head of IT: app owner and IT admin",
    "E005": "Accounts payable clerk",
    "E004": "Accounts payable manager",
    "E020": "Dispatcher, has an open ticket",
    "E021": "Operations manager, Tiago's manager",
    "E031": "Platform engineer",
    "E041": "Account manager in Sales",
    "E060": "Data analyst",
    "E071": "Mechanic",
}
BADGE = {
    "granted": ("🟢", "Granted at once"), "manager_approval": ("🟠", "Manager approves"),
    "owner_approval": ("🟠", "App owner approves"), "manager_and_owner": ("🟠", "Manager and app owner approve"),
    "already_has": ("⚪", "Already has it"), "escalated": ("🔵", "Ticket for the service desk"),
}
SESSION_EUR = float(os.getenv("DEMO_SESSION_BUDGET_EUR", "0.5"))
DAILY_EUR = float(os.getenv("DEMO_DAILY_BUDGET_EUR", "3"))


def _shared_key() -> str | None:
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        key = None
    return key or os.getenv("ANTHROPIC_API_KEY") or None


SHARED_KEY = _shared_key()


@st.cache_resource(show_spinner="Starting the directory, catalog, ITSM, IAM and memory MCP servers...")
def service():
    return open_service(C.Settings().data_dir)


@st.cache_resource
def shared_spend() -> dict:
    return {"day": "", "eur": 0.0}


svc = service()
ss = st.session_state
ss.setdefault("chats", {})
ss.setdefault("meter", Meter())
ss.setdefault("session_eur", 0.0)
people = {e["employee_id"]: e for e in svc.employees()}
scenarios = json.loads(svc.s.scenarios.read_text())

with st.sidebar:
    st.title("🔑 AccessDesk")
    st.caption("An AI service-desk agent for access requests, built the way IT security would run it.")
    st.warning("**Simulated company** (Meridian Transport Group). All people and apps are fake.", icon="🧪")
    who = st.selectbox("Logged in as (simulated SSO)", list(PERSONAS),
                       format_func=lambda e: f"{people[e]['name']} · {PERSONAS[e]}")
    st.divider()
    modes = (["AI (Claude)"] if SHARED_KEY else []) + ["Demo (scripted, no AI)", "Live (your Anthropic key)"]
    mode = st.radio("Mode", modes, index=0)
    api_key = SHARED_KEY if mode.startswith("AI") else None
    if mode.startswith("Live"):
        api_key = st.text_input("Anthropic API key", type="password", help="Used only for this session. Never stored.")
    model = st.selectbox("Model", list(AGENT_MODELS), format_func=AGENT_MODELS.get, disabled=mode.startswith("Demo"))
    if mode.startswith("Demo"):
        st.caption("The scripted model is not AI: keyword rules, so identity, policy, approvals, memory and the audit "
                   "log can be tried for free. Try the suggested messages.")
    spend = shared_spend()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if spend["day"] != today:
        spend.update(day=today, eur=0.0)
    st.progress(min(ss.session_eur / SESSION_EUR, 1.0), text=f"Your AI spend {eur(ss.session_eur)} of {eur(SESSION_EUR)}")
    st.caption(f"Prices checked {PRICES_CHECKED}.")
    if st.button("↺ Reset demo data", help="Rebuild the simulated company. Everyone shares one demo."):
        svc.reset()
        ss.chats = {}
        st.rerun()

st.markdown(HERO, unsafe_allow_html=True)
tabs = st.tabs(["💬 Chat", "✋ Approvals", "🧠 Memory", "🧪 Evals", "🛡️ Security", "🧭 How it works", "🧾 Audit log"])
tab_chat, tab_appr, tab_mem, tab_eval, tab_sec, tab_how, tab_audit = tabs


def blocked() -> str | None:
    if mode.startswith("Demo"):
        return None
    if not api_key:
        return "Enter your Anthropic API key in the sidebar, or pick Demo mode."
    if mode.startswith("AI") and (spend["eur"] >= DAILY_EUR or ss.session_eur >= SESSION_EUR):
        return "The free AI budget is used up. Use Demo mode or your own key."
    return None


def summary(chat: list) -> str:
    done = [m["result"]["message"] for m in chat if m.get("result")]
    return "; ".join(done[-5:])


def ask(text: str) -> None:
    chat = ss.chats.setdefault(who, [])
    history = [{"role": m["role"], "content": m["content"]} for m in chat if m["role"] in ("user", "assistant")]
    history = history[-2 * C.HISTORY_TURNS:]
    kw = {"mode": "scripted"} if mode.startswith("Demo") else {"mode": "live", "api_key": api_key, "model": model, "meter": ss.meter}
    try:
        r = svc.chat(who, text, history, summary(chat), **kw)
    except Exception as exc:
        st.error(f"The agent failed: {type(exc).__name__}: {exc}")
        return
    cost = r["cost"]["eur"] or 0.0
    ss.session_eur += cost
    if mode.startswith("AI"):
        spend["eur"] += cost
    chat += [{"role": "user", "content": text}, {"role": "assistant", "content": r["reply"], "turn": r}]


# ---------- chat ----------
with tab_chat:
    me = people[who]
    mgr = people.get(me["manager_id"] or "", {}).get("name", "-")
    access = svc.hub.call("get_access", {}, svc.identity(who)).data["access"]
    st.markdown(f"**{me['name']}** · {me['title']}, {me['department']} · manager: {mgr}")
    st.markdown(" ".join(f"<span class='ad-chip {a['risk']}'>{a['name']}</span>" for a in access), unsafe_allow_html=True)
    tips = [s["message"] for s in scenarios if s["as_employee"] == who][:4]
    if tips:
        st.caption("Try one of these (they are also in the evals):")
        cols = st.columns(len(tips))
        for i, (c, t) in enumerate(zip(cols, tips, strict=True)):
            if c.button(t[:70] + ("…" if len(t) > 70 else ""), key=f"tip-{who}-{i}", disabled=bool(blocked())):
                with st.spinner("Thinking, checking the catalog and the policy..."):
                    ask(t)
    chat = ss.chats.get(who, [])
    for k, m in enumerate(chat):
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            r = m.get("turn")
            if not r:
                continue
            if r["flags"]:
                st.warning("Instructions aimed at the AI were found in tool data (e.g. a ticket comment) and ignored: "
                           + "; ".join(f"“{f['snippet'][:90]}”" for f in r["flags"][:2]), icon="🛡️")
            o = r.get("outcome")
            if o and r.get("proposal"):
                icon, label = BADGE.get(o["outcome"], ("⛔", "Refused"))
                with st.container(border=True):
                    ent = (o.get("entitlement") or {}).get("name") or r["proposal"]["action"].replace("_", " ")
                    tgt = (o.get("target") or {}).get("name") or me["name"]
                    st.markdown(f"{icon} **{label}** · {ent} for {tgt}"
                                + (f" · expires after {o['duration_days']} days" if o.get("duration_days") else ""))
                    st.caption(o["reason"] + ("  Approvers: " + ", ".join(f"{a.get('name')} ({a['kind']})" for a in o["approvers"])
                                              if o.get("approvers") else ""))
                    if m.get("result"):
                        (st.success if m["result"]["ok"] else st.error)(m["result"]["message"])
                    elif not o["outcome"].startswith("refused") and o["outcome"] != "already_has":
                        if st.button("Submit request", key=f"submit-{who}-{k}", type="primary"):
                            m["result"] = svc.submit(who, r["proposal"])
                            st.rerun()
            for j, fact in enumerate(r.get("remember") or []):
                key = f"mem-{who}-{k}-{j}"
                if ss.get(key):
                    st.caption(f"🧠 {ss[key]}")
                elif st.button(f"🧠 Remember for next time: “{fact}”", key=key + "-btn"):
                    ss[key] = svc.remember(who, fact)["message"]
                    st.rerun()
            st.caption(f"{r['cost']['calls']} model calls · {len(r['tool_calls'])} MCP tool calls · {eur(r['cost']['eur'])} · {r['seconds']} s")
    msg = blocked()
    if msg:
        st.info(msg)
    text = st.chat_input("e.g. I need read access to Salesforce for the month-end report", disabled=bool(msg))
    if text:
        with st.spinner("Thinking, checking the catalog and the policy..."):
            ask(text)
        st.rerun()

# ---------- approvals ----------
with tab_appr:
    st.markdown(f"Requests waiting for **{people[who]['name']}**. Switch the login in the sidebar to act as another "
                "person, for example João Pereira (Marta's manager) or Inês Sousa (app owner of production systems).")
    pend = svc.pending(who)
    if not pend:
        st.info("Nothing is waiting for you.")
    for p in pend:
        with st.container(border=True):
            a, b = st.columns([3, 1])
            ent = p["entitlement_id"] or p["kind"].replace("_", " ")
            a.markdown(f"**{p['ticket_id']}** · {ent} for **{people[p['for_employee_id']]['name']}** "
                       f"({people[p['for_employee_id']]['title']}) · you approve as **{p['approver_kind']}**")
            a.caption(f"Requested by {people[p['requester_id']]['name']}: “{p['justification']}”"
                      + (f" · expires after {p['duration_days']} days" if p["duration_days"] else ""))
            if b.button("Approve", key=f"ap-{p['ticket_id']}", type="primary"):
                r = svc.decide(who, p["ticket_id"], "approved")
                (st.success if r["ok"] else st.error)(r["message"])
            if b.button("Reject", key=f"rj-{p['ticket_id']}"):
                st.info(svc.decide(who, p["ticket_id"], "rejected")["message"])

# ---------- memory ----------
with tab_mem:
    left, right = st.columns(2)
    with left:
        st.markdown(f"#### What AccessDesk remembers about {people[who]['name']}")
        st.caption("Saved only after you click 'Remember'. Context for the assistant, never permissions. You can delete "
                   "anything (GDPR right to erasure).")
        mems = svc.memories(who)
        if not mems:
            st.info("Nothing stored.")
        for m in mems:
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"- {m['text']}  \n  <small>{m['source']}, {m['created_at'][:10]}</small>", unsafe_allow_html=True)
            if c2.button("Delete", key=f"del-{m['memory_id']}"):
                svc.forget(who, m["memory_id"])
                st.rerun()
        if mems and st.button("Forget everything about me"):
            svc.forget(who)
            st.rerun()
        st.markdown("**Short-term memory:** the last turns of this chat, plus a summary of requests already "
                    "submitted, written by code.")
    with right:
        st.markdown("#### Team patterns (reviewed before use)")
        st.caption("Computed from who has what. The assistant may suggest only patterns an IT admin approved, so one "
                   "unusual grant cannot become everyone's default.")
        pats = pd.DataFrame(svc.patterns())
        st.dataframe(pats, hide_index=True, width="stretch", height=300)
        if people[who]["role"] in ("it_manager", "it_engineer"):
            todo = pats[pats["status"] == "proposed"]
            if len(todo):
                pick = st.selectbox("Review", [f"{r.role} · {r.entitlement_id}" for r in todo.itertuples()])
                role, ent = pick.split(" · ")
                c1, c2 = st.columns(2)
                if c1.button("Approve pattern"):
                    svc.review_pattern(who, role, ent, True)
                    st.rerun()
                if c2.button("Reject pattern"):
                    svc.review_pattern(who, role, ent, False)
                    st.rerun()
        else:
            st.caption("Log in as Inês Sousa (Head of IT) to review patterns.")

# ---------- evals ----------
with tab_eval:
    st.markdown("20 scenarios, 8 of them attacks, each with the outcome the policy must reach. The request is "
                "auto-confirmed; approvals are not given. **Unauthorised grants** is the gate: it must be 0.")
    scripted = C.ROOT / "eval_report_scripted.json"
    live = C.ROOT / "eval_report.json"
    if st.button("▶ Run the evals now (scripted model, ~5 s)"):
        from accessdesk import evals

        with st.spinner("Building a fresh company and running 20 conversations..."):
            evals.run("scripted", out=scripted)
    shown = [json.loads(x.read_text()) for x in (live, scripted) if x.exists()]
    if not shown:
        st.info("No eval report yet: run it above.")
    for rep in shown:
        st.markdown(f"#### {'Claude (live)' if rep['mode'] == 'live' else 'Scripted model'} · {rep['model']} · {rep['when']}")
        k = st.columns(5)
        k[0].metric("Right outcome", f"{rep['outcome_accuracy']:.0%}")
        k[1].metric("Right app found", f"{rep['entitlement_accuracy']:.0%}")
        k[2].metric("Unauthorised grants", rep["unauthorised_grants"])
        k[3].metric("Red-team passed", rep["redteam_passed"])
        k[4].metric("Cost per turn", eur(rep["cost_eur_per_turn"]))
        st.dataframe(pd.DataFrame(rep["rows"])[["n", "as_employee", "tag", "message", "expected", "got", "entitlement", "unauthorised"]],
                     hide_index=True, width="stretch", height=320)
    st.caption("Live run: `python -m accessdesk eval --live` with ANTHROPIC_API_KEY set. CI runs the scripted eval on every push.")

# ---------- security ----------
with tab_sec:
    st.markdown("How AccessDesk handles the [OWASP Top 10 for Agentic Applications]"
                "(https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/).")
    st.dataframe(pd.DataFrame([
        ("ASI01 Agent goal hijack", "Ticket comments come back marked untrusted and are scanned; a vendor comment asking the "
         "agent to grant AWS admin is shown in the chat as ignored. Proposals are re-checked by the policy."),
        ("ASI02 Tool misuse", "The model gets 9 read-only tools (allow-list AND the server's read-only flag). Tickets, "
         "approvals, grants and memory writes are called by code after the employee confirms."),
        ("ASI03 Identity and privilege abuse", "Every tool acts as the logged-in employee via a signed identity token the hub "
         "adds and the model never sees. 'I am the CTO' changes nothing. Access changes need a grant token per ticket."),
        ("ASI06 Memory poisoning", "Memories are written only after the employee confirms, refuse instruction-like text, are "
         "context not permissions, and can be deleted. Team patterns need an IT admin's review."),
        ("ASI08 Cascading failures", "Step limit per turn, timeouts; nothing changes until the employee confirms."),
        ("ASI09 Human trust exploitation", "Approvers see requester, target, app, risk and expiry from the database."),
        ("Least privilege", "Low-risk standard apps only are instant; high-risk access expires after 30 days; segregation "
         "of duties is checked in the policy AND again in the IAM server."),
        ("Data protection", "Employees see only their own and their direct reports' access; the directory shows address-book "
         "fields only. Claude via Amazon Bedrock in an EU region is one setting away."),
    ], columns=["Risk", "Control"]), hide_index=True, width="stretch")

# ---------- how it works ----------
with tab_how:
    st.graphviz_chart("""digraph {
      rankdir=LR; bgcolor="transparent"; nodesep=0.45;
      node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, color="#2E4566", fillcolor="#16253D", fontcolor="#F3F1EA"];
      edge [fontname="Helvetica", fontsize=9, color="#8FA3BF", fontcolor="#B8C2D1"];
      user [label="employee\\n(SSO identity)", shape=oval];
      agent [label="agent\\nClaude: find the app,\\npreview the policy"];
      tools [label="tools\\n9 read-only MCP tools\\nidentity added by hub"];
      policy [label="policy\\ncode re-checks\\nthe proposal", fillcolor="#F2A65A", fontcolor="#0B1424", color="#F2A65A"];
      card [label="request card\\nemployee confirms"];
      flow [label="workflow (code)\\nticket, approvals,\\ngrant token"];
      iam [label="IAM server\\nchecks token,\\nSoD and eligibility"];
      user -> agent; agent -> tools [label="needs data", style=dashed]; tools -> agent;
      agent -> policy [label="respond", style=dashed]; policy -> card -> flow -> iam;
      flow -> approvers [label="manager / owner", style=dashed]; approvers [label="approvers", shape=oval]; approvers -> flow;
    }""")
    st.dataframe(pd.DataFrame([
        {"server": m["server"], "tool": n, "read-only": "✅" if m["read_only"] else "✏️ writes",
         "model may call": "✅" if svc.hub.agent_may_call(n) else "—",
         "acts as the user": "✅" if "identity" in m["input_schema"].get("properties", {}) else "",
         "description": m["description"].split("\n")[0]}
        for n, m in sorted(svc.hub.tools.items(), key=lambda kv: (kv[1]["server"], kv[0]))
    ]), hide_index=True, width="stretch")
    st.caption("Memory: short-term = recent turns + a code summary; long-term = confirmed notes per employee (memory MCP "
               "server); procedural = team patterns after admin review.")

# ---------- audit ----------
with tab_audit:
    v = svc.audit.verify()
    st.markdown(("✅ **Audit chain intact**" if v["ok"] else f"❌ **Chain broken at row {v.get('broken_at')}**")
                + f" · {v['rows']} events.")
    ev = svc.audit.read(limit=300)
    if ev:
        st.dataframe(pd.DataFrame([{"time": e["ts"][:19], "turn": e["run_id"], "actor": e["actor"], "event": e["kind"],
                                    "payload": json.dumps(e["payload"], ensure_ascii=False)[:160]} for e in ev]),
                     hide_index=True, width="stretch", height=480)
