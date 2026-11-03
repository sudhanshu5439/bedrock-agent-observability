"""Unit tests for app/traces.py: trace parsing, guardrail matching, caller names and redaction."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import traces  # noqa: E402

ACCOUNT = "111122223333"


def item(kind, step_type, payload, inv="inv-1", **extra):
    return {"session_id": "s1", "invocation_id": inv, "kind": kind, "step_type": step_type,
            "payload": json.dumps(payload), "ts": pd.Timestamp("2026-11-03T08:00:00Z"), "agent_name": "HR Assistant",
            "user": "asha.rao", "scenario": "tool", **extra}


GUARDRAIL_BLOCK = {"guardrailTrace": {
    "action": "INTERVENED", "traceId": "abc-guardrail-pre-0",
    "inputAssessments": [{"topicPolicy": {"topics": [{"name": "Colleague compensation", "action": "BLOCKED"}]}}],
}}
GUARDRAIL_MASK = {"guardrailTrace": {
    "action": "INTERVENED", "traceId": "abc-guardrail-post-0",
    "outputAssessments": [{"sensitiveInformationPolicy": {"piiEntities": [
        {"type": "EMAIL", "match": "hr@acme.example", "action": "ANONYMIZED"}]}}],
}}


def session_items():
    return pd.DataFrame([
        item("request", "user_input", {"prompt": "Leave balance for E102?"}),
        item("trace", "orchestrationTrace.modelInvocationOutput", {"orchestrationTrace": {"modelInvocationOutput": {
            "metadata": {"totalTimeMs": 900, "startTime": "2026-11-03 08:00:01+00:00",
                         "endTime": "2026-11-03 08:00:02+00:00", "usage": {"inputTokens": 1000, "outputTokens": 100}}}}}),
        item("trace", "orchestrationTrace.invocationInput", {"orchestrationTrace": {"invocationInput": {
            "actionGroupInvocationInput": {"function": "get_leave_balance",
                                           "parameters": [{"name": "employee_id", "value": "E102"}]}}}}),
        item("trace", "orchestrationTrace.observation", {"orchestrationTrace": {"observation": {
            "actionGroupInvocationOutput": {"text": '{"annual_leave": 4}', "metadata": {"totalTimeMs": 38}}}}}),
        item("trace", "guardrailTrace.action", GUARDRAIL_MASK),
        item("response", "final_answer", {"answer": "You have 4 days."}, latency_ms=3500, input_tokens=1000,
             output_tokens=100, guardrail_action="INTERVENED", status="ok"),
    ])


def test_classic_steps_builds_the_timeline_in_order():
    steps = traces.classic_steps(session_items())
    assert list(steps["category"]) == ["User", "Model", "Tool", "Tool", "Guardrail", "Answer"]
    assert steps.iloc[1]["detail"] == "1000 in / 100 out tokens"
    assert steps.iloc[2]["label"] == "Tool call: get_leave_balance"
    assert json.loads(steps.iloc[2]["detail"]) == {"employee_id": "E102"}
    assert steps.iloc[3]["ms"] == 38
    assert steps.iloc[4]["label"] == "Guardrail output check: INTERVENED"


def test_classic_steps_reports_failures():
    df = pd.DataFrame([item("trace", "failureTrace.failureCode",
                            {"failureTrace": {"failureCode": 424, "failureReason": "Lambda failed"}})])
    row = traces.classic_steps(df).iloc[0]
    assert (row["category"], row["label"], row["detail"]) == ("Error", "Failure (424)", "Lambda failed")


def test_guardrail_matches_names_each_policy():
    assert traces.guardrail_matches(GUARDRAIL_BLOCK["guardrailTrace"]) == [
        "denied topic 'Colleague compensation' (BLOCKED)"]
    assert traces.guardrail_matches(GUARDRAIL_MASK["guardrailTrace"]) == [
        "PII EMAIL 'hr@acme.example' (ANONYMIZED)"]
    assert traces.guardrail_matches({"action": "NONE", "inputAssessments": [{}]}) == []


def test_guardrail_events_keeps_only_interventions_with_their_prompt():
    items = pd.DataFrame([
        item("request", "user_input", {"prompt": "What is Ravi's salary?"}),
        item("trace", "guardrailTrace.action", GUARDRAIL_BLOCK),
        item("trace", "guardrailTrace.action", {"guardrailTrace": {"action": "NONE", "traceId": "x-pre-0"}}),
    ])
    ev = traces.guardrail_events(items)
    assert len(ev) == 1
    assert ev.iloc[0][["stage", "policy", "prompt"]].tolist() == ["Input", "Denied topic", "What is Ravi's salary?"]


def test_calls_and_sessions_summarise_each_agent_call():
    c = traces.calls(session_items())
    assert len(c) == 1
    assert c.iloc[0]["prompt"] == "Leave balance for E102?"
    assert c.iloc[0]["cost_usd"] == pytest.approx(1000 / 1e6 * 1.0 + 100 / 1e6 * 5.0)
    s = traces.sessions(c)
    assert s.iloc[0][["turns", "errors", "guardrail", "tokens"]].tolist() == [1, 0, 1, 1100]


@pytest.mark.parametrize("arn, expected", [
    ("arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_PowerUserAccess_abc/jane@example.com", "jane@example.com"),
    ("arn:aws:sts::111122223333:assumed-role/bedrock-demo-hr-agent-role/a1b2c3", "role/bedrock-demo-hr-agent-role"),
    ("arn:aws:iam::111122223333:user/ci-bot", "ci-bot"),
])
def test_caller_name(arn, expected):
    assert traces.caller_name(arn) == expected


def test_scrub_masks_account_emails_ips_and_names_but_keeps_demo_addresses():
    text = (f"arn:aws:iam::{ACCOUNT}:role/x called by jane.doe@corp.com from 198.51.100.7 "
            "for Initech staff; contact hr@acme.example; local 127.0.0.1")
    out = traces.scrub(text, ACCOUNT, ("Initech",))
    assert ACCOUNT not in out and "123456789012" in out
    assert "jane.doe@corp.com" not in out and "user@example.com" in out
    assert "198.51.100.7" not in out and "203.0.113.10" in out
    assert "Initech" not in out and "Acme staff" in out
    assert "hr@acme.example" in out and "127.0.0.1" in out


def test_scrub_recurses_into_dicts_lists_and_dataframes():
    nested = {"a": [f"acct {ACCOUNT}", {"b": "x@corp.com"}], "n": 5}
    assert traces.scrub(nested, ACCOUNT) == {"a": ["acct 123456789012", {"b": "user@example.com"}], "n": 5}
    df = pd.DataFrame({"who": ["x@corp.com"], "n": [1]})
    assert traces.scrub(df, ACCOUNT)["who"].tolist() == ["user@example.com"]
