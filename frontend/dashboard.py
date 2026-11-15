"""AlphaForge dashboard (boxes ① and ⑦): profile, question, and a results dashboard personalised by profile.

    streamlit run frontend/dashboard.py

Data comes only from the API (box ②): GET/PUT /v1/profile, POST /v1/runs, GET /v1/runs,
GET /v1/runs/{id}, GET /v1/runs/{id}/evidence and GET /v1/runs/{id}/replay. See frontend/client.py for HTTP vs in-process mode.
Useful variables: AF_STORE=dynamodb (shared DynamoDB data), AF_INTAKE_MODE=bedrock (real intake),
AF_API_URL (deployed API). Simulation only: nothing here is investment advice.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import charts  # noqa: E402
import insights  # noqa: E402
import views  # noqa: E402
from client import DEMO_ACTOR_PREFIX, ApiClient, demo_portfolio, persona_form, personas  # noqa: E402

st.set_page_config(page_title="AlphaForge", page_icon="📈", layout="wide")

PERSONA_BLURB = {
    "amira": "26 · software engineer · high risk · 15+ years",
    "mateo": "41 · operations manager · moderate · ESG",
    "robert": "59 · healthcare professional · capital preservation · 3-5 years",
}
EXAMPLES = {
    "robert": "I'm thinking of putting 20% in Nvidia, everyone is talking about it.",
    "amira": "My portfolio is 40% NVDA, 30% QQQ and 30% Bitcoin. Is it too risky, and is it really diversified?",
    "mateo": "Propose a growth/stability balance avoiding companies with major controversies.",
}


@st.cache_resource
def api() -> ApiClient:
    return ApiClient()


@st.cache_data
def rules() -> dict:
    import yaml

    return yaml.safe_load((Path(__file__).resolve().parents[1] / "backend/app/core/rules.yaml").read_text("utf-8"))


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def pct(x, digits=1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def ev_tag(eid: str | None) -> str:
    return f"[`{eid}`](#{eid})" if eid else ""


# ---------------------------------------------------------------- sidebar: who am I
client = api()
PERSONAS = personas()
with st.sidebar:
    st.title("📈 AlphaForge")
    st.caption("Self-explaining investment analysis · **simulation only, not advice**")
    choice = st.radio("Investor", list(PERSONAS) + ["custom"],
                      format_func=lambda k: k.capitalize() if k != "custom" else "Other user id")
    if choice == "custom":
        actor = st.text_input("User id", value="me")
        persona = None
    else:
        actor = f"{DEMO_ACTOR_PREFIX}{choice}"
        persona = PERSONAS[choice]
        st.caption(PERSONA_BLURB.get(choice, ""))
    st.divider()
    st.markdown("**How much detail?**")
    detail = st.radio("Detail", ["auto", *views.LEVELS], label_visibility="collapsed",
                      format_func=lambda k: "Auto (from my profile)" if k == "auto" else views.LEVEL_LABELS[k],
                      key="detail")
    show_all = st.toggle("Show all data", key="show_all",
                         help="Every figure, table and tool call behind the answer, whatever the profile.")
    st.divider()
    st.caption(f"API: {client.mode}")
    svc = getattr(client, "service", None)
    if svc is not None:
        st.caption(f"Store: {type(svc.runs).__name__} · dispatcher: {type(svc.dispatcher).__name__}")

code, profile = client.get_profile(actor)
profile = profile if code == 200 else None

tab_dash, tab_ask, tab_profile = st.tabs(["📊 My analyses", "💬 Ask", "👤 Profile"])

# ---------------------------------------------------------------- profile
with tab_profile:
    if profile:
        st.success(f"Profile on file · focus: **{views.focus_label(profile)}**")
        c1, c2, c3, c4 = st.columns(4)
        h = profile.get("horizon_years") or {}
        c1.metric("Risk band", str(profile.get("risk_band")).replace("_", "-"))
        c2.metric("Horizon", f"{h.get('min')}–{h.get('max') or '∞'} years")
        c3.metric("Experience", str(profile.get("experience")))
        c4.metric("Capital (fictional)", f"${profile.get('capital') or 0:,.0f}")
        limit = views.profile_loss_limit(profile, rules())
        if limit is not None:
            st.caption(f"Maximum crisis loss for this risk band (rules.yaml): **{pct(limit, 0)}**")
        exp_opts = ["beginner", "intermediate", "advanced"]
        cur = profile.get("experience") if profile.get("experience") in exp_opts else "intermediate"
        new_exp = st.selectbox("Financial knowledge (sets how detailed the dashboard is)", exp_opts,
                               index=exp_opts.index(cur), key=f"exp-{actor}")
        if new_exp != cur and st.button("Save knowledge level"):
            c, p = client.put_profile(actor, {"experience": new_exp})
            st.toast("Saved" if c == 200 else f"Error {c}: {p}")
            st.rerun()
        with st.expander("Full profile JSON"):
            st.json(profile)
    else:
        st.info("No profile yet for this user.")
    if persona and st.button(f"{'Reset' if profile else 'Create'} profile from the {choice.capitalize()} persona"):
        c, p = client.put_profile(actor, persona_form(persona))
        st.toast("Profile saved" if c == 200 else f"Error {c}: {p}")
        st.rerun()

# ---------------------------------------------------------------- ask
with tab_ask:
    st.subheader("Ask AlphaForge")
    default = EXAMPLES.get(choice, "")
    text = st.text_area("Your question", value=default, height=90)
    pf = demo_portfolio(actor)
    if pf:
        st.caption("Current portfolio used for the analysis: " + ", ".join(f"{k} {v:.0%}" for k, v in pf.items()))
    if st.button("Analyze", type="primary", disabled=not text.strip()):
        with st.spinner("Intake → plan → agents → tools (Gateway, Knowledge Base)… this can take a minute"):
            c, out = client.post_message(actor, text)
        if out.get("kind") == "run":
            st.session_state["selected_run"] = out["run_id"]
            st.success(f"Run {out['run_id']} · {out.get('run_status')} · agents: {', '.join(out.get('enabled_agents', []))}")
            st.info("Open **📊 My analyses** to see the dashboard.")
        elif out.get("kind") == "question":
            st.warning(f"AlphaForge needs one more detail: {out.get('question')}")
        else:
            st.write(out.get("message") or out)

# ---------------------------------------------------------------- dashboard
# One layout for every reader, richer at each level (simple ⊂ standard ⊂ expert ⊂ show all data):
#   verdict · score /10 · "you could lose"  →  explanation  →  2022 replay chart  →  Venn diagrams
#   expert adds three charts and the key parameters; "show all data" adds news, rules, evidence and the raw run.
with tab_dash:
    c, runs = client.list_runs(actor)
    runs = runs if c == 200 else []
    if not runs:
        st.info("No analysis yet: ask a question in the 💬 Ask tab.")
        st.stop()
    labels = {r["run_id"]: f"{r['created_at'][:16].replace('T', ' ')} · {r['status']} · {r['question'][:70]}"
              for r in runs}
    ids = list(labels)
    sel = st.session_state.get("selected_run")
    run_id = st.selectbox("Analysis", ids, index=ids.index(sel) if sel in ids else 0, format_func=labels.get)
    _, run = client.get_run(actor, run_id)
    _, records = client.get_evidence(actor, run_id)
    records = records if isinstance(records, list) else []
    rcode, rp = client.get_replay(actor, run_id)
    rp = rp if rcode == 200 and isinstance(rp, dict) else None
    ev = views.RunEvidence.from_records(records)
    prof = run.get("profile") or profile or {}
    mode = theme_mode()
    result = run.get("result") or {}

    level_profile = profile or prof
    level = views.level_for(level_profile, None if detail == "auto" else detail, show_all)
    simple, expert = level == "simple", level == "expert"
    why = ("Show all data is on" if show_all else "your choice" if detail != "auto"
           else f"{level_profile.get('experience', 'unknown')} investor")

    limit = views.profile_loss_limit(prof, rules())
    verdict = views.verdict(ev)
    curve = views.position_curve(ev, limit)
    rc = views.risk_contribution(ev)
    loss = views.loss_limit(ev, limit)
    sc = insights.scores(ev)
    series = (rp or {}).get("series") or {}
    idea, prop = series.get("idea"), series.get("proposal")

    st.markdown(f"### “{run['request']['user_message']}”")
    st.caption(f"{views.LEVEL_LABELS[level]} view · {why} · change it in the sidebar")
    if run["status"] != "DONE":
        st.warning(run.get("error") or run.get("note") or "The analysis has not finished yet.")
        st.stop()

    # ===== 1. verdict · score · what you could lose (same row for everyone)
    col_v, col_s, col_l = st.columns(3)
    with col_v.container(border=True):
        st.markdown("**Verdict**")
        if verdict:
            first = (verdict.get("initial") or verdict)["verdict"]
            good = first == "PASS"
            st.markdown(f"## {'✅' if good else '⛔'} {'A good fit for you' if good else 'Too risky for you'}")
            if verdict.get("initial") and verdict["verdict"] != first:
                st.caption(f"The suggested mix: {'✅ fits your profile' if verdict['verdict'] == 'PASS' else '⛔ still too risky'}"
                           + ("" if simple else f" (governance {first} → {verdict['verdict']})"))
            elif not simple:
                st.caption(f"Governance: {first}")
    with col_s.container(border=True):
        st.markdown("**Fit score for your profile**")
        if sc.get("idea"):
            st.plotly_chart(charts.score_gauge(sc["idea"]["score"], sc["idea"]["band"], mode), width="stretch",
                            theme=None, config={"displayModeBar": False})
            if sc.get("proposal"):
                st.caption(f"Suggested mix: **{sc['proposal']['score']:g} / 10** ({sc['proposal']['band'].lower()})")
            if not simple and sc["idea"]["why"]:
                with st.expander("How the score is computed"):
                    st.caption("Starts at 10; points come off for each rule of your profile the idea breaks.")
                    st.dataframe(sc["idea"]["why"], hide_index=True, width="stretch")
    with col_l.container(border=True):
        st.markdown("**You could lose, in a bad year**")
        if idea and rp:
            lost = rp["capital"] * abs(idea["max_drawdown"])   # peak to trough, as the stress tool measures it
            st.markdown(f"## ${lost:,.0f}")
            st.caption(f"{abs(idea['max_drawdown']):.0%} of ${rp['capital']:,.0f} at the worst moment of 2022"
                       + (f" · the most you accept: {abs(limit):.0%}" if limit is not None else ""))
            if prop:
                st.caption(f"With the suggested mix: ${rp['capital'] * abs(prop['max_drawdown']):,.0f}")

    # ===== 2. explanation
    with st.container(border=True):
        st.markdown("**What this means**" if simple else "**Analysis**")
        if simple:
            st.write(" ".join(views.plain_summary(ev, prof, limit)))
        else:
            st.write(insights.analysis_paragraph(ev, prof, limit, rp))

    # ===== 3. the 2022 replay ("what could happen")
    if rp and series:
        with st.container(border=True):
            st.plotly_chart(charts.replay(rp, limit, mode, detailed=not simple), width="stretch", theme=None)
            st.caption("What your money would have done if 2022, a year when stocks and bonds fell together, "
                       "happened again with these portfolios. A past scenario, not a forecast."
                       + ("" if simple else " Daily rebalancing, same code as the stress tool."))

    # ===== 4. Venn diagrams
    venns = [("Your idea and what your profile allows", insights.venn_idea_vs_allowed(prof, curve, rp))]
    if not simple:
        venns.append(("What changes in your portfolio", insights.venn_holdings(demo_portfolio(actor), (prop or {}).get("weights"))))
    venns = [(t, v) for t, v in venns if v]
    if venns:
        cols = st.columns(len(venns))
        for col, (title, v) in zip(cols, venns):
            with col.container(border=True):
                st.markdown(f"**{title}**")
                st.plotly_chart(charts.venn2(v, mode), width="stretch", theme=None, config={"displayModeBar": False})
    if expert:
        stress_rows = (loss or {}).get("scenarios") or []
        worst = min(stress_rows, key=lambda s: s["max_drawdown"] or 0) if stress_rows else None
        v3 = insights.venn_risk_drivers(rc, (worst or {}).get("per_asset"))
        if v3:
            with st.container(border=True):
                st.markdown("**Where the risk concentrates** · holdings in the centre are big, risky and fell hard")
                st.plotly_chart(charts.venn3(v3, mode), width="stretch", theme=None, config={"displayModeBar": False})
                if v3["outside"]:
                    st.caption("In none of the three sets: " + ", ".join(v3["outside"]))

        # ===== 5. three more charts
        c1, c2, c3 = st.columns(3)
        if curve:
            with c1.container(border=True):
                st.plotly_chart(charts.position_curve(curve, curve["max_weight"], mode), width="stretch", theme=None)
        if rc and rc.get("risk"):
            with c2.container(border=True):
                st.plotly_chart(charts.risk_vs_weight(rc, mode), width="stretch", theme=None)
        if worst and worst.get("per_asset"):
            with c3.container(border=True):
                st.plotly_chart(charts.stress_assets(worst["per_asset"], mode), width="stretch", theme=None)

        # ===== 6. key parameters
        params = insights.key_parameters(ev, rp)
        if params:
            with st.container(border=True):
                st.markdown("**Key parameters**")
                st.dataframe(params, hide_index=True, width="stretch")

    # ===== the full answer, one click away for everyone
    answer = result.get("summary") or ""
    checks = views.claim_check(answer, records)
    with st.expander("Read AlphaForge's full answer"):
        if checks:
            bad = [c["evidence_id"] for c in checks if c["status"] == "not_found"]
            (st.error if bad else st.success)(
                f"Citations not found in the evidence ledger: {', '.join(bad)}" if bad
                else f"All {len(checks)} cited evidence IDs exist in the ledger.")
        st.markdown(views.link_evidence(answer) if show_all else answer)

    if not show_all:
        st.caption(f"Turn on **Show all data** in the sidebar for the news, the rules, and the {len(records)} "
                   "tool calls behind this answer.")
        st.stop()

    # ===== show all data: everything behind the answer
    st.divider()
    st.markdown("### All data")
    n = views.news(ev)
    if n:
        with st.expander(f"News and filings used ({len(n['hits'])})", expanded=True):
            for h in n["hits"]:
                kind = "📄 annual report" if h.get("doc_type") == "filing" else "📰 news"
                link = f"[{h['title']}]({h['source_url']})" if h.get("source_url") else h["title"]
                st.markdown(f"- **{h.get('published_date')}** · {kind} · {link} {ev_tag(h['evidence_id'])}")
    if verdict:
        def fmt(x):
            return pct(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else ("" if x is None else str(x))

        with st.expander("Governance rules: your idea and the suggested mix", expanded=True):
            stages = [("Your idea", verdict.get("initial") or verdict)] + (
                [("Suggested mix", verdict)] if verdict.get("initial") else [])
            st.dataframe([{"portfolio": name, "rule": c.get("id"), "status": c.get("status"), "value": fmt(c.get("value")),
                           "limit": fmt(c.get("limit")), "why": c.get("justification") or "",
                           "evidence": c.get("evidence_id") or ""} for name, v in stages for c in v["checks"]],
                         hide_index=True, width="stretch")
    if checks:
        with st.expander(f"Citation check ({len(checks)} evidence IDs quoted in the answer)"):
            st.dataframe([{"evidence": c["evidence_id"], "found": {"verified": "✅ top-level record",
                           "nested": "✅ inside a tool result", "not_found": "❌ not in the ledger"}[c["status"]]}
                          for c in checks], hide_index=True, width="stretch")
    st.markdown("#### 🔍 Evidence ledger")
    st.caption("Every tool call of this run, in order: who called it, with which inputs, what it returned.")
    cited = {c["evidence_id"] for c in checks}
    for r in records:
        eid = r.get("evidence_id")
        b = views.body(r)
        say = (b.get("say") or b.get("headline") or "") if isinstance(b, dict) else ""
        if eid:
            st.markdown(f'<div id="{eid}"></div>', unsafe_allow_html=True)
        icon = "⭐" if eid in cited else ("❌" if r.get("error") else "•")
        with st.expander(f"{icon} {r['seq']} · {r.get('agent') or 'orchestrator'} → {r['tool']} · {eid or '–'} · {say[:90]}"):
            if r.get("error"):
                st.error(r["error"])
            if isinstance(b, dict):
                for key in ("say", "as_of", "period", "method"):
                    if b.get(key):
                        st.markdown(f"**{key}**: {b[key]}")
                if b.get("assumptions"):
                    st.markdown("**assumptions**: " + " · ".join(map(str, b["assumptions"])))
                if b.get("data_sources"):
                    st.markdown("**sources**: " + " · ".join(map(str, b["data_sources"])))
                st.json({"inputs": b.get("inputs"), "output": b.get("output", b)}, expanded=False)
            else:
                st.code(json.dumps(b, ensure_ascii=False, default=str)[:3000])
    with st.expander("Raw run record and crisis replay"):
        st.json({"run": run, "replay": rp}, expanded=False)
