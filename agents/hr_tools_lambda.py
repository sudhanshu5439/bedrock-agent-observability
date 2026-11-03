"""Action group Lambda for the HR Assistant (classic Bedrock Agent, function-details schema).

Demo data only. Employee E999 deliberately raises an error so failed tool calls
show up in the dashboard's errors tab.
"""
import json
import re

EMPLOYEES = {
    "E101": {"name": "Asha Rao", "annual_leave": 12, "sick_leave": 5, "comp_off": 1},
    "E102": {"name": "Ravi Kumar", "annual_leave": 4, "sick_leave": 7, "comp_off": 0},
    "E103": {"name": "Meera Iyer", "annual_leave": 18, "sick_leave": 2, "comp_off": 3},
}

POLICIES = {
    "leave": "Employees get 24 days of annual leave per year. Up to 5 unused days carry over to the next year. "
             "Leave of more than 5 consecutive days needs manager approval 2 weeks in advance.",
    "sick": "Employees get 10 days of paid sick leave per year. More than 2 consecutive days needs a medical certificate.",
    "wfh": "Hybrid policy: up to 3 work-from-home days per week, agreed with the manager. Core hours are 11:00-16:00 IST.",
    "travel": "Business travel must be booked through the travel desk. Economy class for flights under 6 hours. "
              "Daily allowance is INR 3,000 for domestic travel.",
}


def _param(event, name):
    for p in event.get("parameters") or []:
        if p.get("name") == name:
            return str(p.get("value", "")).strip()
    return ""


def get_leave_balance(event):
    employee_id = _param(event, "employee_id").upper()
    if not re.fullmatch(r"E\d{3}", employee_id):
        return f"'{employee_id}' is not a valid employee ID. IDs look like E101."
    if employee_id == "E999":
        raise RuntimeError("HRMS upstream timeout while fetching leave balance for E999")
    emp = EMPLOYEES.get(employee_id)
    if not emp:
        return f"No employee found with ID {employee_id}."
    return json.dumps({"employee_id": employee_id, **emp})


def get_hr_policy(event):
    topic = _param(event, "topic").lower()
    for key, text in POLICIES.items():
        if key in topic:
            return text
    return f"No policy found for '{topic}'. Available topics: {', '.join(POLICIES)}."


HANDLERS = {"get_leave_balance": get_leave_balance, "get_hr_policy": get_hr_policy}


def lambda_handler(event, context):
    function = event.get("function", "")
    print(json.dumps({"function": function, "sessionId": event.get("sessionId")}))
    handler = HANDLERS.get(function)
    body = handler(event) if handler else f"Unknown function {function}."
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "function": function,
            "functionResponse": {"responseBody": {"TEXT": {"body": body}}},
        },
    }
