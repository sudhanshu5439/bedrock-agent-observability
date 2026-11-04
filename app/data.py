"""Data access for the dashboard. Every function reads one AWS source and returns plain rows / DataFrames.

Sources
  DynamoDB trace table        classic-agent traces + a request/response index for both agents (written by invoke.py)
  Model invocation log        every model call with caller role and tokens (filtered to bedrock-demo-* roles)
  CloudTrail (demo trail)     who invoked which agent, from where (data events)
  CloudTrail event history    who created or changed the agents (management events, 90 days, read-only lookup)
  aws/spans                   AgentCore OpenTelemetry spans (Transaction Search)
  Lambda / agent control APIs configuration and tool health

Only the demo's own agents are shown; the account is shared, so every query is filtered to them.
"""
import copy
import functools
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, UTC

import pandas as pd
from boto3.dynamodb.conditions import Key

from invoke import AGENTS, OUTPUTS, _s as SESSION  # isort: skip  (puts infra/ on sys.path for config)
from config import CLOUDTRAIL_LOG_GROUP, INVOCATION_LOG_GROUP, MODEL_ID, PROFILE, TRACE_TABLE, account_id  # noqa: F401  # isort: skip
from traces import (  # noqa: F401  (re-exported for the dashboard)
    caller_name, calls, classic_steps, cost, guardrail_events, guardrail_matches, scrub, sessions,
)

SPANS_LOG_GROUP = "aws/spans"
HR, IT = AGENTS["hr"], AGENTS["it"]
HARNESS_NAME = "bedrock_demo_it_support"
# Matches our three resources in CloudTrail records (agent id, harness runtime, guardrail id).
OUR_RESOURCES = f"/{HR['agent_id']}|{HARNESS_NAME}|{OUTPUTS['guardrail']['id']}/"
ROLE_TO_AGENT = {"bedrock-demo-hr-agent-role": HR["name"], "bedrock-demo-harness-role": IT["name"]}
LAMBDAS = {HR["name"]: "bedrock-demo-hr-tools", IT["name"]: "bedrock-demo-it-tools"}

# DASHBOARD_REDACT=1 masks the account id, emails, IPs and DASHBOARD_REDACT_NAMES (comma-separated)
# in everything the dashboard shows; used for public screenshots.
REDACT = os.environ.get("DASHBOARD_REDACT") == "1"
REDACT_NAMES = tuple(n.strip() for n in os.environ.get("DASHBOARD_REDACT_NAMES", "").split(",") if n.strip())

logs = SESSION.client("logs")
ddb = SESSION.resource("dynamodb").Table(TRACE_TABLE)
cw = SESSION.client("cloudwatch")
ct = SESSION.client("cloudtrail")
agent_ctl = SESSION.client("bedrock-agent")
bedrock = SESSION.client("bedrock")
ac_ctl = SESSION.client("bedrock-agentcore-control")
lam = SESSION.client("lambda")



def ttl_cache(seconds):
    """Process-wide cache shared by every browser session and by the background warmer.

    Unlike st.cache_data it works from plain threads. One lock per key, so a value is fetched once even
    when several threads ask at the same time. Callers get a copy, so they can't alter the cached value.
    """
    def deco(fn):
        store, locks, guard = {}, {}, threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args):
            with guard:
                lock = locks.setdefault(args, threading.Lock())
            with lock:
                hit = store.get(args)
                if hit is None or time.time() - hit[0] > seconds:
                    hit = (time.time(), fn(*args))
                    store[args] = hit
            value = hit[1]
            return value.copy() if isinstance(value, pd.DataFrame) else copy.deepcopy(value)

        wrapper.clear = store.clear
        _CACHES.append(store)
        return wrapper
    return deco


_CACHES = []


def clear_all():
    for store in _CACHES:
        store.clear()


def public(fn):
    """Apply redaction to a data function's result when DASHBOARD_REDACT=1."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        return scrub(out, account_id(), REDACT_NAMES) if REDACT else out
    return wrapper


def redact_text(text):
    return scrub(text, account_id(), REDACT_NAMES) if REDACT else text


@ttl_cache(300)
def caller_account():
    return SESSION.client("sts").get_caller_identity()["Account"]


def _bin(minutes):
    """Logs Insights bin() size: minutes above 59 must be written in hours (bin(6h), not bin(360m))."""
    return f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes}m"


def since(hours):
    return datetime.now(UTC) - timedelta(hours=hours)


def insights(log_group, query, hours, limit=10000):
    """Run a CloudWatch Logs Insights query and return a list of dicts."""
    end = int(time.time())
    qid = logs.start_query(logGroupName=log_group, startTime=end - int(hours * 3600), endTime=end,
                           queryString=query, limit=limit)["queryId"]
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] not in ("Scheduled", "Running"):
            break
        time.sleep(1)
    return [{f["field"]: f["value"] for f in row if not f["field"].startswith("@ptr")} for row in r["results"]]


# ------------------------------------------------------------------ DynamoDB trace table
@ttl_cache(300)
@public
def trace_items(hours):
    """All items for both agents in the window, via the agent_id GSI."""
    start = since(hours).isoformat(timespec="milliseconds")
    rows = []
    for agent in AGENTS.values():
        kw = {"IndexName": "agent_id-event_ts",
              "KeyConditionExpression": Key("agent_id").eq(agent["agent_id"]) & Key("event_ts").gte(start)}
        while True:
            r = ddb.query(**kw)
            rows += r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["event_ts"].str.split("#").str[0], utc=True)
    for col in ("latency_ms", "input_tokens", "output_tokens"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    return df.sort_values("event_ts")


# ------------------------------------------------------------------ AgentCore spans
SPAN_COLUMNS = ["status.code", "attributes.aws.genai.span_kind", "attributes.gen_ai.usage.input_tokens",
                "attributes.gen_ai.usage.output_tokens", "attributes.gen_ai.tool.name",
                "attributes.gen_ai.tool.call.arguments", "attributes.gen_ai.tool.call.result"]


@ttl_cache(300)
@public
def spans_for_session(session_id, hours=168):
    q = (f"filter attributes.session.id = '{session_id}' "
         "| fields name, kind, startTimeUnixNano, endTimeUnixNano, durationNano, status.code, spanId, parentSpanId, "
         "attributes.aws.genai.span_kind, attributes.gen_ai.usage.input_tokens, attributes.gen_ai.usage.output_tokens, "
         "attributes.gen_ai.tool.name, attributes.gen_ai.tool.call.arguments, attributes.gen_ai.tool.call.result, "
         "attributes.gen_ai.tool.status, attributes.gen_ai.response.finish_reasons "
         "| sort startTimeUnixNano asc | limit 500")
    df = pd.DataFrame(insights(SPANS_LOG_GROUP, q, hours))
    if df.empty:
        return df
    for col in SPAN_COLUMNS:
        if col not in df:
            df[col] = None
    df["start"] = pd.to_datetime(pd.to_numeric(df["startTimeUnixNano"]), unit="ns", utc=True)
    df["end"] = pd.to_datetime(pd.to_numeric(df["endTimeUnixNano"]), unit="ns", utc=True)
    df["ms"] = (pd.to_numeric(df["durationNano"]) / 1e6).round(0)
    return df


@ttl_cache(300)
@public
def span_tool_stats(hours):
    q = (f"filter attributes.harness.id = '{OUTPUTS['it_harness']['harness_id']}' and name like /^mcp tools.call/ "
         "| stats count(*) as n, avg(durationNano) as avg_ns by attributes.gen_ai.tool.name as tool, status.code as status")
    df = pd.DataFrame(insights(SPANS_LOG_GROUP, q, hours))
    if df.empty:
        return df
    df["n"] = pd.to_numeric(df["n"])
    df["ns"] = pd.to_numeric(df["avg_ns"]) * df["n"]
    g = df.groupby("tool")
    return pd.DataFrame({"calls": g["n"].sum(),
                         "errors": df[df["status"] == "ERROR"].groupby("tool")["n"].sum(),
                         "avg_ms": (g["ns"].sum() / g["n"].sum() / 1e6).round(0)}).fillna(0).reset_index()


@ttl_cache(300)
@public
def span_errors(hours):
    q = (f"filter attributes.harness.id = '{OUTPUTS['it_harness']['harness_id']}' and status.code = 'ERROR' and name like /^mcp tools.call/ "
         "| fields startTimeUnixNano, name, attributes.session.id as session_id, "
         "attributes.gen_ai.tool.call.arguments as arguments, attributes.gen_ai.tool.call.result as result "
         "| sort startTimeUnixNano desc | limit 200")
    df = pd.DataFrame(insights(SPANS_LOG_GROUP, q, hours))
    if not df.empty:
        df = df.reindex(columns=["startTimeUnixNano", "name", "session_id", "arguments", "result"])
        df["time"] = pd.to_datetime(pd.to_numeric(df.pop("startTimeUnixNano")), unit="ns", utc=True)
    return df


@ttl_cache(900)
@public
def harness_declared_tools(hours=168):
    q = (f"filter attributes.harness.id = '{OUTPUTS['it_harness']['harness_id']}' and name like /^invoke_agent/ "
         "| fields attributes.gen_ai.agent.tools as tools | sort startTimeUnixNano desc | limit 1")
    rows = insights(SPANS_LOG_GROUP, q, hours)
    return json.loads(rows[0]["tools"]) if rows and rows[0].get("tools") else []


# ------------------------------------------------------------------ model invocation log
@ttl_cache(300)
@public
def model_calls(hours, bin_minutes):
    q = ("filter identity.arn like /bedrock-demo-/ "
         "| parse identity.arn 'assumed-role/*/*' as role, role_session "
         f"| stats count(*) as calls, sum(input.inputTokenCount) as tokens_in, "
         f"sum(output.outputTokenCount) as tokens_out by bin({_bin(bin_minutes)}) as t, role")
    df = pd.DataFrame(insights(INVOCATION_LOG_GROUP, q, hours))
    if df.empty:
        return df
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df["agent"] = df["role"].map(ROLE_TO_AGENT).fillna(df["role"])
    for c in ("calls", "tokens_in", "tokens_out"):
        df[c] = pd.to_numeric(df[c])
    return df


# ------------------------------------------------------------------ CloudTrail
AUDIT_COLUMNS = ["eventTime", "eventName", "eventSource", "caller", "caller_type", "sso_user_id", "sourceIPAddress",
                 "userAgent", "session_id", "resource", "errorCode", "errorMessage"]


@ttl_cache(300)
@public
def audit_events(hours):
    q = (f"filter @message like {OUR_RESOURCES} "
         "| fields eventTime, eventName, eventSource, userIdentity.arn as caller, userIdentity.type as caller_type, "
         "userIdentity.onBehalfOf.userId as sso_user_id, sourceIPAddress, userAgent, "
         "requestParameters.sessionId as session_id, resources.0.ARN as resource, errorCode, errorMessage "
         "| sort eventTime desc | limit 2000")
    df = pd.DataFrame(insights(CLOUDTRAIL_LOG_GROUP, q, hours))
    if df.empty:
        return df
    df = df.reindex(columns=AUDIT_COLUMNS)  # Insights drops fields that are empty in every row
    df["eventTime"] = pd.to_datetime(df["eventTime"], utc=True)
    df["who"] = df["caller"].fillna("").map(caller_name)
    df["agent"] = df["resource"].fillna("").map(
        lambda a: HR["name"] if HR["agent_id"] in a else IT["name"] if HARNESS_NAME in a else
        "HR guardrail" if OUTPUTS["guardrail"]["id"] in a else a)
    return df


# Bedrock / AgentCore write events carry no resource names in event history, so they are looked up
# by event name and matched on our ids inside the event body.
CONFIG_EVENTS = ["CreateAgent", "UpdateAgent", "PrepareAgent", "DeleteAgent", "CreateAgentAlias", "UpdateAgentAlias",
                 "CreateAgentActionGroup", "UpdateAgentActionGroup", "DeleteAgentActionGroup", "CreateGuardrail",
                 "UpdateGuardrail", "CreateGuardrailVersion", "DeleteGuardrail", "CreateHarness", "UpdateHarness",
                 "DeleteHarness", "CreateGateway", "UpdateGateway", "CreateGatewayTarget", "UpdateGatewayTarget"]


def _lookup(attr, value, start, pages=4):
    kw = {"LookupAttributes": [{"AttributeKey": attr, "AttributeValue": value}], "StartTime": start, "MaxResults": 50}
    for _ in range(pages):
        r = ct.lookup_events(**kw)
        yield from r["Events"]
        if "NextToken" not in r:
            return
        kw["NextToken"] = r["NextToken"]
        time.sleep(0.5)  # lookup_events allows 2 requests per second


@ttl_cache(900)
@public
def config_changes(days=30):
    """Who created or changed the demo resources: write management events from CloudTrail event history."""
    ids = {HR["agent_id"]: HR["name"], "bedrock_demo_hr_assistant": HR["name"],
           OUTPUTS["guardrail"]["id"]: "HR guardrail", OUTPUTS["it_harness"]["harness_id"]: IT["name"],
           HARNESS_NAME: IT["name"], OUTPUTS["it_gateway"]["gateway_id"]: "IT gateway",
           LAMBDAS[HR["name"]]: "hr-tools Lambda", LAMBDAS[IT["name"]]: "it-tools Lambda"}
    start = since(days * 24)
    events = [e for name in CONFIG_EVENTS for e in _lookup("EventName", name, start, pages=2)]
    for fn in LAMBDAS.values():
        events += list(_lookup("ResourceName", fn, start))
    rows, seen = [], set()
    for e in events:
        if e["EventId"] in seen:
            continue
        seen.add(e["EventId"])
        raw = e["CloudTrailEvent"]
        body = json.loads(raw)
        if body.get("readOnly", True):
            continue
        hit = next((label for key, label in ids.items() if key in raw), None)
        if not hit:
            continue  # someone else's resource in the shared account
        rows.append({"time": e["EventTime"], "resource": hit, "event": e["EventName"].replace("20150331v2", "")
                     .replace("20150331", ""), "who": (body.get("userIdentity", {}).get("arn") or "").split("/")[-1],
                     "source IP": body.get("sourceIPAddress", ""), "error": body.get("errorCode", "")})
    df = pd.DataFrame(rows)
    return df.sort_values("time", ascending=False) if not df.empty else df


# ------------------------------------------------------------------ Lambda
@ttl_cache(300)
@public
def lambda_metrics(hours):
    end = datetime.now(UTC)
    queries = []
    for i, (agent, fn) in enumerate(LAMBDAS.items()):
        for stat_name, metric, stat in (("invocations", "Invocations", "Sum"), ("errors", "Errors", "Sum"),
                                        ("avg_ms", "Duration", "Average"), ("max_ms", "Duration", "Maximum")):
            queries.append({"Id": f"m{i}_{stat_name}", "Label": f"{agent}|{stat_name}", "ReturnData": True,
                            "MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": metric,
                                                      "Dimensions": [{"Name": "FunctionName", "Value": fn}]},
                                           "Period": int(hours * 3600), "Stat": stat}})
    r = cw.get_metric_data(MetricDataQueries=queries, StartTime=end - timedelta(hours=hours), EndTime=end)
    out = {a: {"function": fn} for a, fn in LAMBDAS.items()}
    for res in r["MetricDataResults"]:
        agent, name = res["Label"].split("|")
        vals = res["Values"]
        out[agent][name] = (sum(vals) if name in ("invocations", "errors") else max(vals)) if vals else 0
    return out


@ttl_cache(300)
@public
def lambda_error_logs(hours):
    rows = []
    for agent, fn in LAMBDAS.items():
        events, kw = [], {}
        try:
            for _ in range(10):
                r = logs.filter_log_events(logGroupName=f"/aws/lambda/{fn}", filterPattern='?"[ERROR]" ?"Task timed out"',
                                           startTime=int(since(hours).timestamp() * 1000), **kw)
                events += r["events"]
                if "nextToken" not in r:
                    break
                kw = {"nextToken": r["nextToken"]}
        except logs.exceptions.ResourceNotFoundException:
            continue
        for e in events:
            rows.append({"time": pd.to_datetime(e["timestamp"], unit="ms", utc=True), "agent": agent, "function": fn,
                         "message": e["message"].strip()[:400]})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ configuration
@ttl_cache(600)
@public
def classic_config():
    a = OUTPUTS["hr_agent"]
    agent = agent_ctl.get_agent(agentId=a["agent_id"])["agent"]
    alias = agent_ctl.get_agent_alias(agentId=a["agent_id"], agentAliasId=a["alias_id"])["agentAlias"]
    versions = agent_ctl.list_agent_versions(agentId=a["agent_id"])["agentVersionSummaries"]
    groups = []
    for g in agent_ctl.list_agent_action_groups(agentId=a["agent_id"], agentVersion="DRAFT")["actionGroupSummaries"]:
        groups.append(agent_ctl.get_agent_action_group(agentId=a["agent_id"], agentVersion="DRAFT",
                                                       actionGroupId=g["actionGroupId"])["agentActionGroup"])
    g = OUTPUTS["guardrail"]
    guardrail = bedrock.get_guardrail(guardrailIdentifier=g["id"], guardrailVersion=g["version"])
    return {"agent": agent, "alias": alias, "versions": versions, "action_groups": groups, "guardrail": guardrail,
            "lambda": lam.get_function_configuration(FunctionName=LAMBDAS[HR["name"]])}


@ttl_cache(600)
@public
def agentcore_config():
    h = ac_ctl.get_harness(harnessId=OUTPUTS["it_harness"]["harness_id"])["harness"]
    gid = OUTPUTS["it_gateway"]["gateway_id"]
    gw = ac_ctl.get_gateway(gatewayIdentifier=gid)
    target = ac_ctl.get_gateway_target(gatewayIdentifier=gid, targetId=OUTPUTS["it_gateway"]["target_id"])
    return {"harness": h, "gateway": gw, "target": target,
            "lambda": lam.get_function_configuration(FunctionName=LAMBDAS[IT["name"]])}


MODEL_LABEL = MODEL_ID


# ------------------------------------------------------------------ speed: parallel prefetch + keep-warm
WARM_EVERY_S = 240     # refresh before the 300 s caches expire
WARM_WHILE_S = 1800    # keep refreshing only while someone opened the dashboard in the last 30 minutes


def _jobs(hours, bin_minutes):
    return [(trace_items, (hours,)), (classic_config, ()), (agentcore_config, ()), (harness_declared_tools, ()),
            (config_changes, ()), (model_calls, (hours, bin_minutes)), (span_tool_stats, (hours,)),
            (lambda_metrics, (hours,)), (audit_events, (hours,)), (span_errors, (hours,)), (lambda_error_logs, (hours,))]


def _safe(fn, args):
    try:
        fn(*args)
    except Exception:  # noqa: S110  (the tab that needs it calls again and shows the error)
        pass


_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="prefetch")


def prefetch(hours, bin_minutes, wait=False):
    """Fetch every source in parallel: a cold load costs the slowest call, not the sum (≈34 s sequential).

    Without wait, it returns at once; each tab then blocks only on its own data (per-key lock in ttl_cache),
    so the first tab renders while the others are still loading.
    """
    futures = [_POOL.submit(_safe, fn, args) for fn, args in _jobs(hours, bin_minutes)]
    if wait:
        for f in futures:
            f.result()


_WARM_SEEN = {}
_WARM_STARTED = threading.Event()
_WARM_LOCK = threading.Lock()


def _warmer():
    """One background thread per server: re-fills the caches for recently viewed time ranges."""
    seen = _WARM_SEEN
    with _WARM_LOCK:
        if _WARM_STARTED.is_set():
            return seen
        _WARM_STARTED.set()

    def loop():
        while True:
            time.sleep(WARM_EVERY_S)
            now = time.time()
            for (hours, bin_minutes), last in list(seen.items()):
                if now - last < WARM_WHILE_S:
                    prefetch(hours, bin_minutes, wait=True)

    threading.Thread(target=loop, name="cache-warmer", daemon=True).start()
    return seen


def keep_warm(hours, bin_minutes):
    _warmer()[(hours, bin_minutes)] = time.time()


# Warm the default view (last 7 days) as soon as the server starts, so the first visitor doesn't wait.
if os.environ.get("DASHBOARD_WARM_ON_START", "1") == "1":
    _POOL.submit(_safe, caller_account, ())
    prefetch(168, 360)
    keep_warm(168, 360)
