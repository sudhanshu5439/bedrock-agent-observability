"""Bedrock agent observability & audit dashboard.

Run:  .venv\\Scripts\\streamlit run app\\dashboard.py
Needs a valid SSO session:  aws sso login --profile genai-demo
"""
import hmac
import json
import os
import shutil
import subprocess
import time

import altair as alt
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Bedrock Agent Observability", layout="wide")


def password_gate():
    """When DASHBOARD_PASSWORD is set (hosted mode), ask for it once per browser session."""
    expected = os.environ.get("DASHBOARD_PASSWORD", "")
    if not expected or st.session_state.get("authed"):
        return
    st.title("Bedrock Agent Observability")
    with st.form("login"):
        given = st.text_input("Password", type="password")
        if st.form_submit_button("Open dashboard", type="primary"):
            if hmac.compare_digest(given.encode(), expected.encode()):
                st.session_state["authed"] = True
                st.rerun()
            time.sleep(2)  # slow down guessing
            st.error("Wrong password.")
    st.stop()


password_gate()

import data  # noqa: E402  (after set_page_config: data imports streamlit caching)
from invoke import AGENTS, invoke  # noqa: E402

# ------------------------------------------------------------------ chart style (reference palette, light mode)
AGENT_COLORS = {"HR Assistant": "#2a78d6", "IT Support": "#eb6834"}  # categorical slots 1-2
STEP_COLORS = {"Model": "#2a78d6", "Tool": "#eb6834", "Guardrail": "#1baf7a", "Agent loop": "#b9b8b2"}
CRITICAL = "#d03b3b"
TEXT_2, GRID = "#52514e", "#e9e8e4"


@alt.theme.register("demo", enable=True)
def _theme():
    return {"config": {
        "background": "transparent", "view": {"stroke": None},
        "font": "Source Sans Pro, sans-serif",
        "axis": {"labelColor": TEXT_2, "titleColor": TEXT_2, "gridColor": GRID, "domain": False,
                 "tickColor": GRID, "labelFontSize": 12, "titleFontSize": 12, "titleFontWeight": "normal"},
        "legend": {"labelColor": TEXT_2, "titleColor": TEXT_2, "orient": "top", "labelFontSize": 12},
        "line": {"strokeWidth": 2}, "point": {"size": 70, "filled": True},
        "bar": {"cornerRadiusEnd": 4},
    }}



def agent_color(field="agent"):
    return alt.Color(f"{field}:N", title=None,
                     scale=alt.Scale(domain=list(AGENT_COLORS), range=list(AGENT_COLORS.values())))


def fmt_ms(v):
    return "–" if pd.isna(v) else f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"


def short_json(obj, n=400):
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "…"


# ------------------------------------------------------------------ header + global filter
st.title("Bedrock Agent Observability")


def aws_signin_gate():
    """Stop with a sign-in button when the SSO session has expired or AWS is unreachable."""
    try:
        return data.caller_account()
    except Exception as e:  # expired SSO token, missing login, or no network
        msg = str(e)
        offline = any(k in msg for k in ("Could not connect", "getaddrinfo", "Failed to resolve", "EndpointConnection"))
        if offline:
            st.error("Can't reach AWS. Check the internet connection (if Chrome works but this doesn't, the "
                     "router's DNS is down: restart the router or set Wi-Fi DNS to 8.8.8.8).", icon="📡")
        else:
            st.warning("Your AWS sign-in has expired. Sign in, then press Retry.", icon="🔑")
            if st.button("Sign in to AWS (opens your browser)", type="primary"):
                aws = shutil.which("aws") or r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"
                subprocess.Popen([aws, "sso", "login", "--profile", data.PROFILE],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                st.info("Finish the sign-in in the browser tab that opened, then press Retry.")
        with st.expander("Details"):
            st.code(msg[:600])
        if st.button("Retry"):
            data.clear_all()
            st.rerun()
        st.stop()


ACCOUNT = data.redact_text(aws_signin_gate())
st.caption(f"Account {ACCOUNT} · us-east-1 · demo agents only (bedrock-demo-*) · all data read live from AWS")

RANGES = {"Last 1 hour": 1, "Last 6 hours": 6, "Last 24 hours": 24, "Last 7 days": 168}
c1, c2, _ = st.columns([2, 1, 5])
range_label = c1.selectbox("Time range", list(RANGES), index=2)
HOURS = RANGES[range_label]
BIN = {1: 5, 6: 15, 24: 60, 168: 360}[HOURS]
if c2.button("Refresh", width="stretch"):
    data.clear_all()

data.prefetch(HOURS, BIN)  # starts every fetch in parallel and returns; instant when the warmer has filled the caches
data.keep_warm(HOURS, BIN)
items = data.trace_items(HOURS)
call_df = data.calls(items)
sess_df = data.sessions(call_df)

# Only the open tab is drawn: drawing all seven (12 tables, 5 charts) made every page load take seconds.
tabs = st.tabs(["Overview", "Configuration", "Metrics", "Sessions & traces", "Audit trail",
                "Guardrails & errors", "Live test"], key="tab", on_change="rerun", bind="query-params")

# ================================================================== 1. Overview
with tabs[0]:
    if tabs[0].open:
        if call_df.empty:
            st.info("No agent calls in this time range. Use the Live test tab or run traffic/run_traffic.py.")
        else:
            cols = st.columns(2)
            for col, agent in zip(cols, AGENTS.values(), strict=True):
                d = call_df[call_df["agent_name"] == agent["name"]]
                s = sess_df[sess_df["agent"] == agent["name"]]
                kind = "Classic Bedrock Agent" if agent["type"] == "classic" else "AgentCore Harness + Gateway"
                with col.container(border=True):
                    st.subheader(agent["name"])
                    st.caption(f"{kind} · `{agent['agent_id']}`")
                    m = st.columns(4)
                    m[0].metric("Conversations", len(s))
                    m[1].metric("Calls", len(d))
                    err = (d["status"] == "error").mean() if len(d) else 0
                    m[2].metric("Error rate", f"{err:.0%}", help="Calls that returned an exception to the caller")
                    m[3].metric("p95 latency", fmt_ms(d["latency_ms"].quantile(0.95)))
                    m = st.columns(4)
                    m[0].metric("Median latency", fmt_ms(d["latency_ms"].median()))
                    m[1].metric("Tokens", f"{int(d['input_tokens'].sum() + d['output_tokens'].sum()):,}")
                    m[2].metric("Est. cost", f"${d['cost_usd'].sum():.3f}", help="Haiku 4.5 list price, model tokens only")
                    m[3].metric("Guardrail blocks", int((d["guardrail_action"] == "INTERVENED").sum()))
                    last = d["ts"].max()
                    st.caption(f"Last call {last:%d %b %H:%M} UTC · {d['user'].nunique()} distinct users")

            st.markdown("##### Calls over time")
            st.caption(f"Agent calls per {BIN} minutes")
            bins = pd.date_range(pd.Timestamp(data.since(HOURS)).floor(f"{BIN}min"), pd.Timestamp.now(tz="UTC").floor(f"{BIN}min"),
                                 freq=f"{BIN}min")
            per_bin = (call_df.assign(t=call_df["ts"].dt.floor(f"{BIN}min"))
                       .groupby(["t", "agent_name"]).size()
                       .reindex(pd.MultiIndex.from_product([bins, list(AGENT_COLORS)], names=["t", "agent_name"]),
                                fill_value=0)
                       .rename("calls").reset_index().rename(columns={"agent_name": "agent"}))
            per_bin["period"] = per_bin["t"].dt.strftime("%d %b %H:%M")
            chart = alt.Chart(per_bin).mark_bar().encode(
                x=alt.X("period:O", title=None, sort=None, axis=alt.Axis(labelAngle=0, labelOverlap="greedy")),
                xOffset=alt.XOffset("agent:N", sort=list(AGENT_COLORS)),
                y=alt.Y("calls:Q", title="Calls"), color=agent_color(),
                tooltip=[alt.Tooltip("period:N", title="Period starting (UTC)"), "agent:N", "calls:Q"])
            st.altair_chart(chart, width="stretch", theme=None)

            st.markdown("##### Latest calls")
            latest = call_df.sort_values("ts", ascending=False).head(15)
            st.dataframe(latest[["ts", "agent_name", "user", "prompt", "status", "latency_ms", "guardrail_action"]]
                         .rename(columns={"ts": "time", "agent_name": "agent", "latency_ms": "latency (ms)",
                                          "guardrail_action": "guardrail"}),
                         hide_index=True, width="stretch")

# ================================================================== 2. Configuration
with tabs[1]:
    if tabs[1].open:
        st.caption("Current configuration read from the Bedrock and AgentCore control APIs, plus who changed it.")
        left, right = st.columns(2)
        with left:
            st.subheader("HR Assistant · classic agent")
            try:
                c = data.classic_config()
                a, g = c["agent"], c["guardrail"]
                st.table(pd.DataFrame({"": [
                    a["agentStatus"], a["foundationModel"], a["agentResourceRoleArn"].split("/")[-1],
                    f"{a['idleSessionTTLInSeconds']} s",
                    f"{a['guardrailConfiguration']['guardrailIdentifier']} v{a['guardrailConfiguration']['guardrailVersion']}",
                    f"{c['alias']['agentAliasName']} → version "
                    f"{', '.join(r['agentVersion'] for r in c['alias'].get('routingConfiguration', []))}",
                    f"{a['updatedAt']:%d %b %Y %H:%M} UTC"]},
                    index=["Status", "Model", "Service role", "Idle session timeout", "Guardrail", "Alias", "Last updated"]), hide_header=True)
                with st.expander("Instruction (system prompt)"):
                    st.write(a["instruction"])
                st.markdown("**Tools (action groups)**")
                for ag in c["action_groups"]:
                    fns = ag.get("functionSchema", {}).get("functions", [])
                    st.dataframe(pd.DataFrame([{
                        "action group": ag["actionGroupName"], "function": f["name"], "state": ag["actionGroupState"],
                        "parameters": ", ".join(f.get("parameters", {})), "description": f.get("description", "")}
                        for f in fns]), hide_index=True, width="stretch")
                lm = c["lambda"]
                st.caption(f"Executor: Lambda `{lm['FunctionName']}` · {lm['Runtime']} · {lm['MemorySize']} MB · "
                           f"timeout {lm['Timeout']} s · modified {lm['LastModified'][:16]}")
                st.markdown("**Guardrail policies**")
                rows = [{"policy": "Denied topic", "setting": t["name"], "action": t.get("inputAction", "BLOCK")}
                        for t in g.get("topicPolicy", {}).get("topics", [])]
                rows += [{"policy": "Content filter", "setting": f["type"],
                          "action": f"input {f['inputStrength']} / output {f['outputStrength']}"}
                         for f in g.get("contentPolicy", {}).get("filters", [])]
                rows += [{"policy": "PII", "setting": p["type"], "action": p["action"]}
                         for p in g.get("sensitiveInformationPolicy", {}).get("piiEntities", [])]
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            except Exception as e:
                st.error(f"Could not read classic agent config: {e}")

        with right:
            st.subheader("IT Support · AgentCore harness")
            try:
                c = data.agentcore_config()
                h = c["harness"]
                model = h.get("model", {}).get("bedrockModelConfig", {})
                st.table(pd.DataFrame({"": [
                    h["status"], model.get("modelId", ""), h["executionRoleArn"].split("/")[-1],
                    f"{h.get('maxIterations')} iterations · {h.get('maxTokens')} tokens · {h.get('timeoutSeconds')} s",
                    "disabled" if "disabled" in (h.get("memory") or {}) else json.dumps(h.get("memory")),
                    f"{c['gateway']['name']} ({c['gateway']['status']}, auth {c['gateway']['authorizerType']})",
                    f"{h['updatedAt']:%d %b %Y %H:%M} UTC" if h.get("updatedAt") else ""]},
                    index=["Status", "Model", "Execution role", "Limits", "Memory", "Gateway", "Last updated"]), hide_header=True)
                with st.expander("System prompt"):
                    st.write(" ".join(p.get("text", "") for p in h.get("systemPrompt", [])))
                st.markdown("**Tools available to the agent**")
                declared = data.harness_declared_tools()
                gw_tools = {t["name"]: t for t in c["target"]["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"]}
                rows = []
                for name in declared or [f"{c['target']['name']}___{n}" for n in gw_tools]:
                    short = name.split("___")[-1]
                    if short in gw_tools:
                        rows.append({"tool": name, "source": f"Gateway target {c['target']['name']} → Lambda",
                                     "description": gw_tools[short]["description"]})
                    else:
                        rows.append({"tool": name, "source": "Built into the harness", "description": "⚠ not configured by us"})
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                builtins = [r["tool"] for r in rows if r["source"] == "Built into the harness"]
                if builtins:
                    st.warning(f"The harness also exposes built-in tools: {', '.join(builtins)}. They were not added by us "
                               "and are only held back by the system prompt. Review whether they should be disabled.",
                               icon="⚠️")
                lm = c["lambda"]
                st.caption(f"Gateway executor: Lambda `{lm['FunctionName']}` · {lm['Runtime']} · {lm['MemorySize']} MB · "
                           f"timeout {lm['Timeout']} s · modified {lm['LastModified'][:16]}")
            except Exception as e:
                st.error(f"Could not read AgentCore config: {e}")

        st.subheader("Configuration change history")
        st.caption("Create / update / delete calls on these resources, from CloudTrail event history (last 30 days).")
        try:
            ch = data.config_changes()
            if ch.empty:
                st.info("No configuration changes found.")
            else:
                st.dataframe(ch, hide_index=True, width="stretch")
        except Exception as e:
            st.error(f"Could not read CloudTrail event history: {e}")

# ================================================================== 3. Metrics
with tabs[2]:
    if tabs[2].open:
        if call_df.empty:
            st.info("No calls in this time range.")
        else:
            a, b = st.columns(2)
            with a:
                st.markdown("##### End-to-end latency per call")
                pts = call_df.rename(columns={"agent_name": "agent"})
                chart = alt.Chart(pts).mark_point().encode(
                    x=alt.X("ts:T", title="Time (UTC)", scale=alt.Scale(type="utc"), axis=alt.Axis(format="%d %b %H:%M")), y=alt.Y("latency_ms:Q", title="Latency (ms)"), color=agent_color(),
                    tooltip=[alt.Tooltip("utcyearmonthdatehoursminutesseconds(ts):T", title="Time (UTC)", format="%d %b %H:%M:%S"), "agent:N", "prompt:N",
                             alt.Tooltip("latency_ms:Q", title="Latency (ms)"), "status:N"])
                st.altair_chart(chart, width="stretch", theme=None)
                q = call_df.groupby("agent_name")["latency_ms"].quantile([0.5, 0.95]).unstack()
                q.columns = ["p50", "p95"]
                st.dataframe(q.map(fmt_ms), width="stretch")
            with b:
                st.markdown("##### Model tokens over time")
                st.caption("From the model invocation log: every model call the agents made, including inner reasoning steps.")
                try:
                    mc = data.model_calls(HOURS, BIN)
                    if mc.empty:
                        st.info("No model calls logged yet (delivery can lag a few minutes).")
                    else:
                        mc["tokens"] = mc["tokens_in"] + mc["tokens_out"]
                        bins = pd.date_range(pd.Timestamp(data.since(HOURS)).floor(f"{BIN}min"),
                                             pd.Timestamp.now(tz="UTC").floor(f"{BIN}min"), freq=f"{BIN}min")
                        full = (mc.set_index(["t", "agent"])[["calls", "tokens_in", "tokens_out", "tokens"]]
                                .reindex(pd.MultiIndex.from_product([bins, list(AGENT_COLORS)], names=["t", "agent"]),
                                         fill_value=0).reset_index())
                        full["period"] = full["t"].dt.strftime("%d %b %H:%M")
                        chart = alt.Chart(full).mark_bar().encode(
                            x=alt.X("period:O", title=None, sort=None, axis=alt.Axis(labelAngle=0, labelOverlap="greedy")),
                            xOffset=alt.XOffset("agent:N", sort=list(AGENT_COLORS)),
                            y=alt.Y("tokens:Q", title="Tokens"), color=agent_color(),
                            tooltip=[alt.Tooltip("period:N", title="Period starting (UTC)"), "agent:N",
                                     alt.Tooltip("calls:Q", title="Model calls"), alt.Tooltip("tokens_in:Q", format=","),
                                     alt.Tooltip("tokens_out:Q", format=",")])
                        st.altair_chart(chart, width="stretch", theme=None)
                        tot = mc.groupby("agent")[["calls", "tokens_in", "tokens_out"]].sum()
                        tot["model calls per agent call"] = (
                            tot["calls"] / call_df.groupby("agent_name").size().reindex(tot.index)).round(1)
                        tot["est. cost"] = data.cost(tot["tokens_in"], tot["tokens_out"]).map(lambda v: f"${v:.3f}")
                        st.dataframe(tot, width="stretch")
                except Exception as e:
                    st.error(f"Invocation log query failed: {e}")

            st.markdown("##### Tool usage")
            a, b = st.columns(2)
            tool_rows = []
            hr_tools = items[items["step_type"] == "orchestrationTrace.invocationInput"]
            for _, it in hr_tools.iterrows():
                fn = json.loads(it["payload"])["orchestrationTrace"]["invocationInput"].get(
                    "actionGroupInvocationInput", {}).get("function")
                if fn:
                    tool_rows.append({"agent": "HR Assistant", "tool": fn})
            tools = pd.DataFrame(tool_rows).value_counts().rename("calls").reset_index() if tool_rows else pd.DataFrame()
            try:
                st_ = data.span_tool_stats(HOURS)
                if not st_.empty:
                    st_["agent"] = "IT Support"
                    st_["tool"] = st_["tool"].str.split("___").str[-1]
                    tools = pd.concat([tools, st_[["agent", "tool", "calls"]]])
            except Exception as e:
                st.warning(f"Span query failed: {e}")
            with a:
                if not tools.empty:
                    chart = alt.Chart(tools).mark_bar(height=18).encode(
                        y=alt.Y("tool:N", title=None, sort="-x"), x=alt.X("calls:Q", title="Tool calls"),
                        color=agent_color(), tooltip=["agent:N", "tool:N", "calls:Q"])
                    st.altair_chart(chart, width="stretch", theme=None)
            with b:
                try:
                    lm = data.lambda_metrics(HOURS)
                    lm_df = pd.DataFrame(lm).T
                    for col in ("invocations", "errors"):
                        lm_df[col] = pd.to_numeric(lm_df[col]).astype(int)
                    for col in ("avg_ms", "max_ms"):
                        lm_df[col] = pd.to_numeric(lm_df[col]).round(1)
                    st.dataframe(lm_df.rename(columns={"avg_ms": "avg duration (ms)",
                                                                    "max_ms": "max duration (ms)"}),
                                 width="stretch")
                    st.caption("Tool Lambdas, from CloudWatch AWS/Lambda metrics.")
                except Exception as e:
                    st.warning(f"Lambda metrics failed: {e}")

# ================================================================== 4. Sessions & traces
with tabs[3]:
    if tabs[3].open:
        if sess_df.empty:
            st.info("No sessions in this time range.")
        else:
            f1, f2, f3 = st.columns(3)
            fa = f1.multiselect("Agent", list(AGENT_COLORS), default=list(AGENT_COLORS))
            fs = f2.multiselect("Scenario", sorted(sess_df["scenario"].unique()))
            only_bad = f3.checkbox("Only sessions with errors or guardrail blocks")
            view = sess_df[sess_df["agent"].isin(fa)]
            if fs:
                view = view[view["scenario"].isin(fs)]
            if only_bad:
                view = view[(view["errors"] > 0) | (view["guardrail"] > 0)]
            st.caption(f"{len(view)} sessions. Select a row to open its trace.")
            pick = st.dataframe(view.drop(columns=["session_id"]).assign(total=view["total_ms"].map(fmt_ms))
                                .drop(columns=["total_ms"]),
                                hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row")
            rows = pick.selection.rows
            linked = st.query_params.get("session")  # deep link: ?session=<id>
            if not rows and linked in set(sess_df["session_id"]):
                srow = sess_df[sess_df["session_id"] == linked].iloc[0]
            elif rows:
                srow = view.iloc[rows[0]]
                st.query_params["session"] = srow["session_id"]
            else:
                srow = None
            if srow is not None:
                sid = srow["session_id"]
                st.divider()
                st.subheader(f"{srow['agent']} · {srow['user']} · {srow['turns']} turn(s)")
                st.caption(f"Session `{sid}`")
                s_items = items[items["session_id"] == sid]
                if srow["agent"] == "HR Assistant":
                    steps = data.classic_steps(s_items)
                    timed = steps.dropna(subset=["start"]).copy() if "start" in steps else pd.DataFrame()
                    if not timed.empty:
                        timed["start"] = pd.to_datetime(timed["start"], utc=True)
                        timed["end"] = pd.to_datetime(timed["end"], utc=True)
                        timed = timed[timed["category"].isin(STEP_COLORS)]
                        timed["step"] = [f"{i + 1:02d} {label}" for i, label in enumerate(timed["label"])]
                        st.markdown("##### Where the time went")
                        chart = alt.Chart(timed).mark_bar(height=14, cornerRadius=3).encode(
                            x=alt.X("start:T", title="Time (UTC)", scale=alt.Scale(type="utc"), axis=alt.Axis(format="%H:%M:%S")), x2="end:T", y=alt.Y("step:N", title=None, sort=None),
                            color=alt.Color("category:N", title=None, scale=alt.Scale(
                                domain=list(STEP_COLORS), range=list(STEP_COLORS.values()))),
                            tooltip=["label:N", alt.Tooltip("ms:Q", title="ms"), "detail:N"])
                        st.altair_chart(chart, width="stretch", height=max(140, 26 * len(timed) + 60), theme=None)
                    st.markdown("##### Step by step")
                    icons = {"User": "🧑", "Answer": "💬", "Model": "🧠", "Reasoning": "💭", "Tool": "🔧",
                             "Guardrail": "🛡️", "Error": "❌"}
                    for _, grp in steps.groupby("invocation_id", sort=False):
                        with st.container(border=True):
                            for _, r in grp.iterrows():
                                ms = f" · {fmt_ms(r['ms'])}" if pd.notna(r.get("ms")) else ""
                                text = f"{icons.get(r['category'], '•')} **{r['label']}**{ms}"
                                if r["category"] == "Error":
                                    st.error(f"{r['label']}: {r['detail']}")
                                elif r["category"] in ("User", "Answer", "Reasoning"):
                                    st.markdown(text)
                                    st.markdown(f"> {r['detail']}" if r["detail"] else "")
                                else:
                                    st.markdown(f"{text}  \n`{short_json(r['detail'], 300)}`")
                    with st.expander("Raw trace events (as returned by InvokeAgent)"):
                        for _, it in s_items[s_items["kind"] == "trace"].iterrows():
                            st.caption(it["step_type"])
                            st.json(json.loads(it["payload"]), expanded=False)
                else:
                    try:
                        sp = data.spans_for_session(sid)
                    except Exception as e:
                        sp = pd.DataFrame()
                        st.error(f"Span query failed: {e}")
                    if sp.empty:
                        st.info("No spans yet for this session. AgentCore spans arrive in aws/spans 1–3 minutes after the call.")
                    else:
                        kind = sp["attributes.aws.genai.span_kind"].fillna("")
                        sp["category"] = kind.map({"LLM": "Model", "TOOL": "Tool"}).fillna("Agent loop")
                        sp = sp[~sp["name"].str.startswith("chat us.") & ~sp["name"].str.startswith("mcp tools/call")]
                        sp["step"] = [f"{i + 1:02d} {n}" for i, n in enumerate(sp["name"])]
                        sp["tokens"] = (pd.to_numeric(sp.get("attributes.gen_ai.usage.input_tokens"), errors="coerce")
                                        .fillna(0) + pd.to_numeric(sp.get("attributes.gen_ai.usage.output_tokens"),
                                                                   errors="coerce").fillna(0))
                        st.markdown("##### Span waterfall (OpenTelemetry, from aws/spans)")
                        chart = alt.Chart(sp).mark_bar(height=14, cornerRadius=3).encode(
                            x=alt.X("start:T", title="Time (UTC)", scale=alt.Scale(type="utc"), axis=alt.Axis(format="%H:%M:%S")), x2="end:T", y=alt.Y("step:N", title=None, sort=None),
                            color=alt.Color("category:N", title=None, scale=alt.Scale(
                                domain=list(STEP_COLORS), range=list(STEP_COLORS.values()))),
                            tooltip=["name:N", alt.Tooltip("ms:Q", title="ms"), alt.Tooltip("tokens:Q", title="tokens"),
                                     alt.Tooltip("status.code:N", title="status")])
                        st.altair_chart(chart, width="stretch", height=max(140, 26 * len(sp) + 60), theme=None)
                        tools = data.spans_for_session(sid)
                        tools = tools[tools["name"].str.startswith("mcp tools/call")]
                        for _, t in tools.iterrows():
                            ok = t.get("status.code") != "ERROR"
                            msg = (f"🔧 **{t['attributes.gen_ai.tool.name']}** · {fmt_ms(t['ms'])}  \n"
                                   f"input `{t.get('attributes.gen_ai.tool.call.arguments')}`  \n"
                                   f"result `{short_json(str(t.get('attributes.gen_ai.tool.call.result')), 300)}`")
                            (st.markdown if ok else st.error)(msg if ok else "Tool call failed  \n" + msg)
                    st.markdown("##### Conversation")
                    for _, r in call_df[call_df["session_id"] == sid].iterrows():
                        with st.container(border=True):
                            st.markdown(f"🧑 **{r['prompt']}**")
                            (st.markdown if r["status"] == "ok" else st.error)(r["answer"])
                            st.caption(f"{fmt_ms(r['latency_ms'])} · {int(r['input_tokens'] + r['output_tokens']):,} tokens")

# ================================================================== 5. Audit trail
with tabs[4]:
    if tabs[4].open:
        st.caption("Every invocation of the demo agents and guardrail, recorded by CloudTrail data events: "
                   "who called, from where, when, and whether it failed. Model-call detail is in the invocation log.")
        try:
            au = data.audit_events(HOURS)
        except Exception as e:
            au = pd.DataFrame()
            st.error(f"CloudTrail query failed: {e}")
        if au.empty:
            st.info("No CloudTrail events in this range (delivery to CloudWatch Logs takes ~5 minutes).")
        else:
            f1, f2 = st.columns(2)
            ev = f1.multiselect("Event", sorted(au["eventName"].unique()), default=sorted(au["eventName"].unique()))
            who = f2.multiselect("Caller", sorted(au["who"].unique()))
            v = au[au["eventName"].isin(ev)]
            if who:
                v = v[v["who"].isin(who)]
            m = st.columns(4)
            m[0].metric("Events", len(v))
            m[1].metric("Distinct callers", v["who"].nunique())
            m[2].metric("Call sources", v["sourceIPAddress"].nunique(), help="Source IPs, or the AWS service that made the call")
            m[3].metric("Failed calls", int(v["errorCode"].notna().sum()))
            st.dataframe(v[["eventTime", "eventName", "agent", "who", "caller_type", "sourceIPAddress", "session_id",
                            "errorCode", "userAgent"]].rename(columns={"eventTime": "time", "who": "caller",
                                                                       "sourceIPAddress": "source IP"}),
                         hide_index=True, width="stretch")
            st.caption("Calls made by the agents themselves (model and guardrail calls under the agent's role) show "
                       "the agent's service role as the caller.")

# ================================================================== 6. Guardrails & errors
with tabs[5]:
    if tabs[5].open:
        st.subheader("Guardrail interventions")
        ge = data.guardrail_events(items)
        if ge.empty:
            st.info("No guardrail interventions in this range.")
        else:
            a, b = st.columns([1, 2])
            with a:
                byp = ge.groupby(["policy", "stage"]).size().rename("count").reset_index()
                chart = alt.Chart(byp).mark_bar(height=18).encode(
                    y=alt.Y("policy:N", title=None, sort="-x"), x=alt.X("count:Q", title="Interventions"),
                    color=alt.Color("stage:N", title=None, scale=alt.Scale(domain=["Input", "Output"],
                                                                           range=["#2a78d6", "#eb6834"])),
                    tooltip=["policy:N", "stage:N", "count:Q"])
                st.altair_chart(chart, width="stretch", theme=None)
            with b:
                st.dataframe(ge[["time", "stage", "policy", "match", "prompt", "user"]], hide_index=True,
                             width="stretch")
            st.caption("Input-stage blocks stop the request before the model runs; output-stage interventions mask or "
                       "block the answer. Review matches for false positives (e.g. a legitimate question blocked).")
        st.caption("IT Support has no guardrail attached: off-topic and unsafe requests rely on its system prompt alone.")

        st.subheader("Errors")
        err_rows = []
        if not call_df.empty:
            for _, r in call_df[call_df["status"] == "error"].iterrows():
                err_rows.append({"time": r["ts"], "agent": r["agent_name"], "where": "Agent call (returned to caller)",
                                 "detail": r["answer"][:300], "prompt": r["prompt"], "session_id": r["session_id"]})
            ft = items[items["step_type"].str.startswith("failureTrace")]
            for _, it in ft.iterrows():
                node = json.loads(it["payload"])["failureTrace"]
                err_rows.append({"time": it["ts"], "agent": it["agent_name"], "where": "Classic trace: failureTrace",
                                 "detail": node.get("failureReason", "")[:300], "prompt": "", "session_id": it["session_id"]})
        try:
            se = data.span_errors(HOURS)
            for _, r in se.iterrows():
                err_rows.append({"time": r["time"], "agent": "IT Support", "where": f"Span error: {r['name']}",
                                 "detail": f"args {r.get('arguments')} → {short_json(str(r.get('result')), 200)}",
                                 "prompt": "", "session_id": r.get("session_id")})
        except Exception as e:
            st.warning(f"Span query failed: {e}")
        try:
            le = data.lambda_error_logs(HOURS)
            for _, r in le.iterrows():
                err_rows.append({"time": r["time"], "agent": r["agent"], "where": f"Lambda log: {r['function']}",
                                 "detail": r["message"], "prompt": "", "session_id": ""})
        except Exception as e:
            st.warning(f"Lambda log query failed: {e}")
        if not err_rows:
            st.success("No errors in this range.")
        else:
            er = pd.DataFrame(err_rows).sort_values("time", ascending=False)
            m = st.columns(len(er["where"].map(lambda w: w.split(":")[0]).unique()))
            for col, (k, n) in zip(m, er["where"].map(lambda w: w.split(":")[0]).value_counts().items(), strict=True):
                col.metric(f"❌ {k}", n)
            st.dataframe(er, hide_index=True, width="stretch")
            st.caption("The same failure shows up in several layers: the Lambda raises, the agent records it in its "
                       "trace/span, and (classic) the caller receives an exception. AgentCore recovered and answered.")

# ================================================================== 7. Live test
with tabs[6]:
    if tabs[6].open:
        st.caption("Send a question to an agent through the same wrapper the traffic generator uses. The call is "
                   "recorded like any other and appears in the other tabs after Refresh.")
        agent_key = st.radio("Agent", list(AGENTS), format_func=lambda k: AGENTS[k]["name"], horizontal=True)
        hist_key, sid_key = f"chat_{agent_key}", f"sid_{agent_key}"
        st.session_state.setdefault(hist_key, [])
        if st.button("New conversation"):
            st.session_state[hist_key] = []
            st.session_state.pop(sid_key, None)
        for msg in st.session_state[hist_key]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["text"])
                if msg.get("meta"):
                    st.caption(msg["meta"])
        prompt = st.chat_input(f"Ask {AGENTS[agent_key]['name']}…")
        if prompt:
            st.session_state[hist_key].append({"role": "user", "text": prompt})
            with st.spinner("Calling the agent…"):
                r = invoke(agent_key, prompt, session_id=st.session_state.get(sid_key), user="live-demo", scenario="live")
            st.session_state[sid_key] = r["session_id"]
            if r["ok"]:
                gr = " · 🛡️ guardrail intervened" if r.get("guardrail_action") == "INTERVENED" else ""
                meta = (f"{fmt_ms(r['latency_ms'])} · {r['input_tokens'] + r['output_tokens']:,} tokens · "
                        f"{len(r['steps'])} trace steps{gr} · session {r['session_id'][:16]}…")
                st.session_state[hist_key].append({"role": "assistant", "text": r["answer"] or "(empty answer)", "meta": meta})
            else:
                st.session_state[hist_key].append({"role": "assistant", "text": f"❌ {r['error']}",
                                                   "meta": f"{fmt_ms(r['latency_ms'])} · recorded as an error"})
            data.trace_items.clear()  # only the new call changed; keep the rest warm
            st.rerun()
