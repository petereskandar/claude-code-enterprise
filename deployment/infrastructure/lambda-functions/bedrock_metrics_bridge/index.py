# ABOUTME: Lambda function that bridges Bedrock inference profile metrics to OTEL log format
# ABOUTME: Reads InputTokenCount/OutputTokenCount/CacheRead/CacheWrite from AWS/Bedrock
# ABOUTME: CloudWatch namespace per InferenceProfileArn, maps ARN to user email via
# ABOUTME: DynamoDB InferenceProfileMapping table (populated by the provisioner Lambda),
# ABOUTME: then writes structured JSON log records to /aws/claude-code/metrics in exactly
# ABOUTME: the same format the ADOT awsemf exporter would produce.

import json
import boto3
import os
from datetime import datetime, timedelta, timezone

# Clients
cloudwatch_client = boto3.client("cloudwatch")
logs_client = boto3.client("logs")
dynamodb_resource = boto3.resource("dynamodb")
boto3_session = boto3.session.Session()

# Configuration
LOG_GROUP = os.environ.get("METRICS_LOG_GROUP", "/aws/claude-code/metrics")
LOG_STREAM = "bedrock-metrics-bridge"
MAPPING_TABLE_NAME = os.environ.get("INFERENCE_PROFILE_MAPPING_TABLE", "InferenceProfileMapping")
AGGREGATION_WINDOW = 5    # minutes — must match the EventBridge schedule rate
METRIC_PERIOD = 300       # seconds — granularity of AWS/Bedrock datapoints (5 min)
# Query slightly more than one period back to absorb CloudWatch ingestion delay
# (~1-2 min) without double-counting: we ask for exactly one METRIC_PERIOD window
# ending 2 minutes ago so the datapoint has had time to be ingested.
INGESTION_DELAY = 2       # minutes


def lambda_handler(event, context):
    print(f"Starting Bedrock metrics bridge → {LOG_GROUP}")

    # Shift the window back by INGESTION_DELAY to ensure the most recent
    # 5-minute Bedrock datapoint has been ingested before we query it.
    end_time = datetime.now(timezone.utc) - timedelta(minutes=INGESTION_DELAY)
    start_time = end_time - timedelta(minutes=AGGREGATION_WINDOW)

    # 1. Build ARN → { email, model } map
    arn_to_profile = _build_arn_to_profile_map()
    if not arn_to_profile:
        print("No inference profiles with email tags found — nothing to bridge")
        return {"statusCode": 200, "body": "No inference profiles found"}

    print(f"Found {len(arn_to_profile)} inference profile(s)")

    # 2. Fetch token counts from AWS/Bedrock namespace
    bedrock_metrics = _get_bedrock_metrics(list(arn_to_profile.keys()), start_time, end_time)

    # 3. Ensure log group and stream exist
    _ensure_log_stream(LOG_GROUP, LOG_STREAM)

    # 4. Build log events in OTEL/awsemf format and put them
    log_events = _build_log_events(arn_to_profile, bedrock_metrics, end_time)
    if log_events:
        _put_log_events(LOG_GROUP, LOG_STREAM, log_events)
        print(f"Published {len(log_events)} log event(s) to {LOG_GROUP}")
    else:
        print("No token activity in this window — no log events written")

    return {"statusCode": 200, "body": json.dumps(f"Published {len(log_events)} log events")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_arn_to_profile_map() -> dict:
    """
    Return { inferenceProfileArn: { "email": str, "model": str } } by scanning
    the InferenceProfileMapping DynamoDB table (populated by the provisioner Lambda).
    """
    result = {}
    try:
        table = dynamodb_resource.Table(MAPPING_TABLE_NAME)
        response = table.scan()
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        for item in items:
            arn = item.get("profileArn")
            email = item.get("email")
            model = item.get("model", "unknown")
            if arn and email:
                result[arn] = {"email": email, "model": model}

        print(f"Loaded {len(result)} profile(s) from DynamoDB mapping table")

    except Exception as e:
        print(f"Error scanning InferenceProfileMapping table: {e}")
    return result


def _get_bedrock_metrics(profile_arns: list, start_time: datetime, end_time: datetime) -> dict:
    """
    Fetch token counts from AWS/Bedrock namespace for each profile ARN.
    Returns { arn: { "input": N, "output": N, "cache_read": N, "cache_write": N } }
    """
    metric_names = [
        ("InputTokenCount",           "input"),
        ("OutputTokenCount",          "output"),
        ("CacheReadInputTokenCount",  "cache_read"),
        ("CacheWriteInputTokenCount", "cache_write"),
    ]

    queries = []
    index = {}  # query id → (arn, key)
    for i, arn in enumerate(profile_arns):
        # AWS/Bedrock metrics use ModelId dimension with the profile's short ID
        # e.g. arn:.../application-inference-profile/82ey7vcct4m3 → "82ey7vcct4m3"
        profile_id = arn.split("/")[-1]
        for cw_name, key in metric_names:
            qid = f"q{i}_{key}"
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Bedrock",
                        "MetricName": cw_name,
                        "Dimensions": [{"Name": "ModelId", "Value": profile_id}],
                    },
                    "Period": METRIC_PERIOD,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            })
            index[qid] = (arn, key)

    results = {arn: {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
               for arn in profile_arns}

    # get_metric_data max 500 queries per call
    for batch_start in range(0, len(queries), 500):
        batch = queries[batch_start:batch_start + 500]
        try:
            response = cloudwatch_client.get_metric_data(
                MetricDataQueries=batch,
                StartTime=start_time,
                EndTime=end_time,
            )
            for r in response.get("MetricDataResults", []):
                values = r.get("Values", [])
                qid = r["Id"]
                if qid in index:
                    arn, key = index[qid]
                    if values:
                        total = sum(values)
                        results[arn][key] += total
                        print(f"  {qid}: {len(values)} datapoint(s), total={total:.0f}")
                    else:
                        print(f"  {qid}: no datapoints")
        except Exception as e:
            print(f"Error fetching Bedrock metrics: {e}")

    # Log summary
    active = {arn: v for arn, v in results.items() if any(v.values())}
    print(f"Active ARNs with data: {len(active)}/{len(profile_arns)}")

    return results


def _build_log_events(arn_to_profile: dict, bedrock_metrics: dict, timestamp: datetime) -> list:
    """
    Produce one CloudWatch Logs event per (user, token_type) combination, in the
    same JSON structure the ADOT awsemf exporter writes.
    """
    ts_ms = int(timestamp.timestamp() * 1000)
    events = []

    type_map = [
        ("input",       "input"),
        ("output",      "output"),
        ("cache_read",  "cacheRead"),
        ("cache_write", "cacheCreation"),
    ]

    for arn, counts in bedrock_metrics.items():
        profile = arn_to_profile.get(arn)
        if not profile:
            continue

        email = profile["email"]
        model = profile["model"]

        for metric_key, otel_type in type_map:
            count = counts.get(metric_key, 0.0)
            if count <= 0:
                continue

            record = {
                "_aws": {
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "ClaudeCode",
                            "Dimensions": [
                                ["type"],
                                ["model"],
                                ["user.email"],
                                ["type", "model"],
                                ["type", "user.email"],
                            ],
                            "Metrics": [
                                {"Name": "claude_code.token.usage", "Unit": "Count"},
                            ],
                        }
                    ],
                    "Timestamp": ts_ms,
                },
                "user.email": email,
                "claude_code.token.usage": count,
                "type": otel_type,
                "model": model,
                "OTelLib": "claude_code",
            }
            events.append({
                "timestamp": ts_ms,
                "message": json.dumps(record, separators=(',', ':')),
            })

    return events


def _ensure_log_stream(log_group: str, log_stream: str) -> None:
    """Create the log group and stream if they do not already exist."""
    try:
        logs_client.create_log_group(logGroupName=log_group)
        print(f"Created log group {log_group}")
    except logs_client.exceptions.ResourceAlreadyExistsException:
        pass
    except Exception as e:
        print(f"Warning: could not create log group {log_group}: {e}")

    try:
        logs_client.create_log_stream(logGroupName=log_group, logStreamName=log_stream)
        print(f"Created log stream {log_stream}")
    except logs_client.exceptions.ResourceAlreadyExistsException:
        pass
    except Exception as e:
        print(f"Warning: could not create log stream {log_stream}: {e}")


def _put_log_events(log_group: str, log_stream: str, events: list) -> None:
    """Write events to CloudWatch Logs, batching if needed (max 10 000 / 1 MB per call)."""
    # Sort by timestamp (required by PutLogEvents)
    events = sorted(events, key=lambda e: e["timestamp"])

    batch_size = 500  # well within the 10 000 event and 1 MB limits
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        try:
            logs_client.put_log_events(
                logGroupName=log_group,
                logStreamName=log_stream,
                logEvents=batch,
            )
        except Exception as e:
            print(f"Error putting log events (batch {i}): {e}")
