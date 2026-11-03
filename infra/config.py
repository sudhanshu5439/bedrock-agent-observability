"""Shared settings for the Bedrock observability demo.

Nothing account-specific is stored here. Override with environment variables:
  BEDROCK_DEMO_PROFILE       AWS CLI profile (default: genai-demo)
  BEDROCK_DEMO_REGION        region (default: us-east-1)
  BEDROCK_DEMO_BUDGET_EMAIL  where budget alerts go (needed only by phase1_foundation.py)
The account id is read from the active credentials.
"""
import functools
import os

import boto3

PROFILE = os.environ.get("BEDROCK_DEMO_PROFILE", "genai-demo")
REGION = os.environ.get("BEDROCK_DEMO_REGION", "us-east-1")
PREFIX = "bedrock-demo"
TAGS = {"Project": "bedrock-observability-demo"}

INVOCATION_LOG_GROUP = f"/{PREFIX}/model-invocations"
CLOUDTRAIL_LOG_GROUP = f"/{PREFIX}/cloudtrail"
TRACE_TABLE = f"{PREFIX}-agent-traces"
TRAIL_NAME = f"{PREFIX}-trail"
BUDGET_NAME = f"{PREFIX}-monthly-budget"
BUDGET_EMAIL = os.environ.get("BEDROCK_DEMO_BUDGET_EMAIL", "")
BUDGET_USD = "25"

INVOCATION_LOGGING_ROLE = f"{PREFIX}-invocation-logging-role"
CLOUDTRAIL_CWLOGS_ROLE = f"{PREFIX}-cloudtrail-cwlogs-role"

# Cheapest Claude model available in the account (cross-region inference profile).
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


@functools.cache
def session():
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


@functools.cache
def account_id():
    return session().client("sts").get_caller_identity()["Account"]


def __getattr__(name):
    # ACCOUNT_ID and LOG_BUCKET resolve on first use, so importing this module makes no AWS call.
    if name == "ACCOUNT_ID":
        return account_id()
    if name == "LOG_BUCKET":
        return f"{PREFIX}-logs-{account_id()}-{REGION}"
    raise AttributeError(name)


def tag_list(key="Key", value="Value"):
    return [{key: k, value: v} for k, v in TAGS.items()]
