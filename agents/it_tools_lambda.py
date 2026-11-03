"""AgentCore Gateway Lambda target for the IT Support agent.

The gateway passes tool arguments as the event and the tool name in
context.client_context.custom["bedrockAgentCoreToolName"] as "<target>___<tool>".
Demo data only. Ticket INC-500 deliberately raises an error for the errors tab.
"""
import json

TICKETS = {
    "INC-101": {"title": "VPN disconnects every 10 minutes", "status": "In progress", "assignee": "Network team",
                "priority": "P2", "updated": "2026-11-02"},
    "INC-102": {"title": "Laptop battery swelling", "status": "Awaiting hardware", "assignee": "Desktop support",
                "priority": "P1", "updated": "2026-11-03"},
    "INC-103": {"title": "Outlook not syncing calendar", "status": "Resolved", "assignee": "M365 team",
                "priority": "P3", "updated": "2026-10-30"},
}

RESET_STEPS = {
    "sso": ["Open https://id.example.internal/reset", "Enter your work email", "Approve the push on your MFA app",
            "Choose a new password of at least 14 characters"],
    "vpn": ["VPN uses your SSO password; reset SSO first", "Reconnect the VPN client after 5 minutes"],
    "laptop": ["At the login screen choose 'Forgot password'", "Answer the recovery prompt with your SSO account",
               "If BitLocker asks for a recovery key, raise a P2 ticket with Desktop support"],
}


def lookup_ticket(args):
    ticket_id = str(args.get("ticket_id", "")).strip().upper()
    if ticket_id == "INC-500":
        raise RuntimeError("ServiceNow API returned 503 for INC-500")
    ticket = TICKETS.get(ticket_id)
    if not ticket:
        return {"found": False, "message": f"No ticket {ticket_id}. Known tickets: {', '.join(TICKETS)}"}
    return {"found": True, "ticket_id": ticket_id, **ticket}


def password_reset_steps(args):
    system = str(args.get("system", "")).strip().lower()
    steps = RESET_STEPS.get(system)
    if not steps:
        return {"found": False, "message": f"No reset guide for '{system}'. Supported: {', '.join(RESET_STEPS)}"}
    return {"found": True, "system": system, "steps": steps}


HANDLERS = {"lookup_ticket": lookup_ticket, "password_reset_steps": password_reset_steps}


def lambda_handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    tool = custom.get("bedrockAgentCoreToolName", "").split("___")[-1]
    print(json.dumps({"tool": tool, "args": event}))
    handler = HANDLERS.get(tool)
    if not handler:
        return {"error": f"Unknown tool {tool}"}
    return handler(event)
