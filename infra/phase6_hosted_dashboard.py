"""Phase 6: host the dashboard on ECS Fargate with a public IP, open only to listed IPs, behind a password.

    python phase6_hosted_dashboard.py deploy --allow 203.0.113.7 --allow 198.51.100.0/24 [--ref <git sha>]
    python phase6_hosted_dashboard.py allow  203.0.113.7 198.51.100.9     # replace the allowed IPs
    python phase6_hosted_dashboard.py url                                  # current link (changes on restart)
    python phase6_hosted_dashboard.py stop | start                         # pause / resume (stop = no compute cost)
    python phase6_hosted_dashboard.py password                             # print the dashboard password

No image build: the task runs the public Python image, downloads this repository at --ref from GitHub,
and starts app/serve.py. It uses a read-only task role (plus invoke rights for the Live test tab), never
personal credentials. Plain HTTP on port 8501: access is limited by the security group and the password.
Removed by teardown.py.
"""
import argparse
import json
import secrets
import sys
import time
import urllib.request
from pathlib import Path

from botocore.exceptions import ClientError

from config import ACCOUNT_ID, PREFIX, REGION, TAGS, TRACE_TABLE, session, tag_list
from phase1_foundation import ensure_role, step

OUTPUTS_FILE = Path(__file__).with_name("outputs.json")
REPO_TARBALL = "https://github.com/sudhanshu5439/bedrock-agent-observability/archive/{ref}.tar.gz"
IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"
PORT = 8501

CLUSTER = f"{PREFIX}-cluster"
SERVICE = f"{PREFIX}-dashboard"
FAMILY = f"{PREFIX}-dashboard"
SG_NAME = f"{PREFIX}-dashboard-sg"
SECRET = f"{PREFIX}/dashboard-password"
LOG_GROUP = f"/{PREFIX}/dashboard"
EXEC_ROLE = f"{PREFIX}-dashboard-exec-role"
TASK_ROLE = f"{PREFIX}-dashboard-task-role"

# Runs inside the container before the app: fetch the code at the pinned ref, write the resource ids.
BOOTSTRAP = (
    "import io,os,tarfile,urllib.request;"
    "b=urllib.request.urlopen(os.environ['CODE_URL'],timeout=120).read();"
    "t=tarfile.open(fileobj=io.BytesIO(b));root=t.getmembers()[0].name.split('/')[0];"
    "t.extractall('/tmp/src',filter='data');os.rename('/tmp/src/'+root,'/app');"
    "open('/app/infra/outputs.json','w').write(os.environ['OUTPUTS_JSON'])"
)
COMMAND = (
    'python -c "$BOOTSTRAP" && cd /app && pip install --no-cache-dir -q -r requirements.txt && '
    f"exec python app/serve.py --server.port {PORT} --server.address 0.0.0.0 --server.headless true"
)

s = session()
ec2, ecs, logs, sm = (s.client(n) for n in ("ec2", "ecs", "logs", "secretsmanager"))


def outputs():
    return json.loads(OUTPUTS_FILE.read_text())


def cidr(ip):
    return ip if "/" in ip else f"{ip}/32"


# ------------------------------------------------------------------ building blocks
def ensure_secret():
    step(f"Secret {SECRET}")
    try:
        arn = sm.describe_secret(SecretId=SECRET)["ARN"]
        print("already exists")
    except sm.exceptions.ResourceNotFoundException:
        arn = sm.create_secret(Name=SECRET, SecretString=secrets.token_urlsafe(12),
                               Description="Password for the hosted Bedrock observability dashboard",
                               Tags=tag_list())["ARN"]
        print("created")
    return arn


def ensure_log_group():
    step(f"Log group {LOG_GROUP}")
    try:
        logs.create_log_group(logGroupName=LOG_GROUP, tags=TAGS)
        print("created")
    except logs.exceptions.ResourceAlreadyExistsException:
        print("already exists")
    logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)


def ensure_roles(secret_arn, out):
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}}}]}
    exec_arn = ensure_role(EXEC_ROLE, trust, {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:{LOG_GROUP}:*"},
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arn},
    ]}, "ECS execution role for the hosted dashboard: logs and its password secret")

    a = f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}"
    ac = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}"
    lg = f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group"
    agent_id = out["hr_agent"]["agent_id"]
    table = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TRACE_TABLE}"
    task_arn = ensure_role(TASK_ROLE, trust, {"Version": "2012-10-17", "Statement": [
        {"Sid": "TraceStore", "Effect": "Allow", "Action": ["dynamodb:Query", "dynamodb:PutItem"],
         "Resource": [table, f"{table}/index/*"]},
        {"Sid": "LogQueries", "Effect": "Allow", "Action": ["logs:StartQuery", "logs:FilterLogEvents"],
         "Resource": [f"{lg}:/{PREFIX}/*", f"{lg}:aws/spans", f"{lg}:/aws/lambda/{PREFIX}-*",
                      f"{lg}:/{PREFIX}/*:*", f"{lg}:aws/spans:*", f"{lg}:/aws/lambda/{PREFIX}-*:*"]},
        {"Sid": "LogQueryResults", "Effect": "Allow", "Action": ["logs:GetQueryResults", "logs:StopQuery"],
         "Resource": "*"},
        {"Sid": "ReadOnlyLookups", "Effect": "Allow",
         "Action": ["cloudtrail:LookupEvents", "cloudwatch:GetMetricData"], "Resource": "*"},
        {"Sid": "ClassicAgentConfig", "Effect": "Allow",
         "Action": ["bedrock:GetAgent", "bedrock:GetAgentAlias", "bedrock:ListAgentVersions",
                    "bedrock:ListAgentActionGroups", "bedrock:GetAgentActionGroup", "bedrock:GetGuardrail"],
         "Resource": [f"{a}:agent/{agent_id}", f"{a}:agent-alias/{agent_id}/*",
                      out["guardrail"]["arn"], f"{out['guardrail']['arn']}:*"]},
        {"Sid": "AgentCoreConfig", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetHarness", "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:GetGatewayTarget"],
         "Resource": [out["it_harness"]["harness_arn"], out["it_gateway"]["gateway_arn"],
                      f"{out['it_gateway']['gateway_arn']}/*"]},
        {"Sid": "ToolLambdaConfig", "Effect": "Allow", "Action": "lambda:GetFunctionConfiguration",
         "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{PREFIX}-*"},
        {"Sid": "LiveTest", "Effect": "Allow",
         "Action": ["bedrock:InvokeAgent", "bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"],
         "Resource": [f"{a}:agent-alias/{agent_id}/{out['hr_agent']['alias_id']}", out["it_harness"]["harness_arn"],
                      f"{ac}:runtime/harness_bedrock_demo_it_support-*"]},
    ]}, "Hosted dashboard task role: read the demo's records, invoke the two demo agents")
    return exec_arn, task_arn


def default_network():
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = [x["SubnetId"] for x in ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc]}, {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]]
    return vpc, subnets


def ensure_sg(vpc):
    step(f"Security group {SG_NAME}")
    found = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]},
                                                  {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if found:
        print("already exists")
        return found[0]["GroupId"]
    gid = ec2.create_security_group(GroupName=SG_NAME, VpcId=vpc,
                                    Description="Hosted Bedrock observability dashboard: listed IPs only",
                                    TagSpecifications=[{"ResourceType": "security-group", "Tags": tag_list()}])["GroupId"]
    print("created")
    return gid


def set_allowed(gid, ips):
    """Make the port-8501 ingress rules exactly the given IPs."""
    step(f"Allowed IPs on port {PORT}: {', '.join(ips)}")
    current = ec2.describe_security_groups(GroupIds=[gid])["SecurityGroups"][0]["IpPermissions"]
    stale = [p for p in current if p.get("FromPort") == PORT]
    if stale:
        ec2.revoke_security_group_ingress(GroupId=gid, IpPermissions=stale)
    ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": PORT, "ToPort": PORT,
        "IpRanges": [{"CidrIp": cidr(ip), "Description": "dashboard viewer"} for ip in ips]}])
    print("updated")


def register_task(exec_arn, task_arn, secret_arn, out, ref):
    step(f"Task definition {FAMILY} (code at {ref})")
    env = {"BOOTSTRAP": BOOTSTRAP, "CODE_URL": REPO_TARBALL.format(ref=ref), "OUTPUTS_JSON": json.dumps(out),
           "BEDROCK_DEMO_PROFILE": "", "BEDROCK_DEMO_REGION": REGION, "PYTHONUNBUFFERED": "1"}
    arn = ecs.register_task_definition(
        family=FAMILY, requiresCompatibilities=["FARGATE"], networkMode="awsvpc", cpu="512", memory="1024",
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        executionRoleArn=exec_arn, taskRoleArn=task_arn, tags=tag_list("key", "value"),
        containerDefinitions=[{
            "name": "dashboard", "image": IMAGE, "essential": True,
            "entryPoint": ["sh", "-c"], "command": [COMMAND],
            "portMappings": [{"containerPort": PORT, "protocol": "tcp"}],
            "environment": [{"name": k, "value": v} for k, v in env.items()],
            "secrets": [{"name": "DASHBOARD_PASSWORD", "valueFrom": secret_arn}],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": LOG_GROUP, "awslogs-region": REGION, "awslogs-stream-prefix": "dashboard"}},
            "healthCheck": {"command": ["CMD-SHELL", "python -c \"import urllib.request;"
                                        f"urllib.request.urlopen('http://127.0.0.1:{PORT}/_stcore/health',timeout=5)\""],
                            "interval": 30, "timeout": 10, "retries": 3, "startPeriod": 300},
        }],
    )["taskDefinition"]["taskDefinitionArn"]
    print(arn.split("/")[-1])
    return arn


def ensure_cluster():
    step(f"ECS cluster {CLUSTER}")
    c = ecs.describe_clusters(clusters=[CLUSTER])["clusters"]
    if c and c[0]["status"] == "ACTIVE":
        print("already exists")
        return
    ecs.create_cluster(clusterName=CLUSTER, capacityProviders=["FARGATE"], tags=tag_list("key", "value"))
    print("created")


def ensure_service(task_def, subnets, gid):
    step(f"ECS service {SERVICE}")
    svc = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"]
    if svc and svc[0]["status"] == "ACTIVE":
        ecs.update_service(cluster=CLUSTER, service=SERVICE, taskDefinition=task_def, desiredCount=1,
                           forceNewDeployment=True)
        print("updated, new deployment started")
        return
    for attempt in range(8):  # new IAM roles take a few seconds to be usable by ECS
        try:
            ecs.create_service(
                cluster=CLUSTER, serviceName=SERVICE, taskDefinition=task_def, desiredCount=1, launchType="FARGATE",
                networkConfiguration={"awsvpcConfiguration": {"subnets": subnets, "securityGroups": [gid],
                                                              "assignPublicIp": "ENABLED"}},
                deploymentConfiguration={"minimumHealthyPercent": 0, "maximumPercent": 100},
                tags=tag_list("key", "value"), propagateTags="SERVICE")
            print("created")
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidParameterException" or attempt == 7:
                raise
            print(f"  waiting for IAM ({e.response['Error']['Message'][:80]})")
            time.sleep(10)


def current_url(wait_s=0):
    deadline = time.time() + wait_s
    while True:
        tasks = ecs.list_tasks(cluster=CLUSTER, serviceName=SERVICE, desiredStatus="RUNNING")["taskArns"]
        if tasks:
            t = ecs.describe_tasks(cluster=CLUSTER, tasks=tasks)["tasks"]
            t = max(t, key=lambda x: x.get("startedAt") or x["createdAt"])
            eni = next((d["value"] for a in t.get("attachments", []) for d in a["details"]
                        if d["name"] == "networkInterfaceId"), None)
            if eni and t["lastStatus"] == "RUNNING":
                ip = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])["NetworkInterfaces"][0] \
                    .get("Association", {}).get("PublicIp")
                if ip:
                    return f"http://{ip}:{PORT}"
        if time.time() > deadline:
            return None
        time.sleep(10)


def wait_healthy(url, wait_s=480):
    step("Waiting for the dashboard to answer (installs packages on first start, ~2-4 min)")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(f"{url}/_stcore/health", timeout=5).read() == b"ok":  # noqa: S310
                print("healthy")
                return True
        except OSError:
            pass
        time.sleep(10)
    print(f"not answering yet; check the logs in {LOG_GROUP}")
    return False


# ------------------------------------------------------------------ commands
def deploy(args):
    out = outputs()
    secret_arn = ensure_secret()
    ensure_log_group()
    exec_arn, task_arn = ensure_roles(secret_arn, out)
    vpc, subnets = default_network()
    gid = ensure_sg(vpc)
    set_allowed(gid, args.allow)
    ensure_cluster()
    task_def = register_task(exec_arn, task_arn, secret_arn, out, args.ref)
    ensure_service(task_def, subnets, gid)
    step("Waiting for the task to start")
    url = current_url(wait_s=600)
    if not url:
        sys.exit("task did not start; see the ECS console events for the service")
    print(url)
    wait_healthy(url)
    print(f"\nDashboard: {url}\nPassword:  python phase6_hosted_dashboard.py password")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deploy")
    d.add_argument("--allow", action="append", required=True, help="viewer IP or CIDR; repeat for more")
    d.add_argument("--ref", default="main", help="git commit or branch of the GitHub repo to run")
    a = sub.add_parser("allow")
    a.add_argument("ips", nargs="+")
    for name in ("url", "stop", "start", "password"):
        sub.add_parser(name)
    args = ap.parse_args()

    if args.cmd == "deploy":
        deploy(args)
    elif args.cmd == "allow":
        vpc, _ = default_network()
        set_allowed(ensure_sg(vpc), args.ips)
    elif args.cmd == "url":
        print(current_url() or "not running")
    elif args.cmd in ("stop", "start"):
        ecs.update_service(cluster=CLUSTER, service=SERVICE, desiredCount=0 if args.cmd == "stop" else 1)
        print(f"{args.cmd} requested" + ("; run `url` in ~3 minutes for the new address" if args.cmd == "start" else ""))
    elif args.cmd == "password":
        print(sm.get_secret_value(SecretId=SECRET)["SecretString"])


if __name__ == "__main__":
    main()
