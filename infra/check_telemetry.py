"""Confirm the demo agents' activity reached each telemetry source (last 60 minutes)."""
import json
import time

from config import CLOUDTRAIL_LOG_GROUP, INVOCATION_LOG_GROUP, session

s = session()
logs = s.client("logs")
now = int(time.time())
start = now - 3600


def insights(group, query):
    try:
        qid = logs.start_query(logGroupName=group, startTime=start, endTime=now, queryString=query)["queryId"]
    except logs.exceptions.ResourceNotFoundException:
        return "log group not found"
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            return [{f["field"]: f["value"] for f in row} for row in r["results"]]
        time.sleep(1)


print("Model invocation logs by caller:")
print(json.dumps(insights(INVOCATION_LOG_GROUP,
                          "stats count(*) as calls, sum(input.inputTokenCount) as in_tok, "
                          "sum(output.outputTokenCount) as out_tok by identity.arn"), indent=1))
print("\nCloudTrail data events (demo trail):")
print(json.dumps(insights(CLOUDTRAIL_LOG_GROUP,
                          "stats count(*) as events by eventSource, eventName"), indent=1))
print("\nAgentCore spans (aws/spans) for the demo harness:")
print(json.dumps(insights("aws/spans",
                          "filter @message like /bedrock_demo_it_support/ | stats count(*) as spans by name "
                          "| sort spans desc | limit 12"), indent=1))
