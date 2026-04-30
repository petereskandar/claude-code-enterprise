# ABOUTME: Lambda function that enforces quotas by tagging Bedrock Application Inference Profiles
# ABOUTME: Runs every 5 minutes and sets status=enabled/disabled on each user's inference profile
# ABOUTME: based on monthly cost (USD) from UserQuotaMetrics.total_cost field.
# ABOUTME: Policy precedence: user-specific > group > default. Personal policy always wins.
# ABOUTME: Optimized for 1500+ profiles: ThreadPoolExecutor for parallel Bedrock API calls,
# ABOUTME: skip-unchanged to avoid redundant tag_resource calls.

import json
import boto3
import os
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Clients
bedrock_client = boto3.client("bedrock")
dynamodb = boto3.resource("dynamodb")

# Configuration
QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE", "QuotaPolicies")
MAX_WORKERS = 10  # parallel Bedrock API calls

quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE)


def lambda_handler(event, context):
    print("Starting cost-based quota enforcement via inference profile tagging")

    # 1. Load ALL policies in one scan (typically < 100 items)
    all_policies = _load_all_policies()
    default_policy = all_policies.get("default:default")

    # 2. Build email -> [{arn, current_status}, ...] map from APPLICATION inference profiles
    email_to_profiles = _build_email_to_profiles_map()
    if not email_to_profiles:
        print("No application inference profiles with user.email tags found")
        return {"statusCode": 200, "body": json.dumps({"enabled": 0, "disabled": 0})}

    total_profiles = sum(len(profiles) for profiles in email_to_profiles.values())
    print(f"Found {total_profiles} profiles for {len(email_to_profiles)} user(s)")

    # 3. Batch-read current month's usage for all users
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage_map = _batch_get_usage(list(email_to_profiles.keys()), current_month, current_date)

    # 4. For each user: resolve policy, check cost, determine desired status
    tag_operations = []  # list of (arn, desired_status, current_status)
    enabled_count = 0
    disabled_count = 0

    for email, profiles in email_to_profiles.items():
        groups = usage_map.get(email, {}).get("groups", [])
        policy = _resolve_policy(email, groups, all_policies, default_policy)

        if policy is None or policy.get("enforcement_mode", "alert") != "block":
            desired = "enabled"
        else:
            usage = usage_map.get(email, {})
            should_block, reason = _check_cost_quota(usage, policy)
            desired = "disabled" if should_block else "enabled"
            if should_block:
                print(f"  BLOCKED {email}: {reason}")

        for prof in profiles:
            current = prof["current_status"]
            if current != desired:
                tag_operations.append((prof["arn"], desired))
            if desired == "enabled":
                enabled_count += 1
            else:
                disabled_count += 1

    # 5. Apply tag changes in parallel (only changed profiles)
    skipped = total_profiles - len(tag_operations)
    print(f"Tagging: {len(tag_operations)} changes needed, {skipped} already correct")

    if tag_operations:
        _parallel_tag(tag_operations)

    print(f"Done: {enabled_count} profile(s) enabled, {disabled_count} profile(s) disabled")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "enabled": enabled_count,
            "disabled": disabled_count,
            "tag_changes": len(tag_operations),
            "skipped": skipped,
        }),
    }


def _load_all_policies() -> dict:
    """Load all CURRENT policies from DynamoDB into a dict keyed by 'type:identifier'."""
    policies = {}
    try:
        response = policies_table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr("sk").eq("CURRENT")
        )
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = policies_table.scan(
                FilterExpression=boto3.dynamodb.conditions.Attr("sk").eq("CURRENT"),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        for item in items:
            pt = item.get("policy_type", "")
            ident = item.get("identifier", "")
            if pt and ident:
                key = f"{pt}:{ident}"
                policies[key] = {
                    "monthly_cost_limit": float(item.get("monthly_cost_limit", 0)),
                    "daily_cost_limit": float(item.get("daily_cost_limit", 0)) if item.get("daily_cost_limit") else None,
                    "monthly_token_limit": int(item.get("monthly_token_limit", 0)),
                    "daily_token_limit": int(item.get("daily_token_limit", 0)) if item.get("daily_token_limit") else None,
                    "enforcement_mode": item.get("enforcement_mode", "alert"),
                    "enabled": item.get("enabled", True),
                    "policy_type": pt,
                    "identifier": ident,
                }

        print(f"Loaded {len(policies)} policies")
    except Exception as e:
        print(f"Error loading policies: {e}")
    return policies


def _resolve_policy(email: str, groups: list, all_policies: dict, default_policy) -> dict | None:
    """
    Resolve effective policy. Precedence: user > group > default.
    Personal policy ALWAYS wins over group policy.
    """
    # 1. User-specific
    user_key = f"user:{email}"
    if user_key in all_policies:
        p = all_policies[user_key]
        if p.get("enabled", True):
            return p

    # 2. Group (most restrictive cost limit wins)
    if groups:
        group_policies = []
        for g in groups:
            gk = f"group:{g}"
            if gk in all_policies:
                p = all_policies[gk]
                if p.get("enabled", True):
                    group_policies.append(p)
        if group_policies:
            return min(group_policies, key=lambda p: p.get("monthly_cost_limit", float("inf")) or float("inf"))

    # 3. Default
    if default_policy and default_policy.get("enabled", True):
        return default_policy

    return None


def _get_tags_for_profile(arn: str) -> dict:
    """Get tags for a single profile. Returns {arn, email, current_status}."""
    try:
        tags = bedrock_client.list_tags_for_resource(resourceARN=arn).get("tags", [])
        email = None
        status = "enabled"  # default if no status tag
        for t in tags:
            if t.get("key") == "user.email":
                email = t["value"]
            elif t.get("key") == "status":
                status = t["value"]
        return {"arn": arn, "email": email, "current_status": status}
    except Exception as e:
        print(f"Warning: could not get tags for {arn}: {e}")
        return {"arn": arn, "email": None, "current_status": "unknown"}


def _build_email_to_profiles_map() -> dict:
    """
    Return { email: [{arn, current_status}, ...] } for all APPLICATION inference profiles.
    Uses ThreadPoolExecutor to parallelize list_tags_for_resource calls.
    """
    # First collect all ARNs
    all_arns = []
    try:
        paginator = bedrock_client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="APPLICATION"):
            for summary in page.get("inferenceProfileSummaries", []):
                arn = summary.get("inferenceProfileArn")
                if arn:
                    all_arns.append(arn)
    except Exception as e:
        print(f"Error listing inference profiles: {e}")
        return {}

    print(f"Listed {len(all_arns)} APPLICATION inference profiles, fetching tags in parallel...")

    # Parallel tag fetch
    result = defaultdict(list)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_get_tags_for_profile, arn): arn for arn in all_arns}
        for future in as_completed(futures):
            profile = future.result()
            if profile["email"]:
                result[profile["email"]].append({
                    "arn": profile["arn"],
                    "current_status": profile["current_status"],
                })

    return dict(result)


def _batch_get_usage(emails: list, current_month: str, current_date: str) -> dict:
    """
    Fetch current month's usage for all emails using batch reads.
    DynamoDB batch_get_item supports 100 items per call.
    Returns { email: { total_cost, daily_cost, total_tokens, daily_tokens, groups } }
    """
    usage = {}
    keys = [{"pk": f"USER#{email}", "sk": f"MONTH#{current_month}"} for email in emails]

    table_name = quota_table.table_name
    for batch_start in range(0, len(keys), 100):
        batch_keys = keys[batch_start:batch_start + 100]
        try:
            response = dynamodb.meta.client.batch_get_item(
                RequestItems={table_name: {"Keys": batch_keys}}
            )
            for item in response.get("Responses", {}).get(table_name, []):
                email = item.get("email", {})
                if isinstance(email, dict):
                    email = email.get("S", "")
                else:
                    email = str(email)

                daily_cost = float(item.get("daily_cost", 0))
                daily_tokens = float(item.get("daily_tokens", 0))
                if item.get("daily_date") != current_date:
                    daily_cost = 0.0
                    daily_tokens = 0.0

                usage[email] = {
                    "total_cost": float(item.get("total_cost", 0)),
                    "daily_cost": daily_cost,
                    "total_tokens": float(item.get("total_tokens", 0)),
                    "daily_tokens": daily_tokens,
                    "groups": item.get("groups", []),
                }

            # Handle unprocessed keys
            unprocessed = response.get("UnprocessedKeys", {}).get(table_name, {}).get("Keys", [])
            for key in unprocessed:
                print(f"Warning: unprocessed key {key}")

        except Exception as e:
            print(f"Error in batch_get_usage (batch {batch_start}): {e}")

    return usage


def _check_cost_quota(usage: dict, policy: dict) -> tuple:
    """
    Check if user is over cost quota. Returns (should_block, reason).
    Cost-based limits take priority; falls back to token-based if no cost limit set.
    """
    monthly_cost_limit = policy.get("monthly_cost_limit", 0)
    daily_cost_limit = policy.get("daily_cost_limit")
    total_cost = usage.get("total_cost", 0)
    daily_cost = usage.get("daily_cost", 0)

    # Cost-based enforcement (primary)
    if monthly_cost_limit and monthly_cost_limit > 0:
        if total_cost >= monthly_cost_limit:
            pct = total_cost / monthly_cost_limit * 100
            return True, f"monthly cost ${total_cost:.2f}/${monthly_cost_limit:.2f} ({pct:.0f}%)"

        if daily_cost_limit and daily_cost_limit > 0 and daily_cost >= daily_cost_limit:
            pct = daily_cost / daily_cost_limit * 100
            return True, f"daily cost ${daily_cost:.2f}/${daily_cost_limit:.2f} ({pct:.0f}%)"

        return False, f"within cost quota ${total_cost:.2f}/${monthly_cost_limit:.2f}"

    # Fallback: token-based enforcement (backward compat)
    monthly_token_limit = policy.get("monthly_token_limit", 0)
    daily_token_limit = policy.get("daily_token_limit")
    total_tokens = usage.get("total_tokens", 0)
    daily_tokens = usage.get("daily_tokens", 0)

    if monthly_token_limit and monthly_token_limit > 0:
        if total_tokens >= monthly_token_limit:
            pct = total_tokens / monthly_token_limit * 100
            return True, f"monthly tokens {int(total_tokens):,}/{monthly_token_limit:,} ({pct:.0f}%)"

        if daily_token_limit and daily_token_limit > 0 and daily_tokens >= daily_token_limit:
            pct = daily_tokens / daily_token_limit * 100
            return True, f"daily tokens {int(daily_tokens):,}/{daily_token_limit:,} ({pct:.0f}%)"

        return False, f"within token quota {int(total_tokens):,}/{monthly_token_limit:,}"

    return False, "no limit configured"


def _tag_single_profile(arn: str, status: str) -> bool:
    """Tag a single profile. Returns True on success."""
    try:
        bedrock_client.tag_resource(
            resourceARN=arn,
            tags=[{"key": "status", "value": status}],
        )
        return True
    except Exception as e:
        print(f"Error tagging {arn} with status={status}: {e}")
        return False


def _parallel_tag(operations: list) -> None:
    """Apply tag_resource calls in parallel using ThreadPoolExecutor."""
    success = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_tag_single_profile, arn, status): (arn, status)
            for arn, status in operations
        }
        for future in as_completed(futures):
            if future.result():
                success += 1
            else:
                failed += 1
    print(f"  Tag results: {success} succeeded, {failed} failed")
