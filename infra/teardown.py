"""Delete every resource the demo created. Dry run by default; pass --yes to actually delete.

    python infra/teardown.py          # list what would be deleted
    python infra/teardown.py --yes    # delete it

Only touches resources named bedrock-demo-* / bedrock_demo_* (plus the ids in infra/outputs.json).
Left alone on purpose (shared with other teams): Transaction Search, the aws/spans log group, the org trail,
ap-south-1 invocation logging. Model invocation logging in us-east-1 is removed only if it still points at
the demo log group.
"""
import argparse
import json
import time
from pathlib import Path

from botocore.exceptions import ClientError

from config import (
    ACCOUNT_ID, BUDGET_NAME, CLOUDTRAIL_CWLOGS_ROLE, INVOCATION_LOG_GROUP, INVOCATION_LOGGING_ROLE, LOG_BUCKET,
    PREFIX, TRACE_TABLE, TRAIL_NAME, session,
)

OUTPUTS = Path(__file__).with_name("outputs.json")
ROLES = [INVOCATION_LOGGING_ROLE, CLOUDTRAIL_CWLOGS_ROLE, f"{PREFIX}-tools-lambda-role", f"{PREFIX}-hr-agent-role",
         f"{PREFIX}-it-gateway-role", f"{PREFIX}-harness-role"]
LAMBDAS = [f"{PREFIX}-hr-tools", f"{PREFIX}-it-tools"]
LOG_GROUP_PREFIXES = [f"/{PREFIX}/", f"/aws/lambda/{PREFIX}-", "/aws/bedrock-agentcore/runtimes/harness_bedrock_demo_",
                      "/aws/bedrock-agentcore/runtimes/bedrock_demo_"]

s = session()
EXECUTE = False


def act(what, fn):
    """Print the action; run it only with --yes. Missing resources are not errors."""
    if not EXECUTE:
        print(f"  would delete  {what}")
        return
    try:
        fn()
        print(f"  deleted       {what}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "NoSuchEntity", "NoSuchBucket", "TrailNotFoundException",
                    "NotFoundException", "ResourceNotFound"):
            print(f"  already gone  {what}")
        else:
            print(f"  FAILED        {what}: {code} {e.response['Error'].get('Message', '')[:150]}")


def wait_gone(check, what, attempts=30, delay=5):
    if not EXECUTE:
        return
    for _ in range(attempts):
        try:
            check()
        except ClientError:
            return
        time.sleep(delay)
    print(f"  (still deleting {what}, continuing)")


def agentcore(out):
    print("\nAgentCore")
    ac = s.client("bedrock-agentcore-control")
    h = out.get("it_harness", {})
    if h:
        act(f"harness {h['harness_id']}", lambda: ac.delete_harness(harnessId=h["harness_id"]))
        wait_gone(lambda: ac.get_harness(harnessId=h["harness_id"]), "harness")
    g = out.get("it_gateway", {})
    if g:
        act(f"gateway target {g['target_id']}",
            lambda: ac.delete_gateway_target(gatewayIdentifier=g["gateway_id"], targetId=g["target_id"]))
        wait_gone(lambda: ac.get_gateway_target(gatewayIdentifier=g["gateway_id"], targetId=g["target_id"]), "target")
        act(f"gateway {g['gateway_id']}", lambda: ac.delete_gateway(gatewayIdentifier=g["gateway_id"]))
    try:
        ids, kw = [], {}
        while True:  # the API caps maxResults at 20
            r = ac.list_workload_identities(maxResults=20, **kw)
            ids += r.get("workloadIdentities", [])
            if not r.get("nextToken"):
                break
            kw = {"nextToken": r["nextToken"]}
        for w in ids:
            name = w.get("name", "")
            if name.startswith((f"{PREFIX}-", "bedrock_demo_", "harness_bedrock_demo_")):
                act(f"workload identity {name}", lambda n=name: ac.delete_workload_identity(name=n))
    except ClientError as e:
        print(f"  skipped workload identities: {e.response['Error']['Code']}")


def classic(out):
    print("\nClassic Bedrock agent + guardrail")
    ag = s.client("bedrock-agent")
    a = out.get("hr_agent", {})
    if a:
        act(f"agent alias {a['alias_id']}", lambda: ag.delete_agent_alias(agentId=a["agent_id"], agentAliasId=a["alias_id"]))
        time.sleep(3 if EXECUTE else 0)
        act(f"agent {a['agent_id']}", lambda: ag.delete_agent(agentId=a["agent_id"], skipResourceInUseCheck=True))
    gr = out.get("guardrail", {})
    if gr:
        act(f"guardrail {gr['id']} (all versions)",
            lambda: s.client("bedrock").delete_guardrail(guardrailIdentifier=gr["id"]))


def lambdas():
    print("\nLambda")
    lam = s.client("lambda")
    for fn in LAMBDAS:
        act(f"function {fn}", lambda f=fn: lam.delete_function(FunctionName=f))


def logging_and_audit():
    print("\nLogging, audit, storage")
    bedrock = s.client("bedrock")
    cfg = bedrock.get_model_invocation_logging_configuration().get("loggingConfig") or {}
    if cfg.get("cloudWatchConfig", {}).get("logGroupName") == INVOCATION_LOG_GROUP:
        act("model invocation logging config (us-east-1, points at demo log group)",
            bedrock.delete_model_invocation_logging_configuration)
    else:
        print("  keep          model invocation logging (not ours or not set)")
    act(f"CloudTrail trail {TRAIL_NAME}", lambda: s.client("cloudtrail").delete_trail(Name=TRAIL_NAME))
    act(f"DynamoDB table {TRACE_TABLE}", lambda: s.client("dynamodb").delete_table(TableName=TRACE_TABLE))

    logs = s.client("logs")
    for prefix in LOG_GROUP_PREFIXES:
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for lg in page["logGroups"]:
                act(f"log group {lg['logGroupName']}", lambda n=lg["logGroupName"]: logs.delete_log_group(logGroupName=n))

    def empty_and_delete():
        b = s.resource("s3").Bucket(LOG_BUCKET)
        b.object_versions.delete()
        b.objects.all().delete()
        b.delete()
    act(f"S3 bucket {LOG_BUCKET} (and all objects)", empty_and_delete)
    act(f"budget {BUDGET_NAME}", lambda: s.client("budgets").delete_budget(AccountId=ACCOUNT_ID, BudgetName=BUDGET_NAME))


def iam():
    print("\nIAM")
    client = s.client("iam")
    for role in ROLES:
        def delete_role(r=role):
            for p in client.list_attached_role_policies(RoleName=r)["AttachedPolicies"]:
                client.detach_role_policy(RoleName=r, PolicyArn=p["PolicyArn"])
            client.delete_role(RoleName=r)
        act(f"role {role}", delete_role)
        policy_arn = f"arn:aws:iam::{ACCOUNT_ID}:policy/{role}-policy"

        def delete_policy(arn=policy_arn):
            for v in client.list_policy_versions(PolicyArn=arn)["Versions"]:
                if not v["IsDefaultVersion"]:
                    client.delete_policy_version(PolicyArn=arn, VersionId=v["VersionId"])
            client.delete_policy(PolicyArn=arn)
        if role != f"{PREFIX}-tools-lambda-role":  # that role only uses the AWS-managed basic execution policy
            act(f"policy {role}-policy", delete_policy)


def main():
    global EXECUTE
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    EXECUTE = ap.parse_args().yes
    out = json.loads(OUTPUTS.read_text()) if OUTPUTS.exists() else {}
    print("DELETING demo resources" if EXECUTE else "DRY RUN - nothing will be deleted (pass --yes to delete)")
    agentcore(out)
    classic(out)
    lambdas()
    logging_and_audit()
    iam()  # last: services above may still be using the roles while they delete
    print("\nDone." if EXECUTE else "\nDry run complete.")


if __name__ == "__main__":
    main()
