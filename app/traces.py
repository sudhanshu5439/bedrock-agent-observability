"""Pure functions over recorded agent data: no AWS calls, so they are unit-tested in CI.

  calls / sessions        one row per agent call / per session, from DynamoDB trace items
  classic_steps           a classic agent's InvokeAgent trace flattened into timeline rows
  guardrail_matches       what a guardrail assessment matched, in plain words
  guardrail_events        every guardrail intervention with the prompt that caused it
  caller_name             a readable caller from a CloudTrail identity ARN
  scrub                   redaction for public screenshots (DASHBOARD_REDACT=1)
"""
import json
import re

import pandas as pd

# Claude Haiku 4.5 list price on Bedrock (USD per million tokens). Used for estimates only.
PRICE_IN, PRICE_OUT = 1.00, 5.00


def cost(tokens_in, tokens_out):
    return tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT


def calls(items):
    """One row per agent call (response or error item), joined to its prompt."""
    if items.empty:
        return pd.DataFrame()
    done = items[items["kind"].isin(["response", "error"])].copy()
    prompts = items[items["kind"] == "request"].set_index("invocation_id")["payload"].map(
        lambda p: json.loads(p).get("prompt", ""))
    done["prompt"] = done["invocation_id"].map(prompts)
    done["status"] = done.get("status", pd.Series(index=done.index, dtype=object)).fillna("ok")
    done["answer"] = done.apply(
        lambda r: json.loads(r["payload"]).get("answer" if r["kind"] == "response" else "error", ""), axis=1)
    done["input_tokens"] = done["input_tokens"].fillna(0)
    done["output_tokens"] = done["output_tokens"].fillna(0)
    done["cost_usd"] = cost(done["input_tokens"], done["output_tokens"])
    done["guardrail_action"] = done.get("guardrail_action", pd.Series(index=done.index, dtype=object)).fillna("")
    return done


def sessions(call_df):
    if call_df.empty:
        return pd.DataFrame()
    g = call_df.groupby("session_id")
    out = pd.DataFrame({
        "started": g["ts"].min(),
        "agent": g["agent_name"].first(),
        "user": g["user"].first(),
        "scenario": g["scenario"].first(),
        "turns": g.size(),
        "errors": g["status"].apply(lambda s: int((s == "error").sum())),
        "guardrail": g["guardrail_action"].apply(lambda s: int((s == "INTERVENED").sum())),
        "total_ms": g["latency_ms"].sum(),
        "tokens": g["input_tokens"].sum() + g["output_tokens"].sum(),
        "first_prompt": g["prompt"].first(),
    }).reset_index()
    return out.sort_values("started", ascending=False)


def classic_steps(session_items):
    """Flatten classic trace events of one session into timeline rows."""
    rows = []
    for _, it in session_items.iterrows():
        p = json.loads(it["payload"]) if it["payload"] else {}
        base = {"invocation_id": it["invocation_id"], "ts": it["ts"]}
        if it["kind"] == "request":
            rows.append({**base, "category": "User", "label": "User question", "detail": p.get("prompt", "")})
            continue
        if it["kind"] == "response":
            rows.append({**base, "category": "Answer", "label": "Final answer", "detail": p.get("answer", ""),
                         "ms": it.get("latency_ms")})
            continue
        if it["kind"] == "error":
            rows.append({**base, "category": "Error", "label": f"Error: {it['step_type']}", "detail": p.get("error", "")})
            continue
        outer, sub = (it["step_type"].split(".") + [""])[:2]
        node = p.get(outer, {})
        inner = node.get(sub, {}) if isinstance(node, dict) else {}
        meta = (inner.get("metadata") if isinstance(inner, dict) else None) or node.get("metadata") or {}
        row = {**base, "ms": meta.get("totalTimeMs"), "start": meta.get("startTime"), "end": meta.get("endTime")}
        if outer == "guardrailTrace":
            stage = "input" if "-pre-" in node.get("traceId", "") or "inputAssessments" in node else "output"
            matched = guardrail_matches(node)
            rows.append({**row, "category": "Guardrail", "label": f"Guardrail {stage} check: {node.get('action')}",
                         "detail": "; ".join(matched) or "no policy matched"})
        elif sub == "modelInvocationOutput":
            u = meta.get("usage", {})
            rows.append({**row, "category": "Model", "label": "Model call",
                         "detail": f"{u.get('inputTokens', 0)} in / {u.get('outputTokens', 0)} out tokens",
                         "tokens": u.get("inputTokens", 0) + u.get("outputTokens", 0)})
        elif sub == "rationale":
            rows.append({**row, "category": "Reasoning", "label": "Reasoning", "detail": inner.get("text", "")})
        elif sub == "invocationInput":
            ag = inner.get("actionGroupInvocationInput", {})
            params = {x["name"]: x.get("value") for x in ag.get("parameters", [])}
            rows.append({**row, "category": "Tool", "label": f"Tool call: {ag.get('function', inner.get('invocationType'))}",
                         "detail": json.dumps(params)})
        elif sub == "observation":
            if "actionGroupInvocationOutput" in inner:
                out = inner["actionGroupInvocationOutput"]
                m = out.get("metadata", {})
                rows.append({**row, "ms": m.get("totalTimeMs"), "start": m.get("startTime"), "end": m.get("endTime"),
                             "category": "Tool", "label": "Tool result", "detail": out.get("text", "")})
        elif outer == "failureTrace":
            rows.append({**row, "category": "Error", "label": f"Failure ({node.get('failureCode')})",
                         "detail": node.get("failureReason", "")})
    return pd.DataFrame(rows)


def guardrail_matches(node):
    """Human-readable list of what a guardrail assessment matched."""
    found = []
    for a in (node.get("inputAssessments") or []) + (node.get("outputAssessments") or []):
        for t in (a.get("topicPolicy") or {}).get("topics", []):
            found.append(f"denied topic '{t.get('name')}' ({t.get('action')})")
        for f in (a.get("contentPolicy") or {}).get("filters", []):
            found.append(f"content filter {f.get('type')} {f.get('confidence', '')} ({f.get('action')})")
        sip = a.get("sensitiveInformationPolicy") or {}
        for e in sip.get("piiEntities", []):
            found.append(f"PII {e.get('type')} '{e.get('match')}' ({e.get('action')})")
        for r in sip.get("regexes", []):
            found.append(f"regex {r.get('name')} ({r.get('action')})")
        for w in (a.get("wordPolicy") or {}).get("customWords", []):
            found.append(f"word '{w.get('match')}' ({w.get('action')})")
    return found


POLICY_KIND = {"PII": "PII", "content": "Content filter", "regex": "Regex", "word": "Word", "denied": "Denied topic"}


def guardrail_events(items):
    """Every guardrail check that intervened, with the prompt that triggered it."""
    if items.empty:
        return pd.DataFrame()
    g = items[items["step_type"] == "guardrailTrace.action"]
    prompts = items[items["kind"] == "request"].set_index("invocation_id")["payload"].map(
        lambda p: json.loads(p).get("prompt", ""))
    rows = []
    for _, it in g.iterrows():
        node = json.loads(it["payload"])["guardrailTrace"]
        if node.get("action") != "INTERVENED":
            continue
        stage = "Input" if "-pre-" in node.get("traceId", "") else "Output"
        for m in guardrail_matches(node) or ["(no detail)"]:
            rows.append({"time": it["ts"], "agent": it["agent_name"], "user": it["user"], "stage": stage,
                         "policy": POLICY_KIND.get(m.split(" ")[0], m.split(" ")[0]), "match": m,
                         "prompt": prompts.get(it["invocation_id"], ""), "session_id": it["session_id"]})
    return pd.DataFrame(rows)


def caller_name(arn):
    """People by their SSO user name; services by role name (their session names are random per call)."""
    parts = arn.split("/")
    if ":assumed-role/" in arn and len(parts) >= 3:
        return parts[2] if parts[1].startswith("AWSReservedSSO") else f"role/{parts[1]}"
    return parts[-1]


# ------------------------------------------------------------------ redaction for public screenshots
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def scrub(obj, account_id="", names=()):
    """Mask an account id, real emails, IP addresses and the given names; recurse into containers.

    Addresses on the reserved .example domain are kept: they are demo data, not people.
    """
    if isinstance(obj, str):
        s = obj.replace(account_id, "123456789012") if account_id else obj
        for name in names:
            s = re.sub(re.escape(name), lambda m: "Acme" if m.group(0)[0].isupper() else "acme", s, flags=re.I)
        s = EMAIL.sub(lambda m: m.group(0) if m.group(0).endswith(".example") else "user@example.com", s)
        return IPV4.sub(lambda m: m.group(0) if m.group(0) == "127.0.0.1" else "203.0.113.10", s)
    if isinstance(obj, dict):
        return {k: scrub(v, account_id, names) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub(v, account_id, names) for v in obj)
    if isinstance(obj, pd.DataFrame):
        out = obj.copy()
        for col in out.columns:
            if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(out[col]):
                out[col] = out[col].map(lambda v: scrub(v, account_id, names))
        return out
    return obj
