# Bedrock Agent Observability

[![ci](https://github.com/sudhanshu5439/bedrock-agent-observability/actions/workflows/ci.yml/badge.svg)](https://github.com/sudhanshu5439/bedrock-agent-observability/actions/workflows/ci.yml)

**One dashboard for both kinds of Amazon Bedrock agent, classic Bedrock Agents and AgentCore agents,
built only on AWS-native recording.** For any agent it answers five questions: *what is it configured to
do, what did it actually do, who used or changed it, what did it cost, and what went wrong.*

- **Every step of every session.** A classic agent's trace exists only inside the `InvokeAgent` response,
  so a thin wrapper saves it to DynamoDB. AgentCore spans come from OpenTelemetry / Transaction Search.
  Both are replayed as the same step-by-step timeline: reasoning, tool calls with inputs and outputs,
  guardrail checks, model calls with tokens, answer.
- **Audit trail.** CloudTrail data events show who invoked which agent, when and from where. CloudTrail
  event history shows who created or changed it.
- **Guardrails you can review.** Every intervention, with the policy that fired, the stage and the
  prompt, so false positives are visible.
- **One failure, every layer.** A tool error is followed from the Lambda log to the agent trace or span
  to the caller.
- **Nothing leaves the account.** No vendor or licence; the dashboard only reads, and every query is
  filtered to the demo's own resources because the account is shared.

> Built as a demo for my team in a shared company AWS account. Account ids, names and addresses are
> removed from this repository and masked in the screenshots (`DASHBOARD_REDACT=1`). Every number below
> comes from measured runs.

## Architecture

![Architecture](docs/screenshots/architecture.png)

<sub>Black: the request path (1–9). Green dotted: the records AWS writes as a side effect. Red dashed: what
the dashboard reads (A–F). Long-form design notes, alternatives and cost: [docs/architecture.html](docs/architecture.html).</sub>

| | Classic agent: **HR Assistant** | AgentCore agent: **IT Support** |
|---|---|---|
| Built with | Bedrock Agents, action group, alias `live` | AgentCore Harness (managed Strands loop) |
| Tools | Lambda: leave balance, HR policy | AgentCore Gateway (MCP) → Lambda: ticket lookup, password reset |
| Safety | Guardrail: denied topic, content and prompt-attack filters, PII masking | System prompt only (a finding, below) |
| Step trace | `enableTrace=true`, saved to DynamoDB by `app/invoke.py` | OpenTelemetry spans in `aws/spans` |
| Model | Claude Haiku 4.5 (cross-region inference profile) | same |

Both kinds matter because AWS has put classic Bedrock Agents into maintenance mode and recommends
AgentCore: during a migration a team runs both, and this view does not change when an agent moves.

## Status

| # | Phase | State |
|---|---|---|
| 1 | Recording foundation: model invocation logging, CloudTrail data events, DynamoDB trace store, S3 archive, budget alarm | ✅ |
| 2 | Agents: classic agent + guardrail + Lambda tools; AgentCore Harness + Gateway + Lambda tools | ✅ |
| 3 | Invoke wrapper and scripted traffic: 45 conversations across six scenarios | ✅ |
| 4 | Dashboard: 7 tabs, tested against live data in every tab and time range | ✅ |
| 5 | Handover: runbook, demo script, teardown (dry run by default) | ✅ |

## The dashboard

| Overview | Session trace (classic agent) |
|---|---|
| ![Overview](docs/screenshots/tab0_overview.png) | ![HR session](docs/screenshots/session_hr.png) |
| **Guardrails & errors** | **Session trace (AgentCore, failed tool call)** |
| ![Guardrails and errors](docs/screenshots/tab5_guardrails.png) | ![IT session](docs/screenshots/session_it.png) |

| Tab | Shows | Reads from |
|---|---|---|
| Overview | Calls, error rate, p50/p95 latency, tokens, estimated cost, guardrail blocks per agent | DynamoDB trace store |
| Configuration | Model, prompt, tools, alias, guardrail policies, harness limits; who changed what | Bedrock / AgentCore control APIs, CloudTrail event history |
| Metrics | Latency per call, model tokens per period, tool usage, Lambda health | Invocation log (Logs Insights), spans, CloudWatch |
| Sessions & traces | Session list → step timeline with timing chart; `?session=<id>` deep links | DynamoDB (classic), `aws/spans` (AgentCore) |
| Audit trail | Every invocation: caller, source, time, error | CloudTrail data events |
| Guardrails & errors | Interventions by policy and stage; errors from every layer | Trace store, spans, Lambda logs |
| Live test | Ask either agent; the call is recorded like any other | `InvokeAgent` / `InvokeHarness` |

More: [configuration](docs/screenshots/tab1_configuration.png) · [metrics](docs/screenshots/tab2_metrics.png) ·
[sessions](docs/screenshots/tab3_sessions.png) · [audit trail](docs/screenshots/tab4_audit.png) ·
[live test](docs/screenshots/tab6_live.png)

## Measured results

From the scripted run (`traffic/run_traffic.py`):

| | |
|---|---|
| Agent calls | 53 (51 ok, 2 failed by design), ≈97k tokens, ≈$0.15 |
| Recorded | 379 trace-store events / 47 sessions · 85 model calls in the invocation log · 111 CloudTrail events · 241 spans / 26 traces |
| Latency | median 3.3 s both agents; p95 4.4 s (classic) / 5.0 s (AgentCore) |
| Model calls per agent call | 1.4 (classic) / 1.7 (AgentCore) |
| Guardrail | 10 interventions: colleague-pay topic, card number, prompt attack, PII masking |

## What it found

Four issues that would have gone unnoticed without this view (left in place on purpose for the demo):

1. **Guardrail false positive.** "What's the comp-off balance for E103?" is blocked as *colleague
   compensation*. Fix: reword the topic, add allowed examples.
2. **The guardrail masks the HR team's own address** (`hr@acme.example`) in answers. Fix: allow-list
   company addresses.
3. **The AgentCore harness exposes built-in `shell` and `file_operations` tools** that nobody configured
   (seen in the `gen_ai.agent.tools` span attribute); only the prompt keeps the agent from using them.
   Fix: disable built-in tools.
4. **IT Support has no guardrail**: it answers off-topic questions. Fix: attach one.

And one failure traced end to end: the Lambda raises → the classic agent records a `failureTrace` and the
caller gets `dependencyFailedException`, while the AgentCore agent records an `ERROR` span and recovers
with an apology.

## Quick start

Requirements: Python 3.12, AWS CLI v2, an AWS profile with Bedrock, AgentCore, Lambda, IAM, CloudTrail,
DynamoDB and CloudWatch access in `us-east-1`, and Transaction Search enabled for AgentCore spans.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt    # Linux/macOS: .venv/bin/pip
export BEDROCK_DEMO_PROFILE=my-profile BEDROCK_DEMO_BUDGET_EMAIL=me@example.com

cd infra
python phase1_foundation.py     # logging, trail, trace table, budget (refuses to overwrite others' logging config)
python phase2_agents.py         # Lambdas, guardrail, both agents, smoke tests; writes outputs.json
cd ..
python traffic/run_traffic.py   # ≈45 conversations, ≈$0.15
streamlit run app/dashboard.py  # http://localhost:8501
```

On Windows, `install_autostart.ps1` starts the dashboard at login. When the SSO session expires, the
dashboard offers a **Sign in to AWS** button.

Tear down everything (dry run first, then for real):

```bash
python infra/teardown.py
python infra/teardown.py --yes
```

**Cost:** ≈$6–8 for a demo week of ≈500 agent runs, ≈90% of it model tokens. At 10,000 runs a month the
recording layer is ≈1–2% of what the agents themselves cost.

## Limits, and the path to production

- The **recording layer is production-grade**; the **dashboard is demo-grade**: it runs locally on the
  presenter's SSO credentials, with no login of its own.
- Classic-agent traces are captured only for calls made through the wrapper.
- Costs are estimates from token counts, not billing data. No alerting or answer-quality evaluation yet.
- Not covered: Knowledge Bases / RAG retrieval views, Flows, batch inference, multiple accounts or regions.

Next steps: host behind sign-in (ECS Fargate + ALB + Cognito / IAM Identity Center, read-only task role),
alarms on error and guardrail rates, Bedrock / AgentCore evaluations, real cost from the CUR, and
re-evaluating a managed tool once most agents run on AgentCore.

## Layout

```
agents/   tool Lambdas (E999 and INC-500 fail on purpose)
infra/    config, phase1_foundation.py, phase2_agents.py, teardown.py
app/      invoke.py (wrapper + trace capture), traces.py (pure parsing, unit-tested), data.py (AWS reads), dashboard.py
traffic/  run_traffic.py (scripted conversations)
tests/    unit tests for trace parsing, guardrail matching, redaction and the Lambdas
docs/     architecture page and its generator, screenshots and their capture script
```

CI on every push: Gitleaks secrets scan (full history), Semgrep SAST, ruff, pytest. Dependabot keeps
Actions and Python packages current.
