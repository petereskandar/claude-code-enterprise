# ABOUTME: Lambda function that aggregates Claude Code logs into CloudWatch Metrics
# ABOUTME: Runs every 5 minutes to pre-compute metrics for dashboard performance

import json
import boto3
import os
from datetime import datetime, timedelta, timezone
import time
from collections import defaultdict
from decimal import Decimal

# Initialize clients
logs_client = boto3.client("logs")
cloudwatch_client = boto3.client("cloudwatch")
dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")

# Configuration
NAMESPACE = "ClaudeCode"
LOG_GROUP = os.environ.get("METRICS_LOG_GROUP", "/aws/lambda/bedrock-claude-logs")
METRICS_TABLE = os.environ.get("METRICS_TABLE", "ClaudeCodeMetrics")
QUOTA_TABLE = os.environ.get("QUOTA_TABLE")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE")
ENABLE_FINEGRAINED_QUOTAS = os.environ.get("ENABLE_FINEGRAINED_QUOTAS", "false").lower() == "true"
PRICING_PARAM_NAME = os.environ.get("PRICING_PARAM_NAME")
AGGREGATION_WINDOW = 5  # minutes

# DynamoDB tables
table = dynamodb.Table(METRICS_TABLE)
quota_table = dynamodb.Table(QUOTA_TABLE) if QUOTA_TABLE else None
policies_table = dynamodb.Table(POLICIES_TABLE) if POLICIES_TABLE else None

CURSOR_SK = "CURSOR#AGGREGATOR"

# Cached pricing data (loaded once per Lambda cold start)
_PRICING_DATA = None


def _load_pricing() -> dict:
    """Load model pricing from SSM Parameter Store. Cached per cold start."""
    global _PRICING_DATA
    if _PRICING_DATA is not None:
        return _PRICING_DATA

    if not PRICING_PARAM_NAME:
        print("PRICING_PARAM_NAME not set - cost calculation disabled")
        _PRICING_DATA = {}
        return _PRICING_DATA

    try:
        resp = ssm_client.get_parameter(Name=PRICING_PARAM_NAME)
        _PRICING_DATA = json.loads(resp["Parameter"]["Value"])
        print(f"Loaded pricing for {len(_PRICING_DATA.get('models', {}))} model(s)")
    except Exception as e:
        print(f"Warning: could not load pricing from SSM: {e}")
        _PRICING_DATA = {}
    return _PRICING_DATA


def _cost_for_tokens(model: str, input_t: float, output_t: float,
                     cache_read_t: float, cache_write_t: float) -> float:
    """Calculate USD cost for a set of token counts using model pricing."""
    pricing = _load_pricing()
    if not pricing:
        return 0.0

    models = pricing.get("models", {})
    default = pricing.get("default", {})

    # Try exact match first, then partial match on model name
    rates = models.get(model)
    if not rates:
        for key in models:
            if key in model or model in key:
                rates = models[key]
                break
    if not rates:
        rates = default
    if not rates:
        return 0.0

    cost = (
        input_t * rates.get("input_per_million", 0) / 1_000_000
        + output_t * rates.get("output_per_million", 0) / 1_000_000
        + cache_read_t * rates.get("cache_read_per_million", 0) / 1_000_000
        + cache_write_t * rates.get("cache_write_per_million", 0) / 1_000_000
    )
    return cost


def _read_high_water_mark(default_end: datetime) -> datetime:
    """Read the last-processed timestamp from DynamoDB."""
    try:
        resp = table.get_item(Key={"pk": "METRICS", "sk": CURSOR_SK})
        item = resp.get("Item")
        if item and "last_processed" in item:
            ts = item["last_processed"]
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception as e:
        print(f"Warning: could not read high-water mark: {e}")
    return default_end - timedelta(minutes=10)


def _write_high_water_mark(ts: datetime) -> None:
    """Persist the high-water mark so the next run starts here."""
    try:
        table.put_item(Item={
            "pk": "METRICS",
            "sk": CURSOR_SK,
            "last_processed": ts.isoformat().replace("+00:00", "Z"),
        })
    except Exception as e:
        print(f"Warning: could not write high-water mark: {e}")


def lambda_handler(event, context):
    """
    Aggregate logs incrementally using a high-water mark stored in DynamoDB.
    """
    print(f"Starting metrics aggregation for log group: {LOG_GROUP}")

    end_time = datetime.now(timezone.utc)
    start_time = _read_high_water_mark(end_time)

    start_sec = int(start_time.timestamp())
    end_sec = int(end_time.timestamp())

    try:
        metrics_to_publish = []

        # 1. Total Tokens
        total_tokens = aggregate_total_tokens(start_sec, end_sec)
        if total_tokens is not None:
            metrics_to_publish.append({
                "MetricName": "TotalTokens",
                "Value": total_tokens,
                "Unit": "Count",
                "Timestamp": end_time,
            })

        # 2. Active Users (returns count and details with per-model breakdown)
        active_users_count, user_details = aggregate_active_users(start_sec, end_sec)
        if active_users_count is not None:
            metrics_to_publish.append({
                "MetricName": "ActiveUsers",
                "Value": active_users_count,
                "Unit": "Count",
                "Timestamp": end_time,
            })

        # 3. Lines of Code Added/Removed
        line_events, lines_added, lines_removed = aggregate_lines_of_code(start_sec, end_sec)

        # 3b. Model Rate Metrics
        model_rate_metrics = aggregate_model_rate_metrics(start_sec, end_sec)

        # Write to DynamoDB
        write_to_dynamodb(
            end_time, total_tokens, active_users_count, user_details,
            lines_added, lines_removed, line_events, model_rate_metrics,
        )

        # Update quota tracking
        if quota_table:
            update_quota_table(end_time, user_details)
        else:
            print("Quota monitoring not enabled - skipping quota table updates")

        metrics_to_publish.append({
            "MetricName": "LinesAdded",
            "Value": lines_added,
            "Unit": "Count",
            "Timestamp": end_time,
        })
        metrics_to_publish.append({
            "MetricName": "LinesRemoved",
            "Value": lines_removed,
            "Unit": "Count",
            "Timestamp": end_time,
        })

        # 4. Cache Metrics
        cache_metrics = aggregate_cache_metrics(start_sec, end_sec)
        for metric in cache_metrics:
            metrics_to_publish.append(metric)

        # 5. Top Users
        top_user_metrics = aggregate_top_users(start_sec, end_sec)
        for metric in top_user_metrics:
            metrics_to_publish.append(metric)

        # 6. Operations by Type
        operation_metrics = aggregate_operations(start_sec, end_sec)
        for metric in operation_metrics:
            metrics_to_publish.append(metric)

        # 7. Code Generation by Language
        language_metrics = aggregate_code_languages(start_sec, end_sec)
        for metric in language_metrics:
            metrics_to_publish.append(metric)

        # 8. Commits
        commit_count = aggregate_commits(start_sec, end_sec)
        if commit_count is not None:
            metrics_to_publish.append({
                "MetricName": "Commits",
                "Value": commit_count,
                "Unit": "Count",
                "Timestamp": end_time,
            })

        # Publish metrics in batches (max 20 per request)
        for i in range(0, len(metrics_to_publish), 20):
            batch = metrics_to_publish[i:i + 20]
            cloudwatch_client.put_metric_data(Namespace=NAMESPACE, MetricData=batch)
            print(f"Published {len(batch)} metrics to CloudWatch")

        print(f"Successfully aggregated and published {len(metrics_to_publish)} metrics")

        _write_high_water_mark(end_time)

        return {
            "statusCode": 200,
            "body": json.dumps(f"Published {len(metrics_to_publish)} metrics"),
        }

    except Exception as e:
        print(f"Error during aggregation: {str(e)}")
        return {"statusCode": 500, "body": json.dumps(f"Error: {str(e)}")}


def run_query(query, start_sec, end_sec):
    """Run a CloudWatch Logs Insights query and wait for results."""
    try:
        print(f"run_query: startTime={start_sec} endTime={end_sec} ({datetime.fromtimestamp(start_sec, tz=timezone.utc).isoformat()} -> {datetime.fromtimestamp(end_sec, tz=timezone.utc).isoformat()})")
        response = logs_client.start_query(
            logGroupName=LOG_GROUP,
            startTime=start_sec,
            endTime=end_sec,
            queryString=query,
        )

        query_id = response["queryId"]

        for _ in range(30):
            response = logs_client.get_query_results(queryId=query_id)
            status = response["status"]

            if status == "Complete":
                results = response.get("results", [])
                print(f"run_query: complete, {len(results)} rows returned")
                return results
            elif status in ["Failed", "Cancelled"]:
                print(f"Query failed with status: {status}")
                return []

            time.sleep(1)

        print("Query timed out")
        return []

    except Exception as e:
        print(f"Error running query: {str(e)}")
        return []


def aggregate_total_tokens(start_ms, end_ms):
    """Aggregate total token usage."""
    query = """
    fields @message
    | filter @message like /claude_code.token.usage/
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | stats sum(tokens) as total_tokens
    """

    results = run_query(query, start_ms, end_ms)
    if results and len(results) > 0:
        for field in results[0]:
            if field["field"] == "total_tokens":
                return float(field["value"])
    return 0


def aggregate_active_users(start_ms, end_ms):
    """
    Count distinct active users and return user details with per-model,
    per-token-type breakdown including cache_read and cache_write separately.
    Also extracts user.group from EMF records.
    """
    # Unique count for CloudWatch metric
    query_count = """
    fields @message
    | filter @message like /user.email/
    | parse @message /"user.email":"(?<user>[^"]*)"/
    | stats count_distinct(user) as active_users
    """

    unique_count = 0
    results = run_query(query_count, start_ms, end_ms)
    if results and len(results) > 0:
        for field in results[0]:
            if field["field"] == "active_users":
                unique_count = int(float(field["value"]))

    # Per-user, per-model, per-token-type breakdown
    # Split into 4 queries by token type to stay well under the 10k row limit
    # (supports up to ~2500 user*model combinations per type = ~830 users * 3 models)
    token_types_queries = {
        "input": """
    fields @message
    | filter @message like /user.email/ and @message like /"type":"input"/
    | parse @message /"user.email":"(?<user>[^"]*)"/
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | parse @message /"model":"(?<model>[^"]*)"/
    | stats sum(tokens) as total_tokens, count() as requests by user, model
    """,
        "output": """
    fields @message
    | filter @message like /user.email/ and @message like /"type":"output"/
    | parse @message /"user.email":"(?<user>[^"]*)"/
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | parse @message /"model":"(?<model>[^"]*)"/
    | stats sum(tokens) as total_tokens, count() as requests by user, model
    """,
        "cacheRead": """
    fields @message
    | filter @message like /user.email/ and @message like /"type":"cacheRead"/
    | parse @message /"user.email":"(?<user>[^"]*)"/
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | parse @message /"model":"(?<model>[^"]*)"/
    | stats sum(tokens) as total_tokens, count() as requests by user, model
    """,
        "cacheCreation": """
    fields @message
    | filter @message like /user.email/ and @message like /"type":"cacheCreation"/
    | parse @message /"user.email":"(?<user>[^"]*)"/
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | parse @message /"model":"(?<model>[^"]*)"/
    | stats sum(tokens) as total_tokens, count() as requests by user, model
    """,
    }

    # Structure: { email: { "tokens": N, "requests": N, "models": { model: { input, output, cache_read, cache_write } } } }
    user_data = {}

    for token_type, query in token_types_queries.items():
        results = run_query(query, start_ms, end_ms)
        for result in results:
            user_email = None
            tokens = 0
            requests = 0
            model = None

            for field in result:
                if field["field"] == "user":
                    user_email = field["value"]
                elif field["field"] == "total_tokens":
                    tokens = float(field["value"])
                elif field["field"] == "requests":
                    requests = int(float(field["value"]))
                elif field["field"] == "model":
                    model = field["value"]

            if not user_email or tokens <= 0:
                continue

            if user_email not in user_data:
                user_data[user_email] = {
                    "email": user_email,
                    "tokens": 0,
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "models": {},
                }

            ud = user_data[user_email]
            ud["tokens"] += tokens

            if token_type == "input":
                ud["input_tokens"] += tokens
                ud["requests"] += requests
            elif token_type == "output":
                ud["output_tokens"] += tokens
            elif token_type == "cacheRead":
                ud["cache_read_tokens"] += tokens
            elif token_type == "cacheCreation":
                ud["cache_write_tokens"] += tokens

            # Per-model breakdown
            if model:
                if model not in ud["models"]:
                    ud["models"][model] = {
                        "input": 0, "output": 0,
                        "cache_read": 0, "cache_write": 0,
                    }
                md = ud["models"][model]
                if token_type == "input":
                    md["input"] += tokens
                elif token_type == "output":
                    md["output"] += tokens
                elif token_type == "cacheRead":
                    md["cache_read"] += tokens
                elif token_type == "cacheCreation":
                    md["cache_write"] += tokens

    # Calculate cost per user using per-model pricing
    for email, ud in user_data.items():
        total_cost = 0.0
        for model, md in ud["models"].items():
            total_cost += _cost_for_tokens(
                model, md["input"], md["output"],
                md["cache_read"], md["cache_write"],
            )
        ud["cost"] = total_cost

    # Query for user.group from EMF logs
    query_groups = """
    fields @message
    | filter @message like /user.email/ and @message like /user.group/
    | parse @message /"user.email":"(?<uemail>[^"]*)"/
    | parse @message /"user.group":"(?<ugrp>[^"]*)"/
    | stats latest(ugrp) as latest_grp by uemail
    """

    results = run_query(query_groups, start_ms, end_ms)
    for result in results:
        user_email = None
        user_group = None
        for field in result:
            if field["field"] == "uemail":
                user_email = field["value"]
            elif field["field"] == "latest_grp":
                user_group = field["value"]
        if user_email and user_group and user_email in user_data:
            user_data[user_email]["groups"] = [user_group]

    # Convert to list sorted by tokens
    user_details = sorted(user_data.values(), key=lambda x: x["tokens"], reverse=True)

    return unique_count, user_details


def aggregate_cache_metrics(start_ms, end_ms):
    """Aggregate cache hit/miss metrics and token type metrics."""
    metrics = []
    timestamp = datetime.now(timezone.utc)

    query = """
    fields @message
    | filter @message like /claude_code.token.usage/
    | parse @message /"type":"(?<token_type>[^"]*)"/
    | filter token_type in ["input", "output", "cacheRead", "cacheCreation"]
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | stats sum(tokens) as total by token_type
    """

    results = run_query(query, start_ms, end_ms)

    for result in results:
        token_type = None
        total = 0
        for field in result:
            if field["field"] == "token_type":
                token_type = field["value"]
            elif field["field"] == "total":
                total = float(field["value"])

        if token_type and total > 0:
            if token_type == "input":
                metrics.append({"MetricName": "InputTokens", "Value": total, "Unit": "Count", "Timestamp": timestamp})
            elif token_type == "output":
                metrics.append({"MetricName": "OutputTokens", "Value": total, "Unit": "Count", "Timestamp": timestamp})
            elif token_type == "cacheRead":
                metrics.append({"MetricName": "CacheReadTokens", "Value": total, "Unit": "Count", "Timestamp": timestamp})
            elif token_type == "cacheCreation":
                metrics.append({"MetricName": "CacheCreationTokens", "Value": total, "Unit": "Count", "Timestamp": timestamp})

    cache_read_tokens = 0
    cache_creation_tokens = 0
    for metric in metrics:
        if metric["MetricName"] == "CacheReadTokens":
            cache_read_tokens = metric["Value"]
        elif metric["MetricName"] == "CacheCreationTokens":
            cache_creation_tokens = metric["Value"]

    total_cache = cache_read_tokens + cache_creation_tokens
    if total_cache > 0:
        efficiency = (cache_read_tokens / total_cache) * 100
        metrics.append({"MetricName": "CacheEfficiency", "Value": efficiency, "Unit": "Percent", "Timestamp": timestamp})

    return metrics


def aggregate_top_users(start_ms, end_ms):
    """Aggregate top 10 users by token usage."""
    metrics = []
    timestamp = datetime.now(timezone.utc)

    query = """
    fields @message
    | filter @message like /user.email/
    | parse @message /"user.email":"(?<user>[^"]*)"/
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | stats sum(tokens) as total_tokens by user
    | sort total_tokens desc
    | limit 10
    """

    results = run_query(query, start_ms, end_ms)

    for rank, result in enumerate(results, 1):
        user = None
        tokens = 0
        for field in result:
            if field["field"] == "user":
                user = field["value"]
            elif field["field"] == "total_tokens":
                tokens = float(field["value"])

        if user and tokens > 0:
            metrics.append({
                "MetricName": "TopUserTokens",
                "Dimensions": [
                    {"Name": "Rank", "Value": str(rank)},
                    {"Name": "User", "Value": user},
                ],
                "Value": tokens,
                "Unit": "Count",
                "Timestamp": timestamp,
            })

    return metrics


def aggregate_operations(start_ms, end_ms):
    """Aggregate operations by type."""
    metrics = []
    timestamp = datetime.now(timezone.utc)

    query = """
    fields @message
    | filter @message like /tool_name/
    | parse @message /"tool_name":"(?<tool>[^"]*)"/
    | stats count() as usage by tool
    """

    results = run_query(query, start_ms, end_ms)

    for result in results:
        tool = None
        usage = 0
        for field in result:
            if field["field"] == "tool":
                tool = field["value"]
            elif field["field"] == "usage":
                usage = float(field["value"])

        if tool and usage > 0:
            metrics.append({
                "MetricName": "OperationCount",
                "Dimensions": [{"Name": "OperationType", "Value": tool}],
                "Value": usage,
                "Unit": "Count",
                "Timestamp": timestamp,
            })

    return metrics


def aggregate_code_languages(start_ms, end_ms):
    """Aggregate code generation by language."""
    metrics = []
    timestamp = datetime.now(timezone.utc)

    query = """
    fields @message
    | filter @message like /code_edit_tool.decision/
    | parse @message /"language":"(?<lang>[^"]*)"/
    | stats count() as edits by lang
    """

    results = run_query(query, start_ms, end_ms)

    for result in results:
        lang = None
        edits = 0
        for field in result:
            if field["field"] == "lang":
                lang = field["value"]
            elif field["field"] == "edits":
                edits = float(field["value"])

        if lang and edits > 0:
            metrics.append({
                "MetricName": "CodeEditsByLanguage",
                "Dimensions": [{"Name": "Language", "Value": lang}],
                "Value": edits,
                "Unit": "Count",
                "Timestamp": timestamp,
            })

    return metrics


def aggregate_commits(start_ms, end_ms):
    """Aggregate commit count."""
    query = """
    fields @message
    | filter @message like /claude_code.commit.count/
    | stats count() as total_commits
    """

    results = run_query(query, start_ms, end_ms)
    if results and len(results) > 0:
        for field in results[0]:
            if field["field"] == "total_commits":
                return int(float(field["value"]))
    return 0


def aggregate_lines_of_code(start_ms, end_ms):
    """Get individual line change events."""
    query = """
    fields @timestamp, @message
    | filter @message like /claude_code.lines_of_code.count/
    | parse @message /"type":"(?<type>[^"]*)"/
    | parse @message /"claude_code.lines_of_code.count":(?<lines>[0-9.]+)/
    | sort @timestamp asc
    """

    events = []
    lines_added_total = 0
    lines_removed_total = 0

    results = run_query(query, start_ms, end_ms)
    for result in results:
        timestamp = None
        line_type = None
        lines = 0

        for field in result:
            if field["field"] == "@timestamp":
                timestamp = field["value"]
            elif field["field"] == "type":
                line_type = field["value"].lower()
            elif field["field"] == "lines":
                lines = float(field["value"])

        if timestamp and line_type and lines >= 0:
            events.append({"timestamp": timestamp, "type": line_type, "count": lines})

            if line_type == "added":
                lines_added_total += lines
            elif line_type == "removed":
                lines_removed_total += lines

    return events, lines_added_total, lines_removed_total


def aggregate_model_rate_metrics(start_ms, end_ms):
    """Query logs and bucket token/request counts by model and minute."""
    query = """
    fields @timestamp, @message
    | filter @message like /claude_code.token.usage/
    | parse @message /"model":"(?<model>[^"]*)"/
    | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
    | parse @message /"type":"(?<token_type>[^"]*)"/
    | sort @timestamp asc
    """

    model_metrics = defaultdict(lambda: defaultdict(lambda: {"tokens": 0, "requests": 0}))

    results = run_query(query, start_ms, end_ms)
    for result in results:
        timestamp = None
        model = None
        tokens = 0
        token_type = None

        for field in result:
            if field["field"] == "@timestamp":
                timestamp = field["value"]
            elif field["field"] == "model":
                model = field["value"]
            elif field["field"] == "tokens":
                tokens = float(field["value"])
            elif field["field"] == "token_type":
                token_type = field["value"]

        if timestamp and model and tokens > 0:
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                minute_dt = dt.replace(second=0, microsecond=0)
                minute_str = minute_dt.strftime("%H:%M:%S")

                model_metrics[model][minute_str]["tokens"] += tokens

                if token_type == "input":
                    model_metrics[model][minute_str]["requests"] += 1
            except Exception as e:
                print(f"Error parsing timestamp {timestamp}: {str(e)}")

    return model_metrics


def write_to_dynamodb(
    timestamp, total_tokens, unique_users, user_details,
    lines_added, lines_removed, line_events=None, model_rate_metrics=None,
):
    """Write aggregated metrics to DynamoDB."""
    try:
        iso_timestamp = timestamp.isoformat().replace("+00:00", "Z")
        ttl = int((timestamp + timedelta(days=30)).timestamp())

        top_users_decimal = []
        for user in user_details[:10] if user_details else []:
            top_users_decimal.append({
                "email": user["email"],
                "tokens": Decimal(str(user.get("tokens", 0))),
                "requests": Decimal(str(user.get("requests", 0))),
            })

        with table.batch_writer() as batch:
            # 1. Window aggregate
            window_item = {
                "pk": "METRICS",
                "sk": f"{iso_timestamp}#WINDOW#SUMMARY",
                "unique_users": unique_users,
                "total_tokens": Decimal(str(total_tokens)) if total_tokens else Decimal(0),
                "top_users": top_users_decimal,
                "lines_added": Decimal(str(lines_added)) if lines_added else Decimal(0),
                "lines_removed": Decimal(str(lines_removed)) if lines_removed else Decimal(0),
                "timestamp": iso_timestamp,
                "ttl": ttl,
            }
            batch.put_item(Item=window_item)

            # 2. Lines of code
            if lines_added > 0 or lines_removed > 0:
                lines_item = {
                    "pk": "METRICS",
                    "sk": f"{iso_timestamp}#LINES#SUMMARY",
                    "lines_added": Decimal(str(lines_added)),
                    "lines_removed": Decimal(str(lines_removed)),
                    "timestamp": iso_timestamp,
                    "ttl": ttl,
                }
                batch.put_item(Item=lines_item)

            if line_events:
                for event in line_events:
                    event_dt = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                    event_iso = event_dt.isoformat() + "Z"
                    event_id = f"{event['type'].upper()}#{event_dt.timestamp()}"

                    line_event_item = {
                        "pk": "METRICS",
                        "sk": f"{event_iso}#LINES#EVENT#{event_id}",
                        "type": event["type"],
                        "count": Decimal(str(event["count"])),
                        "timestamp": event_iso,
                        "ttl": ttl,
                    }
                    batch.put_item(Item=line_event_item)

            # 3. User metrics
            for user in user_details:
                user_item = {
                    "pk": "METRICS",
                    "sk": f'{iso_timestamp}#USER#{user["email"]}',
                    "tokens": Decimal(str(user.get("tokens", 0))),
                    "requests": Decimal(str(user.get("requests", 0))),
                    "email": user["email"],
                    "timestamp": iso_timestamp,
                    "ttl": ttl,
                }
                batch.put_item(Item=user_item)

            # 4. Model rate metrics
            if model_rate_metrics:
                for model_id, minute_data in model_rate_metrics.items():
                    for minute_time, metrics in minute_data.items():
                        minute_dt = datetime.combine(
                            timestamp.date(),
                            datetime.strptime(minute_time, "%H:%M:%S").time(),
                            tzinfo=timezone.utc,
                        )
                        minute_iso = minute_dt.isoformat().replace("+00:00", "Z")

                        model_rate_item = {
                            "pk": "METRICS",
                            "sk": f"{minute_iso}#MODEL_RATE#{model_id}",
                            "model": model_id,
                            "tpm": Decimal(str(metrics["tokens"])),
                            "rpm": Decimal(str(metrics["requests"])),
                            "timestamp": minute_iso,
                            "ttl": ttl,
                        }
                        batch.put_item(Item=model_rate_item)

        line_events_count = len(line_events) if line_events else 0
        model_rate_count = (
            sum(len(minutes) for minutes in model_rate_metrics.values())
            if model_rate_metrics else 0
        )
        print(f"Wrote window summary, {line_events_count} line events, {model_rate_count} model rate metrics, and {len(user_details)} user records to DynamoDB")

    except Exception as e:
        print(f"Error writing to DynamoDB: {str(e)}")


def update_quota_table(timestamp, user_details):
    """
    Update monthly user quota tracking table with per-model token breakdown and cost.
    Schema: PK=USER#{email}, SK=MONTH#{YYYY-MM}
    Fields: input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            total_tokens, daily_tokens, daily_cost, total_cost, models (map), groups
    """
    if not user_details:
        return

    try:
        current_month = timestamp.strftime("%Y-%m")
        current_date = timestamp.strftime("%Y-%m-%d")
        ttl = int(
            (timestamp.replace(day=28) + timedelta(days=32)).replace(day=1).timestamp()
        )

        for user in user_details:
            user_email = user["email"]
            tokens_to_add = float(user.get("tokens", 0))
            input_tokens = float(user.get("input_tokens", 0))
            output_tokens = float(user.get("output_tokens", 0))
            cache_read_tokens = float(user.get("cache_read_tokens", 0))
            cache_write_tokens = float(user.get("cache_write_tokens", 0))
            cost_increment = float(user.get("cost", 0))
            groups = user.get("groups", [])
            models_data = user.get("models", {})

            if tokens_to_add <= 0:
                continue

            pk = f"USER#{user_email}"
            sk = f"MONTH#{current_month}"

            try:
                response = quota_table.get_item(Key={"pk": pk, "sk": sk})
                existing = response.get("Item", {})
                existing_daily_date = existing.get("daily_date")

                daily_reset = existing_daily_date != current_date

                # Build the models map update: merge per-model token counts
                existing_models = existing.get("models", {})
                for model, md in models_data.items():
                    if model not in existing_models:
                        existing_models[model] = {
                            "input": Decimal("0"), "output": Decimal("0"),
                            "cache_read": Decimal("0"), "cache_write": Decimal("0"),
                            "cost": Decimal("0"),
                        }
                    em = existing_models[model]
                    em["input"] = Decimal(str(float(em.get("input", 0)) + md["input"]))
                    em["output"] = Decimal(str(float(em.get("output", 0)) + md["output"]))
                    em["cache_read"] = Decimal(str(float(em.get("cache_read", 0)) + md["cache_read"]))
                    em["cache_write"] = Decimal(str(float(em.get("cache_write", 0)) + md["cache_write"]))
                    model_cost = _cost_for_tokens(model, md["input"], md["output"], md["cache_read"], md["cache_write"])
                    em["cost"] = Decimal(str(float(em.get("cost", 0)) + model_cost))

                # Build update expression
                update_expr = """
                    ADD total_tokens :tokens,
                        input_tokens :input_tokens,
                        output_tokens :output_tokens,
                        cache_read_tokens :cache_read_tokens,
                        cache_write_tokens :cache_write_tokens,
                        total_cost :cost_inc
                    SET last_updated = :updated,
                        #ttl = :ttl,
                        email = :email,
                        daily_date = :daily_date,
                        models = :models
                """

                expr_attr_values = {
                    ":tokens": Decimal(str(tokens_to_add)),
                    ":input_tokens": Decimal(str(input_tokens)),
                    ":output_tokens": Decimal(str(output_tokens)),
                    ":cache_read_tokens": Decimal(str(cache_read_tokens)),
                    ":cache_write_tokens": Decimal(str(cache_write_tokens)),
                    ":cost_inc": Decimal(str(cost_increment)),
                    ":updated": timestamp.isoformat().replace("+00:00", "Z"),
                    ":ttl": ttl,
                    ":email": user_email,
                    ":daily_date": current_date,
                    ":models": existing_models,
                }

                expr_attr_names = {"#ttl": "ttl"}

                if daily_reset:
                    update_expr += ", daily_tokens = :tokens, daily_cost = :cost_inc"
                else:
                    update_expr = update_expr.replace(
                        "ADD total_tokens :tokens",
                        "ADD total_tokens :tokens, daily_tokens :tokens, daily_cost :cost_inc"
                    )

                if groups:
                    update_expr += ", #groups = :groups"
                    expr_attr_values[":groups"] = groups
                    expr_attr_names["#groups"] = "groups"

                quota_table.update_item(
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=expr_attr_names,
                    ExpressionAttributeValues=expr_attr_values,
                )

                daily_note = " (daily reset)" if daily_reset else ""
                print(f"Updated quota for {user_email}: +{tokens_to_add:,.0f} tokens, +${cost_increment:.4f} for {current_month}{daily_note}")

            except Exception as e:
                print(f"Error updating quota for {user_email}: {str(e)}")

    except Exception as e:
        print(f"Error in update_quota_table: {str(e)}")
