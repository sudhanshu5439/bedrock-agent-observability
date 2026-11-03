"""Unit tests for the two tool Lambdas, including their deliberate failures (E999, INC-500)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENTS = Path(__file__).resolve().parent.parent / "agents"


def load(name):
    spec = importlib.util.spec_from_file_location(name, AGENTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = load("hr_tools_lambda")
it = load("it_tools_lambda")


def hr_event(function, **params):
    return {"function": function, "actionGroup": "hr_tools", "sessionId": "s1",
            "parameters": [{"name": k, "value": v} for k, v in params.items()]}


def hr_body(resp):
    return resp["response"]["functionResponse"]["responseBody"]["TEXT"]["body"]


def it_context(tool):
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"it-tools___{tool}"}))


def test_hr_leave_balance_returns_the_employee():
    body = json.loads(hr_body(hr.lambda_handler(hr_event("get_leave_balance", employee_id="e101"), None)))
    assert body["employee_id"] == "E101" and body["annual_leave"] == 12


def test_hr_rejects_bad_ids_and_unknown_employees():
    assert "not a valid employee ID" in hr_body(hr.lambda_handler(hr_event("get_leave_balance", employee_id="X1"), None))
    assert "No employee found" in hr_body(hr.lambda_handler(hr_event("get_leave_balance", employee_id="E555"), None))


def test_hr_e999_fails_on_purpose():
    with pytest.raises(RuntimeError, match="E999"):
        hr.lambda_handler(hr_event("get_leave_balance", employee_id="E999"), None)


def test_hr_policy_lookup_matches_topic_words():
    assert "work-from-home" in hr_body(hr.lambda_handler(hr_event("get_hr_policy", topic="WFH rules"), None))
    assert "No policy found" in hr_body(hr.lambda_handler(hr_event("get_hr_policy", topic="parental"), None))


def test_it_ticket_lookup_and_reset_steps():
    assert it.lambda_handler({"ticket_id": "inc-101"}, it_context("lookup_ticket"))["status"] == "In progress"
    assert it.lambda_handler({"ticket_id": "INC-999"}, it_context("lookup_ticket"))["found"] is False
    assert it.lambda_handler({"system": "VPN"}, it_context("password_reset_steps"))["found"] is True


def test_it_inc500_fails_on_purpose():
    with pytest.raises(RuntimeError, match="INC-500"):
        it.lambda_handler({"ticket_id": "INC-500"}, it_context("lookup_ticket"))


def test_it_unknown_tool():
    assert "Unknown tool" in it.lambda_handler({}, it_context("format_disk"))["error"]
