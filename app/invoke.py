"""Invoke wrapper for the two demo agents.

Every call is recorded in the DynamoDB trace table, one item per event:
  request  -> what was asked, by whom
  trace    -> each classic-agent trace event (enableTrace=true), or each AgentCore tool call / usage event
  response -> the final answer, latency, token usage
  error    -> any exception, so failures show up in the dashboard

Classic agents need this because AWS does not keep their traces anywhere. For the AgentCore
harness the authoritative step record is the OpenTelemetry spans in aws/spans; the items written
here are a lightweight index so both agents appear in one session list.
"""
import json
import sys
import time
import uuid
from datetime import datetime, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "infra"))

from botocore.config import Config  # noqa: E402

from config import TRACE_TABLE, session  # noqa: E402

OUTPUTS = json.loads((ROOT / "infra" / "outputs.json").read_text())
TTL_DAYS = 30
MAX_PAYLOAD = 300_000  # stay under the 400 KB DynamoDB item limit

_s = session()
_retry = Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=180)
_agent_rt = _s.client("bedrock-agent-runtime", config=_retry)
_ac_rt = _s.client("bedrock-agentcore", config=_retry)
_table = _s.resource("dynamodb").Table(TRACE_TABLE)

AGENTS = {
    "hr": {"type": "classic", "name": "HR Assistant", "agent_id": OUTPUTS["hr_agent"]["agent_id"]},
    "it": {"type": "agentcore", "name": "IT Support", "agent_id": OUTPUTS["it_harness"]["harness_id"]},
}


class _Recorder:
    """Writes the events of one invocation, ordered by a per-session sequence in the sort key."""

    def __init__(self, agent_key, session_id, user, scenario):
        self.agent = AGENTS[agent_key]
        self.session_id = session_id
        self.invocation_id = str(uuid.uuid4())
        self.user = user
        self.scenario = scenario
        self.seq = 0
        self.expires_at = int(time.time()) + TTL_DAYS * 86400

    def put(self, kind, step_type="", payload=None, **extra):
        self.seq += 1
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        body = json.dumps(payload, default=str) if payload is not None else ""
        if len(body) > MAX_PAYLOAD:
            body = body[:MAX_PAYLOAD] + '..."<truncated>"'
        item = {
            "session_id": self.session_id,
            "event_ts": f"{now}#{self.invocation_id[:8]}#{self.seq:04d}",
            "agent_id": self.agent["agent_id"],
            "agent_type": self.agent["type"],
            "agent_name": self.agent["name"],
            "invocation_id": self.invocation_id,
            "kind": kind,
            "step_type": step_type,
            "user": self.user,
            "scenario": self.scenario,
            "payload": body,
            "expires_at": self.expires_at,
            **{k: v for k, v in extra.items() if v is not None},
        }
        _table.put_item(Item=item)


def _classic_step(trace):
    """('orchestrationTrace', 'invocationInput') style names for a classic trace event."""
    outer = next((k for k in trace if k.endswith("Trace")), next(iter(trace), ""))
    inner = trace.get(outer) or {}
    sub = next(iter(inner), "") if isinstance(inner, dict) else ""
    return outer, sub


def _invoke_classic(rec, prompt):
    a = OUTPUTS["hr_agent"]
    resp = _agent_rt.invoke_agent(
        agentId=a["agent_id"], agentAliasId=a["alias_id"], sessionId=rec.session_id,
        inputText=prompt, enableTrace=True,
        sessionState={"sessionAttributes": {"user": rec.user}},
    )
    text, steps, tokens_in, tokens_out, guardrail_action = "", [], 0, 0, None
    for event in resp["completion"]:
        if "chunk" in event:
            text += event["chunk"]["bytes"].decode()
        elif "trace" in event:
            trace = event["trace"]["trace"]
            outer, sub = _classic_step(trace)
            steps.append(f"{outer}.{sub}")
            inner = trace.get(outer, {}).get(sub, {}) if isinstance(trace.get(outer), dict) else {}
            usage = (inner.get("metadata") or {}).get("usage") or {} if isinstance(inner, dict) else {}
            tokens_in += usage.get("inputTokens", 0)
            tokens_out += usage.get("outputTokens", 0)
            if outer == "guardrailTrace":
                guardrail_action = trace[outer].get("action", guardrail_action)
            rec.put("trace", f"{outer}.{sub}", trace)
    return text.strip(), steps, tokens_in, tokens_out, guardrail_action


def _invoke_agentcore(rec, prompt):
    h = OUTPUTS["it_harness"]
    resp = _ac_rt.invoke_harness(
        harnessArn=h["harness_arn"], runtimeSessionId=rec.session_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    text, steps, tokens_in, tokens_out = "", [], 0, 0
    tool, tool_input = None, ""
    for event in resp["stream"]:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tool, tool_input = start["toolUse"].get("name"), ""
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            text += delta.get("text", "")
            tool_input += (delta.get("toolUse") or {}).get("input", "")
        elif "contentBlockStop" in event and tool:
            steps.append(f"toolUse.{tool}")
            rec.put("trace", "toolUse", {"tool": tool, "input": tool_input})
            tool = None
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
            tokens_in += usage.get("inputTokens", 0)
            tokens_out += usage.get("outputTokens", 0)
            rec.put("trace", "metadata", event["metadata"])
        elif "messageStop" in event:
            steps.append(f"messageStop.{event['messageStop'].get('stopReason')}")
        else:
            key = next(iter(event), "")
            if key.endswith("Exception") or key.endswith("Error"):
                raise RuntimeError(f"{key}: {event[key].get('message', event[key])}")
    return text.strip(), steps, tokens_in, tokens_out, None


def invoke(agent_key, prompt, session_id=None, user="demo-user", scenario="adhoc"):
    """Call one agent and record the whole exchange. Returns a summary dict (never raises on agent errors)."""
    agent = AGENTS[agent_key]
    if session_id is None:
        session_id = f"{agent_key}-{uuid.uuid4()}"
    rec = _Recorder(agent_key, session_id, user, scenario)
    rec.put("request", "user_input", {"prompt": prompt})
    started = time.perf_counter()
    try:
        fn = _invoke_classic if agent["type"] == "classic" else _invoke_agentcore
        text, steps, tin, tout, guardrail = fn(rec, prompt)
        latency_ms = int((time.perf_counter() - started) * 1000)
        rec.put("response", "final_answer", {"answer": text, "steps": steps}, latency_ms=latency_ms,
                input_tokens=tin, output_tokens=tout, guardrail_action=guardrail, status="ok")
        return {"ok": True, "session_id": session_id, "answer": text, "steps": steps, "latency_ms": latency_ms,
                "input_tokens": tin, "output_tokens": tout, "guardrail_action": guardrail}
    except Exception as e:  # recorded for the Errors tab, then reported to the caller
        latency_ms = int((time.perf_counter() - started) * 1000)
        code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
        rec.put("error", code, {"error": str(e)}, latency_ms=latency_ms, status="error")
        return {"ok": False, "session_id": session_id, "error": f"{code}: {e}", "latency_ms": latency_ms}
