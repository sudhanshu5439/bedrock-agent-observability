"""Make one tiny model call and confirm it shows up in the invocation log group."""
import json
import time

from config import INVOCATION_LOG_GROUP, MODEL_ID, session

s = session()
resp = s.client("bedrock-runtime").converse(
    modelId=MODEL_ID,
    messages=[{"role": "user", "content": [{"text": "Reply with exactly: logging test ok"}]}],
)
print("model said:", resp["output"]["message"]["content"][0]["text"])

logs = s.client("logs")
start = int(time.time() * 1000) - 60_000
for _ in range(12):
    time.sleep(10)
    events = logs.filter_log_events(logGroupName=INVOCATION_LOG_GROUP, startTime=start)["events"]
    records = []
    for e in events:
        try:
            records.append(json.loads(e["message"]))
        except json.JSONDecodeError:
            print("non-JSON log line:", e["message"][:120])
    if records:
        r = records[-1]
        print("logged:", {
            "timestamp": r.get("timestamp"),
            "modelId": r.get("modelId"),
            "operation": r.get("operation"),
            "inputTokens": r.get("input", {}).get("inputTokenCount"),
            "outputTokens": r.get("output", {}).get("outputTokenCount"),
            "caller": r.get("identity", {}).get("arn"),
        })
        break
else:
    print("no invocation record yet")
