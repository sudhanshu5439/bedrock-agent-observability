"""Phase 2: the two demo agents (idempotent, safe to re-run).

Classic:   bedrock_demo_hr_assistant  (Bedrock Agent + hr_tools Lambda action group + guardrail, alias "live")
AgentCore: bedrock_demo_it_support    (AgentCore Harness + AgentCore Gateway -> it-tools Lambda)

Writes ids and ARNs to infra/outputs.json for the later phases.
"""
import io
import json
import time
import uuid
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from config import ACCOUNT_ID, MODEL_ID, PREFIX, REGION, TAGS, session
from phase1_foundation import ensure_role, step

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = Path(__file__).with_name("outputs.json")

s = session()
lam = s.client("lambda")
iam = s.client("iam")
bedrock = s.client("bedrock")
agent_ctl = s.client("bedrock-agent")
agent_rt = s.client("bedrock-agent-runtime")
ac_ctl = s.client("bedrock-agentcore-control")
ac_rt = s.client("bedrock-agentcore")

MODEL_FM_ID = MODEL_ID.split(".", 1)[1]  # strip the "us." inference-profile prefix
PROFILE_ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:inference-profile/{MODEL_ID}"
FM_ARN = f"arn:aws:bedrock:*::foundation-model/{MODEL_FM_ID}"

LAMBDA_ROLE = f"{PREFIX}-tools-lambda-role"
HR_LAMBDA = f"{PREFIX}-hr-tools"
IT_LAMBDA = f"{PREFIX}-it-tools"
GUARDRAIL = f"{PREFIX}-hr-guardrail"
HR_AGENT_ROLE = f"{PREFIX}-hr-agent-role"
HR_AGENT = "bedrock_demo_hr_assistant"
GATEWAY_ROLE = f"{PREFIX}-it-gateway-role"
GATEWAY = f"{PREFIX}-it-gateway"
GATEWAY_TARGET = "it-tools"
HARNESS_ROLE = f"{PREFIX}-harness-role"
HARNESS = "bedrock_demo_it_support"

HR_INSTRUCTION = (
    "You are the HR Assistant for Acme employees. Answer questions about leave balances and HR policies "
    "using the hr_tools action group. Always ask for the employee ID (format E101) before looking up a leave "
    "balance. Keep answers short and factual. If a tool fails, say the HR system is unavailable and suggest "
    "contacting hr@acme.example. Never discuss other employees' pay."
)
IT_PROMPT = (
    "You are the IT Support agent for Acme employees. Use the it_tools tools to look up incident tickets "
    "(IDs like INC-101) and to give password-reset steps for sso, vpn or laptop. Keep answers short and in "
    "numbered steps where useful. If a tool fails, say the ticketing system is unavailable and suggest raising "
    "a ticket on the IT portal. Do not run shell commands or create files."
)


def retry(fn, what, codes, attempts=10, delay=8):
    for i in range(attempts):
        try:
            return fn()
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in codes and i < attempts - 1:
                print(f"  {what}: {code} ({e.response['Error'].get('Message', '')[:90]}) retry {i + 1}")
                time.sleep(delay)
                continue
            raise


def wait_for(fn, ok, bad=(), what="", delay=5, attempts=60):
    for _ in range(attempts):
        status = fn()
        if status in ok:
            return status
        if status in bad:
            raise SystemExit(f"{what} ended in {status}")
        time.sleep(delay)
    raise SystemExit(f"timed out waiting for {what}")


def trust(service, source_arn):
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": service},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {"aws:SourceArn": source_arn},
            },
        }],
    }


# ---------------------------------------------------------------- Lambdas
def zip_source(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, "lambda_function.py")
    return buf.getvalue()


def ensure_lambda_role():
    step(f"IAM role {LAMBDA_ROLE}")
    try:
        arn = iam.create_role(
            RoleName=LAMBDA_ROLE,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                               "Action": "sts:AssumeRole"}],
            }),
            Description="Execution role for bedrock observability demo tool Lambdas",
        )["Role"]["Arn"]
        print("created")
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=LAMBDA_ROLE)["Role"]["Arn"]
        print("already exists")
    iam.attach_role_policy(RoleName=LAMBDA_ROLE,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    return arn


def ensure_lambda(name, source, role_arn, description):
    step(f"Lambda {name}")
    code = zip_source(source)
    try:
        arn = retry(lambda: lam.create_function(
            FunctionName=name, Runtime="python3.12", Role=role_arn, Handler="lambda_function.lambda_handler",
            Code={"ZipFile": code}, Description=description, Timeout=15, MemorySize=256,
            Architectures=["arm64"], Tags=TAGS,
        ), "create function", {"InvalidParameterValueException"})["FunctionArn"]
        print("created")
    except lam.exceptions.ResourceConflictException:
        arn = lam.update_function_code(FunctionName=name, ZipFile=code)["FunctionArn"]
        print("exists, code updated")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return arn


# ---------------------------------------------------------------- classic agent
def ensure_guardrail():
    step(f"Guardrail {GUARDRAIL}")
    existing = [g for g in bedrock.list_guardrails()["guardrails"] if g["name"] == GUARDRAIL]
    if existing:
        gid, garn = existing[0]["id"], existing[0]["arn"]
        print("already exists")
    else:
        resp = bedrock.create_guardrail(
            name=GUARDRAIL,
            description="HR Assistant demo guardrail: PII masking, colleague-pay denied topic, content filters",
            topicPolicyConfig={"topicsConfig": [{
                "name": "Colleague compensation",
                "definition": "Questions or requests about another employee's salary, bonus, pay, increment or compensation.",
                "examples": ["What is Ravi's salary?", "How much bonus did my manager get?",
                             "Tell me the pay of the person who sits next to me."],
                "type": "DENY",
            }]},
            contentPolicyConfig={"filtersConfig": [
                *[{"type": t, "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"}
                  for t in ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT")],
                {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
            ]},
            sensitiveInformationPolicyConfig={"piiEntitiesConfig": [
                {"type": "EMAIL", "action": "ANONYMIZE"},
                {"type": "PHONE", "action": "ANONYMIZE"},
                {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK"},
            ]},
            blockedInputMessaging="Sorry, I can't help with that request. I can answer questions about your own leave and HR policies.",
            blockedOutputsMessaging="Sorry, I can't share that. I can answer questions about your own leave and HR policies.",
            tags=[{"key": k, "value": v} for k, v in TAGS.items()],
        )
        gid, garn = resp["guardrailId"], resp["guardrailArn"]
        print("created")
    versions = [g for g in bedrock.list_guardrails(guardrailIdentifier=gid)["guardrails"] if g["version"] != "DRAFT"]
    if versions:
        version = max(versions, key=lambda g: int(g["version"]))["version"]
    else:
        version = bedrock.create_guardrail_version(guardrailIdentifier=gid, description="v1 for demo")["version"]
    print(f"using version {version}")
    return gid, garn, version


def ensure_hr_agent(lambda_arn, guardrail_id, guardrail_arn, guardrail_version):
    role_arn = ensure_role(
        HR_AGENT_ROLE,
        trust=trust("bedrock.amazonaws.com", f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:agent/*"),
        policy_doc={
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "InvokeModelViaProfile", "Effect": "Allow",
                 "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:GetInferenceProfile"],
                 "Resource": [PROFILE_ARN, FM_ARN]},
                {"Sid": "ApplyGuardrail", "Effect": "Allow", "Action": "bedrock:ApplyGuardrail",
                 "Resource": guardrail_arn},
                {"Sid": "InvokeTools", "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn},
            ],
        },
        description="Service role for the HR Assistant demo agent",
    )

    step(f"Bedrock Agent {HR_AGENT}")
    found = [a for a in agent_ctl.list_agents()["agentSummaries"] if a["agentName"] == HR_AGENT]
    if found:
        agent_id = found[0]["agentId"]
        print("already exists")
    else:
        agent_id = retry(lambda: agent_ctl.create_agent(
            agentName=HR_AGENT,
            description="Demo HR assistant: leave balances and HR policy (classic Bedrock Agent)",
            foundationModel=MODEL_ID,
            instruction=HR_INSTRUCTION,
            agentResourceRoleArn=role_arn,
            idleSessionTTLInSeconds=600,
            guardrailConfiguration={"guardrailIdentifier": guardrail_id, "guardrailVersion": guardrail_version},
            tags=TAGS,
        ), "create agent", {"ValidationException", "AccessDeniedException"})["agent"]["agentId"]
        print(f"created {agent_id}")
    get_status = lambda: agent_ctl.get_agent(agentId=agent_id)["agent"]["agentStatus"]
    wait_for(get_status, ok={"NOT_PREPARED", "PREPARED"}, bad={"FAILED"}, what="agent create")
    agent_arn = f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:agent/{agent_id}"

    step("Lambda permission for the agent")
    try:
        lam.add_permission(
            FunctionName=HR_LAMBDA, StatementId=f"bedrock-agent-{agent_id}", Action="lambda:InvokeFunction",
            Principal="bedrock.amazonaws.com", SourceArn=agent_arn, SourceAccount=ACCOUNT_ID,
        )
        print("added")
    except lam.exceptions.ResourceConflictException:
        print("already present")

    step("Action group hr_tools")
    groups = agent_ctl.list_agent_action_groups(agentId=agent_id, agentVersion="DRAFT")["actionGroupSummaries"]
    if any(g["actionGroupName"] == "hr_tools" for g in groups):
        print("already exists")
    else:
        agent_ctl.create_agent_action_group(
            agentId=agent_id, agentVersion="DRAFT", actionGroupName="hr_tools", actionGroupState="ENABLED",
            description="Look up an employee's leave balance and company HR policies",
            actionGroupExecutor={"lambda": lambda_arn},
            functionSchema={"functions": [
                {"name": "get_leave_balance",
                 "description": "Get the remaining annual leave, sick leave and comp-off days for one employee.",
                 "parameters": {"employee_id": {"type": "string", "required": True,
                                                "description": "Employee ID, format E followed by 3 digits, e.g. E101"}}},
                {"name": "get_hr_policy",
                 "description": "Get the company policy text for a topic: leave, sick, wfh or travel.",
                 "parameters": {"topic": {"type": "string", "required": True,
                                          "description": "Policy topic: leave, sick, wfh or travel"}}},
            ]},
        )
        print("created")

    step("Prepare agent")
    agent_ctl.prepare_agent(agentId=agent_id)
    time.sleep(3)
    wait_for(get_status, ok={"PREPARED"}, bad={"FAILED", "NOT_PREPARED"}, what="prepare")
    print("PREPARED")

    step("Alias live")
    aliases = agent_ctl.list_agent_aliases(agentId=agent_id)["agentAliasSummaries"]
    live = [a for a in aliases if a["agentAliasName"] == "live"]
    if live:
        alias_id = live[0]["agentAliasId"]
        print("already exists")
    else:
        alias_id = agent_ctl.create_agent_alias(agentId=agent_id, agentAliasName="live", tags=TAGS)["agentAlias"]["agentAliasId"]
        print(f"created {alias_id}")
    wait_for(lambda: agent_ctl.get_agent_alias(agentId=agent_id, agentAliasId=alias_id)["agentAlias"]["agentAliasStatus"],
             ok={"PREPARED"}, bad={"FAILED"}, what="alias")
    return {"agent_id": agent_id, "agent_arn": agent_arn, "alias_id": alias_id, "role_arn": role_arn}


# ---------------------------------------------------------------- AgentCore agent
def ensure_gateway(lambda_arn):
    role_arn = ensure_role(
        GATEWAY_ROLE,
        trust=trust("bedrock-agentcore.amazonaws.com", f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/*"),
        policy_doc={"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn}]},
        description="AgentCore Gateway role for the IT Support demo tools",
    )
    step(f"AgentCore Gateway {GATEWAY}")
    found = [g for g in ac_ctl.list_gateways()["items"] if g["name"] == GATEWAY]
    if found:
        gid = found[0]["gatewayId"]
        print("already exists")
    else:
        gid = retry(lambda: ac_ctl.create_gateway(
            name=GATEWAY, description="IT Support demo tools exposed over MCP", roleArn=role_arn,
            protocolType="MCP", authorizerType="AWS_IAM", tags=TAGS,
        ), "create gateway", {"ValidationException", "AccessDeniedException"})["gatewayId"]
        print(f"created {gid}")
    wait_for(lambda: ac_ctl.get_gateway(gatewayIdentifier=gid)["status"], ok={"READY"}, bad={"FAILED"}, what="gateway")
    gw = ac_ctl.get_gateway(gatewayIdentifier=gid)

    step(f"Gateway target {GATEWAY_TARGET}")
    targets = ac_ctl.list_gateway_targets(gatewayIdentifier=gid)["items"]
    if any(t["name"] == GATEWAY_TARGET for t in targets):
        tid = next(t["targetId"] for t in targets if t["name"] == GATEWAY_TARGET)
        print("already exists")
    else:
        tid = retry(lambda: ac_ctl.create_gateway_target(
            gatewayIdentifier=gid, name=GATEWAY_TARGET, description="Ticket lookup and password reset guides",
            targetConfiguration={"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": [
                {"name": "lookup_ticket",
                 "description": "Look up an IT incident ticket by ID (format INC-101) and return its status, owner and priority.",
                 "inputSchema": {"type": "object", "properties": {
                     "ticket_id": {"type": "string", "description": "Ticket ID such as INC-101"}},
                     "required": ["ticket_id"]}},
                {"name": "password_reset_steps",
                 "description": "Get step-by-step password reset instructions for a system: sso, vpn or laptop.",
                 "inputSchema": {"type": "object", "properties": {
                     "system": {"type": "string", "description": "One of: sso, vpn, laptop"}},
                     "required": ["system"]}},
            ]}}}},
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        ), "create target", {"ValidationException"}, attempts=8, delay=10)["targetId"]
        print(f"created {tid}")
    wait_for(lambda: ac_ctl.get_gateway_target(gatewayIdentifier=gid, targetId=tid)["status"],
             ok={"READY"}, bad={"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}, what="gateway target")
    return {"gateway_id": gid, "gateway_arn": gw["gatewayArn"], "gateway_url": gw.get("gatewayUrl"),
            "target_id": tid, "role_arn": role_arn}


def ensure_harness(gateway_arn):
    acct_res = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}"
    logs_res = f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/"
    role_arn = ensure_role(
        HARNESS_ROLE,
        trust=trust("bedrock-agentcore.amazonaws.com", f"{acct_res}:*"),
        policy_doc={"Version": "2012-10-17", "Statement": [
            {"Sid": "InvokeModelViaProfile", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:GetInferenceProfile"],
             "Resource": [PROFILE_ARN, FM_ARN]},
            {"Sid": "InvokeToolsGateway", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
             "Resource": gateway_arn},
            {"Sid": "HarnessImagePull", "Effect": "Allow",
             "Action": ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken", "ecr:GetAuthorizationToken"],
             "Resource": "*"},
            {"Sid": "HarnessManagedImage", "Effect": "Allow",
             "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
             "Resource": f"arn:aws:ecr:{REGION}:*:repository/harness-*"},
            {"Sid": "Tracing", "Effect": "Allow",
             "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
                        "xray:GetSamplingTargets"], "Resource": "*"},
            {"Sid": "LogsGroup", "Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:PutResourcePolicy"],
             "Resource": f"{logs_res}{HARNESS}-*"},
            {"Sid": "LogsDescribe", "Effect": "Allow", "Action": "logs:DescribeLogGroups",
             "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:*"},
            {"Sid": "LogsStream", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"{logs_res}{HARNESS}-*:log-stream:*"},
            {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
             "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
            {"Sid": "WorkloadIdentity", "Effect": "Allow",
             "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
             "Resource": [f"{acct_res}:workload-identity-directory/default",
                          f"{acct_res}:workload-identity-directory/default/workload-identity/{HARNESS}-*"]},
        ]},
        description="Execution role for the IT Support demo harness",
    )

    step(f"AgentCore Harness {HARNESS}")
    found = [h for h in ac_ctl.list_harnesses().get("harnesses", []) if h.get("harnessName") == HARNESS]
    if found:
        hid = found[0]["harnessId"]
        print("already exists")
    else:
        hid = retry(lambda: ac_ctl.create_harness(
            harnessName=HARNESS,
            executionRoleArn=role_arn,
            model={"bedrockModelConfig": {"modelId": MODEL_ID, "maxTokens": 1024, "apiFormat": "converse_stream"}},
            systemPrompt=[{"text": IT_PROMPT}],
            tools=[{"type": "agentcore_gateway", "name": "it_tools",
                    "config": {"agentCoreGateway": {"gatewayArn": gateway_arn, "outboundAuth": {"awsIam": {}}}}}],
            memory={"disabled": {}},
            maxIterations=8,
            maxTokens=8000,
            timeoutSeconds=120,
            tags=TAGS,
        ), "create harness", {"ValidationException", "AccessDeniedException"})["harness"]["harnessId"]
        print(f"created {hid}")
    get = lambda: ac_ctl.get_harness(harnessId=hid)["harness"]
    status = wait_for(lambda: get()["status"], ok={"READY"}, bad={"CREATE_FAILED", "UPDATE_FAILED"},
                      what="harness", delay=10, attempts=60)
    h = get()
    print(status, h["arn"])
    return {"harness_id": hid, "harness_arn": h["arn"], "role_arn": role_arn}


# ---------------------------------------------------------------- smoke tests
def smoke_test_hr(agent):
    step("Smoke test: HR Assistant")
    resp = agent_rt.invoke_agent(
        agentId=agent["agent_id"], agentAliasId=agent["alias_id"], sessionId=str(uuid.uuid4()),
        inputText="How many annual leave days does employee E101 have left?", enableTrace=True,
    )
    text, steps = "", []
    for event in resp["completion"]:
        if "chunk" in event:
            text += event["chunk"]["bytes"].decode()
        elif "trace" in event:
            t = event["trace"]["trace"]
            steps.append(next(iter(t)))
    print("trace steps:", steps)
    print("answer:", text.strip())


def smoke_test_it(harness):
    step("Smoke test: IT Support")
    resp = ac_rt.invoke_harness(
        harnessArn=harness["harness_arn"], runtimeSessionId=f"smoke-{uuid.uuid4()}",
        messages=[{"role": "user", "content": [{"text": "What is the status of ticket INC-101?"}]}],
    )
    text, tools = "", []
    for event in resp["stream"]:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tools.append(start["toolUse"].get("name"))
        elif "contentBlockDelta" in event:
            text += event["contentBlockDelta"].get("delta", {}).get("text", "")
        elif any(k in event for k in ("validationException", "internalServerException", "runtimeClientError")):
            print("stream error:", event)
    print("tools used:", tools)
    print("answer:", text.strip())


def main():
    out = json.loads(OUTPUTS.read_text()) if OUTPUTS.exists() else {}
    lambda_role = ensure_lambda_role()
    hr_lambda = ensure_lambda(HR_LAMBDA, ROOT / "agents" / "hr_tools_lambda.py", lambda_role,
                              "HR Assistant action group tools (demo)")
    it_lambda = ensure_lambda(IT_LAMBDA, ROOT / "agents" / "it_tools_lambda.py", lambda_role,
                              "IT Support gateway tools (demo)")
    gid, garn, gver = ensure_guardrail()
    out["guardrail"] = {"id": gid, "arn": garn, "version": gver}
    out["hr_agent"] = {**ensure_hr_agent(hr_lambda, gid, garn, gver), "lambda_arn": hr_lambda}
    OUTPUTS.write_text(json.dumps(out, indent=2))
    out["it_gateway"] = {**ensure_gateway(it_lambda), "lambda_arn": it_lambda}
    out["it_harness"] = ensure_harness(out["it_gateway"]["gateway_arn"])
    OUTPUTS.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {OUTPUTS}")
    smoke_test_hr(out["hr_agent"])
    smoke_test_it(out["it_harness"])
    print("\nPhase 2 complete.")


if __name__ == "__main__":
    main()
