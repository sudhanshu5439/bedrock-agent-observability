"""Generates docs/architecture.html: the technical write-up with the architecture diagram."""
import html
from pathlib import Path

OUT = Path(__file__).with_name("architecture.html")

INK = "#1b2230"
BLUE = "#2459d6"
GREEN = "#12825a"
RED = "#c8193c"
GREY = "#7b8494"
ORANGE = "#d4560f"

svg = []


def esc(s):
    return html.escape(s, quote=True)


def card(x, y, title, sub, icon, color, w=230, h=64):
    svg.append(
        f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="card"/>'
        f'<rect x="{x + 12}" y="{y + 14}" width="36" height="36" rx="8" fill="{color}"/>'
        f'<text x="{x + 30}" y="{y + 37}" text-anchor="middle" class="ic">{esc(icon)}</text>'
        f'<text x="{x + 58}" y="{y + 29}" class="ct">{esc(title)}</text>'
        f'<text x="{x + 58}" y="{y + 47}" class="cs">{esc(sub)}</text></g>'
    )


def box(x, y, w, h, color, icon, label):
    svg.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" class="box" stroke="{color}"/>'
        f'<rect x="{x}" y="{y}" width="30" height="30" fill="{color}"/>'
        f'<text x="{x + 15}" y="{y + 20}" text-anchor="middle" class="ic sm">{esc(icon)}</text>'
        f'<text x="{x + 42}" y="{y + 20}" class="bh">{esc(label)}</text>'
    )


def zone(x, y, w, h, fill, stroke, label, color):
    svg.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-dasharray="4 3"/>'
        f'<text x="{x + 17}" y="{y + 22}" class="zl" fill="{color}">{esc(label)}</text>'
    )


STYLES = {
    "req": f'stroke="{INK}" stroke-width="1.6" marker-end="url(#a-ink)"',
    "tel": f'stroke="{GREEN}" stroke-width="1.6" stroke-dasharray="2 4" marker-end="url(#a-green)"',
    "read": f'stroke="{RED}" stroke-width="1.6" stroke-dasharray="6 4" marker-end="url(#a-red)"',
}


def path(d, kind):
    svg.append(f'<path d="{d}" fill="none" {STYLES[kind]}/>')


def num(x, y, n):
    svg.append(f'<circle cx="{x}" cy="{y}" r="10" fill="{INK}"/>'
               f'<text x="{x}" y="{y + 4}" text-anchor="middle" class="nm">{n}</text>')


def letter(x, y, s):
    w = 20 if len(s) == 1 else 12 + 8 * len(s)
    svg.append(f'<rect x="{x - w / 2}" y="{y - 10}" width="{w}" height="20" rx="10" fill="{RED}"/>'
               f'<text x="{x}" y="{y + 4}" text-anchor="middle" class="nm">{esc(s)}</text>')


def label(x, y, t, anchor="start", cls="al"):
    svg.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" class="{cls}">{esc(t)}</text>')


def lines(x, y, rows, step=16, right=None):
    """rows: (text, css class) or (text, css class, right-aligned value)."""
    for i, row in enumerate(rows):
        label(x, y + i * step, row[0], cls=row[1])
        if len(row) == 3:
            label(right, y + i * step, row[2], "end", cls=row[1])


C1, C2, C3, C4 = 70, 345, 620, 895  # column x positions, cards are 230 wide

# ---- legend
lx = 960
for i, (txt, kind) in enumerate([("Request · 1–9", "req"), ("Telemetry", "tel"), ("Dashboard reads · A–F", "read")]):
    x = lx + i * 205
    path(f"M{x},22 H{x + 40}", kind)
    label(x + 48, 26, txt, cls="lg")

# ---- laptop row
box(20, 48, 1560, 136, INK, ">_", "Your laptop · Python 3.12 + boto3 · AWS SSO profile genai-demo")
card(50, 96, "Presenter", "asks the agent a question", "P", "#3b4456")
card(C2, 96, "Live test tab", "inside the Streamlit dashboard", "LT", RED)
card(640, 96, "Invoke wrapper", "boto3 · enableTrace=true", "W", "#3b4456")
card(935, 96, "Traffic generator", "50 scripted demo prompts", "TG", "#3b4456")
path("M280,128 H343", "req"); num(312, 128, 1)
path("M575,128 H638", "req"); label(607, 150, "calls", "middle")
path("M935,128 H872", "req"); label(906, 150, "batch", "middle")
lines(1230, 104, [("Credentials come from IAM Identity", "nt"), ("Center (SSO). No long-lived keys", "nt"),
                  ("are stored on the laptop.", "nt")])

# ---- AWS account
box(20, 220, 1180, 640, BLUE, "aws", "AWS account · shared company account · us-east-1")

zone(45, 262, 1130, 290, "#f1f5fe", "#c3d2f5", "AGENTS · two hosting styles, one model", BLUE)
card(C1, 322, "HR Assistant", "classic Bedrock Agent", "HR", BLUE)
card(C2, 322, "Guardrail", "PII mask · denied topic", "G", BLUE)
card(C3, 322, "Claude Haiku 4.5", "us. inference profile", "AI", ORANGE)
card(C4, 322, "IT Support", "AgentCore Harness", "IT", BLUE)
card(C1, 450, "hr-tools Lambda", "leave balance · HR policy", "λ", BLUE)
card(C4, 450, "Gateway → it-tools λ", "MCP: ticket · reset steps", "GW", BLUE)
lines(C2, 474, [("Same model, two loops: Bedrock", "nt"), ("Agents (classic) and a managed", "nt"),
                ("Strands loop in an AgentCore microVM.", "nt")])

path("M700,160 V302 H185 V320", "req"); num(430, 302, 2); label(446, 296, "InvokeAgent · trace on")
path("M820,160 V302 H1010 V320", "req"); num(880, 302, 2); label(896, 296, "InvokeHarness")
path("M300,354 H343", "req"); num(322, 354, 3)
path("M575,354 H618", "req"); num(597, 354, 4)
path("M895,354 H852", "req"); num(872, 354, 4)
path("M185,386 V448", "req"); num(185, 418, 5)
path("M1010,386 V448", "req"); num(1010, 418, 5)

# ---- telemetry zone
zone(45, 585, 1130, 255, "#eefaf3", "#b9e3cc", "TELEMETRY AND AUDIT · everything stays in the account", GREEN)
card(C1, 630, "CloudWatch metrics", "invocations · latency · tokens", "M", GREEN)
card(C2, 630, "CloudTrail · demo trail", "data events: who called", "CT", GREEN)
card(C3, 630, "Model invocation logs", "prompt · response · tokens", "IL", GREEN)
card(C4, 630, "Transaction Search", "OTel spans in aws/spans", "TS", GREEN)
card(C1, 750, "DynamoDB traces", "bedrock-demo-agent-traces", "DB", GREEN)
card(C2, 750, "Company trail", "config changes · read-only", "CT", GREY)
card(C3, 750, "S3 log archive", "30-day expiry", "S3", GREEN)
card(C4, 750, "GenAI Observability", "CloudWatch console view", "GO", GREEN)

path("M460,552 V628", "tel"); num(460, 570, 6); label(476, 574, "who called")
path("M735,386 V628", "tel"); num(735, 570, 7); label(751, 574, "every model call")
path("M70,354 H56 V782 H68", "tel"); num(56, 570, 8)
path("M1010,514 V628", "tel"); num(1010, 570, 8); label(1026, 574, "spans")
path("M185,514 V628", "tel"); num(185, 570, 9); label(201, 574, "metrics")
path("M735,694 V748", "tel"); label(745, 726, "copy")
path("M1010,694 V748", "tel"); label(1020, 726, "rendered in")

# ---- dashboard
box(20, 890, 1180, 180, RED, "D", "Dashboard · Streamlit on your laptop · reads through boto3")
card(C1, 980, "Sessions · classic", "metrics + step timeline", "S", RED)
card(C2, 980, "Audit trail", "who did what, when", "A", RED)
card(C3, 980, "Prompts and guardrails", "model I/O · blocks · errors", "P", RED)
card(C4, 980, "Sessions · AgentCore", "span timeline", "S", RED)

path("M185,978 V816", "read"); letter(185, 950, "C")
path("M280,978 V870 H322 V662 H302", "read"); letter(322, 720, "B")
path("M460,978 V816", "read"); letter(460, 950, "E")
path("M560,978 V870 H597 V662 H577", "read"); letter(597, 720, "E")
path("M830,978 V870 H872 V662 H852", "read"); letter(872, 720, "D F")
path("M1100,978 V870 H1150 V662 H1127", "read"); letter(1150, 720, "C")
path("M1185,890 V354 H1127", "read"); letter(1185, 720, "A"); label(1177, 578, "agent config", "end")

# ---- right panel
def panel(y, h, title, rows):
    svg.append(f'<rect x="1230" y="{y}" width="350" height="{h}" rx="10" class="panel"/>')
    label(1250, y + 30, title, cls="ph")
    lines(1250, y + 56, rows, step=17, right=1560)


panel(220, 330, "Design principles", [
    ("AWS-native.", "pb"), ("Data never leaves the account; no new vendor.", "pt"),
    ("Read-only by default.", "pb"), ("Only the Live test tab invokes an agent.", "pt"),
    ("Every number names its source.", "pb"), ("Each tile maps to one AWS API or log.", "pt"),
    ("Leave shared things alone.", "pb"), ("Region-wide settings are only set where unused.", "pt"),
    ("Easy to remove.", "pb"), ("bedrock-demo-* names, one teardown script.", "pt"),
    ("Cheap on purpose.", "pb"), ("Haiku 4.5, on-demand billing, 30-day expiry.", "pt"),
])
panel(575, 245, "Cost · demo week (≈500 agent runs)", [
    ("Claude Haiku 4.5 model calls", "pt", "≈ $5.50"),
    ("Guardrail checks", "pt", "≈ $0.25"),
    ("AgentCore Harness + Gateway", "pt", "< $0.50"),
    ("Logs, trail, DynamoDB, S3", "pt", "< $0.20"),
    ("Lambda, budget, Streamlit (local)", "pt", "$0"),
    ("Total", "pb", "≈ $6–8"),
    ("Model tokens are ~90% of the bill.", "pt"),
    ("Budget alarm: $25/month, email alerts.", "pt"),
])
panel(845, 225, "Shared account · left untouched", [
    ("Another project's invocation logging (other region)", "pt"),
    ("Company organisation trail (read-only use)", "pt"),
    ("Other teams' Bedrock agents", "pt"),
    ("Other teams' AgentCore runtimes", "pt"),
    ("Our resources: bedrock-demo-* names,", "pt"),
    ("tag Project=bedrock-observability-demo", "pt"),
    ("(IAM roles untagged: no iam:TagRole).", "pt"),
])

DEFS = "".join(
    f'<marker id="a-{n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
    for n, c in (("ink", INK), ("green", GREEN), ("red", RED))
)

SVG = (
    '<svg viewBox="0 0 1600 1090" role="img" aria-label="Requests flow from the laptop to a classic Bedrock Agent and an '
    'AgentCore agent that share Claude Haiku 4.5; every call is recorded by CloudTrail, model invocation logging, '
    'CloudWatch metrics, DynamoDB traces and AgentCore spans, and the Streamlit dashboard reads all of them back.">'
    f'<defs>{DEFS}</defs>' + "".join(svg) + "</svg>"
)

PAGE = (Path(__file__).with_name("architecture_template.html")).read_text(encoding="utf-8").replace("{{SVG}}", SVG)
OUT.write_text(PAGE, encoding="utf-8")
print("wrote", OUT)
