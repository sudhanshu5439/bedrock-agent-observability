"""Phase 1: audit and logging foundation for the Bedrock observability demo.

Creates (idempotent, safe to re-run):
  - S3 bucket for invocation logs and CloudTrail
  - CloudWatch log groups for model invocation logs and CloudTrail
  - IAM roles for Bedrock log delivery and CloudTrail -> CloudWatch Logs
  - Model invocation logging (us-east-1 only; refuses to overwrite a non-demo config)
  - A CloudTrail trail recording Bedrock agent / AgentCore data events
  - DynamoDB table for classic Bedrock Agent traces
  - An account-level monthly budget with email alerts
"""
import json
import time

from botocore.exceptions import ClientError

from config import (
    ACCOUNT_ID, BUDGET_EMAIL, BUDGET_NAME, BUDGET_USD, CLOUDTRAIL_CWLOGS_ROLE,
    CLOUDTRAIL_LOG_GROUP, INVOCATION_LOG_GROUP, INVOCATION_LOGGING_ROLE,
    LOG_BUCKET, REGION, TAGS, TRACE_TABLE, TRAIL_NAME, session, tag_list,
)

s = session()
s3 = s.client("s3")
logs = s.client("logs")
iam = s.client("iam")
bedrock = s.client("bedrock")
cloudtrail = s.client("cloudtrail")
ddb = s.client("dynamodb")
budgets = s.client("budgets")

TRAIL_ARN = f"arn:aws:cloudtrail:{REGION}:{ACCOUNT_ID}:trail/{TRAIL_NAME}"


def step(msg):
    print(f"\n==> {msg}")


def create_bucket():
    step(f"S3 bucket {LOG_BUCKET}")
    try:
        s3.create_bucket(Bucket=LOG_BUCKET)
        print("created")
    except ClientError as e:
        if e.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
        print("already exists")
    s3.put_public_access_block(
        Bucket=LOG_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_tagging(Bucket=LOG_BUCKET, Tagging={"TagSet": tag_list()})
    s3.put_bucket_lifecycle_configuration(
        Bucket=LOG_BUCKET,
        LifecycleConfiguration={"Rules": [{
            "ID": "expire-demo-logs", "Status": "Enabled",
            "Filter": {"Prefix": ""}, "Expiration": {"Days": 30},
        }]},
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockInvocationLogsWrite",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{LOG_BUCKET}/invocation-logs/*",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:*"},
                },
            },
            {
                "Sid": "CloudTrailAclCheck",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:GetBucketAcl",
                "Resource": f"arn:aws:s3:::{LOG_BUCKET}",
                "Condition": {"StringEquals": {"aws:SourceArn": TRAIL_ARN}},
            },
            {
                "Sid": "CloudTrailWrite",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{LOG_BUCKET}/cloudtrail/AWSLogs/{ACCOUNT_ID}/*",
                "Condition": {"StringEquals": {
                    "s3:x-amz-acl": "bucket-owner-full-control",
                    "aws:SourceArn": TRAIL_ARN,
                }},
            },
        ],
    }
    s3.put_bucket_policy(Bucket=LOG_BUCKET, Policy=json.dumps(policy))
    print("public access blocked, tagged, 30-day expiry, bucket policy set")


def create_log_group(name):
    step(f"Log group {name}")
    try:
        logs.create_log_group(logGroupName=name, tags=TAGS)
        print("created")
    except logs.exceptions.ResourceAlreadyExistsException:
        print("already exists")
    logs.put_retention_policy(logGroupName=name, retentionInDays=30)
    return f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:{name}"


def ensure_role(role_name, trust, policy_doc, description):
    """Create a role plus a customer-managed policy (inline policies aren't allowed for this user)."""
    step(f"IAM role {role_name}")
    try:
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=description,
        )["Role"]["Arn"]
        print("created")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        print("already exists")

    policy_name = f"{role_name}-policy"
    policy_arn = f"arn:aws:iam::{ACCOUNT_ID}:policy/{policy_name}"
    try:
        iam.create_policy(PolicyName=policy_name, PolicyDocument=json.dumps(policy_doc))
        print(f"policy {policy_name} created")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"policy {policy_name} already exists")
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return role_arn


def create_roles(inv_lg_arn, ct_lg_arn):
    inv_role = ensure_role(
        INVOCATION_LOGGING_ROLE,
        trust={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:*"},
                },
            }],
        },
        policy_doc={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"{inv_lg_arn}:log-stream:*",
                },
                {
                    "Effect": "Allow",
                    "Action": "s3:PutObject",
                    "Resource": f"arn:aws:s3:::{LOG_BUCKET}/invocation-logs/*",
                },
            ],
        },
        description="Bedrock model invocation logging delivery (bedrock observability demo)",
    )
    ct_role = ensure_role(
        CLOUDTRAIL_CWLOGS_ROLE,
        trust={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceArn": TRAIL_ARN}},
            }],
        },
        policy_doc={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"{ct_lg_arn}:log-stream:*",
            }],
        },
        description="CloudTrail to CloudWatch Logs delivery (bedrock observability demo)",
    )
    return inv_role, ct_role


def retry(fn, what, attempts=12, delay=10):
    """New IAM roles take a few seconds to propagate; retry validation errors."""
    for i in range(attempts):
        try:
            return fn()
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ValidationException", "InvalidCloudWatchLogsRoleArnException",
                        "InvalidCloudWatchLogsLogGroupArnException", "InsufficientS3BucketPolicyException",
                        "AccessDeniedException") and i < attempts - 1:
                print(f"  {what}: {code}, waiting for IAM propagation ({i + 1}/{attempts})...")
                time.sleep(delay)
                continue
            raise


def enable_invocation_logging(inv_role):
    step(f"Model invocation logging in {REGION}")
    current = bedrock.get_model_invocation_logging_configuration().get("loggingConfig")
    if current:
        cw = current.get("cloudWatchConfig", {}).get("logGroupName")
        if cw != INVOCATION_LOG_GROUP:
            raise SystemExit(
                f"Refusing to overwrite existing invocation logging config (log group {cw}). "
                "It belongs to someone else."
            )
    retry(lambda: bedrock.put_model_invocation_logging_configuration(loggingConfig={
        "cloudWatchConfig": {
            "logGroupName": INVOCATION_LOG_GROUP,
            "roleArn": inv_role,
            "largeDataDeliveryS3Config": {"bucketName": LOG_BUCKET, "keyPrefix": "invocation-logs/large-data"},
        },
        "s3Config": {"bucketName": LOG_BUCKET, "keyPrefix": "invocation-logs"},
        "textDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": False,
        "embeddingDataDeliveryEnabled": False,
    }), "invocation logging")
    print("enabled (CloudWatch + S3)")


def create_trail(ct_lg_arn, ct_role):
    step(f"CloudTrail trail {TRAIL_NAME}")
    try:
        retry(lambda: cloudtrail.create_trail(
            Name=TRAIL_NAME,
            S3BucketName=LOG_BUCKET,
            S3KeyPrefix="cloudtrail",
            IsMultiRegionTrail=False,
            CloudWatchLogsLogGroupArn=f"{ct_lg_arn}:*",
            CloudWatchLogsRoleArn=ct_role,
            TagsList=tag_list(),
        ), "create trail")
        print("created")
    except cloudtrail.exceptions.TrailAlreadyExistsException:
        print("already exists")

    # Data events only: management events are already captured by the company trail.
    wanted = {
        "Bedrock agent invocations": "AWS::Bedrock::AgentAlias",
        "AgentCore runtime invocations": "AWS::BedrockAgentCore::Runtime",
        "Bedrock guardrail evaluations": "AWS::Bedrock::Guardrail",
    }
    accepted = []
    for name, rtype in wanted.items():
        candidate = accepted + [{"Name": name, "FieldSelectors": [
            {"Field": "eventCategory", "Equals": ["Data"]},
            {"Field": "resources.type", "Equals": [rtype]},
        ]}]
        try:
            cloudtrail.put_event_selectors(TrailName=TRAIL_NAME, AdvancedEventSelectors=candidate)
            accepted = candidate
            print(f"data events: {rtype} OK")
        except ClientError as e:
            print(f"data events: {rtype} not supported here ({e.response['Error']['Code']}), skipped")
    cloudtrail.start_logging(Name=TRAIL_NAME)
    print("logging started")


def create_trace_table():
    step(f"DynamoDB table {TRACE_TABLE}")
    try:
        ddb.create_table(
            TableName=TRACE_TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "event_ts", "AttributeType": "S"},
                {"AttributeName": "agent_id", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "session_id", "KeyType": "HASH"},
                {"AttributeName": "event_ts", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "agent_id-event_ts",
                "KeySchema": [
                    {"AttributeName": "agent_id", "KeyType": "HASH"},
                    {"AttributeName": "event_ts", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            Tags=tag_list(),
        )
        print("creating...")
    except ddb.exceptions.ResourceInUseException:
        print("already exists")
    ddb.get_waiter("table_exists").wait(TableName=TRACE_TABLE)
    try:
        ddb.update_time_to_live(
            TableName=TRACE_TABLE,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
        )
    except ClientError as e:
        if "already enabled" not in str(e):
            raise
    print("active, TTL on expires_at")


def create_budget():
    step(f"Budget {BUDGET_NAME} (${BUDGET_USD}/month, whole account)")
    if not BUDGET_EMAIL:
        print("skipped: set BEDROCK_DEMO_BUDGET_EMAIL to create the budget alarm")
        return
    try:
        budgets.create_budget(
            AccountId=ACCOUNT_ID,
            Budget={
                "BudgetName": BUDGET_NAME,
                "BudgetLimit": {"Amount": BUDGET_USD, "Unit": "USD"},
                "TimeUnit": "MONTHLY",
                "BudgetType": "COST",
            },
            NotificationsWithSubscribers=[
                {
                    "Notification": {"NotificationType": nt, "ComparisonOperator": "GREATER_THAN",
                                     "Threshold": th, "ThresholdType": "PERCENTAGE"},
                    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": BUDGET_EMAIL}],
                }
                for nt, th in (("ACTUAL", 80.0), ("FORECASTED", 100.0))
            ],
        )
        print(f"created, alerts to {BUDGET_EMAIL}")
    except budgets.exceptions.DuplicateRecordException:
        print("already exists")


def main():
    create_bucket()
    inv_lg_arn = create_log_group(INVOCATION_LOG_GROUP)
    ct_lg_arn = create_log_group(CLOUDTRAIL_LOG_GROUP)
    inv_role, ct_role = create_roles(inv_lg_arn, ct_lg_arn)
    enable_invocation_logging(inv_role)
    create_trail(ct_lg_arn, ct_role)
    create_trace_table()
    create_budget()
    print("\nPhase 1 complete.")


if __name__ == "__main__":
    main()
