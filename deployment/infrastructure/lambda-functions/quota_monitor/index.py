# ABOUTME: Lambda function that monitors user cost/token quotas and sends SNS alerts
# ABOUTME: Supports cost-based (primary) and token-based (fallback) quotas
# ABOUTME: Policy precedence: user > group > default. Personal always wins.

import json
import boto3
import os
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key, Attr

# Initialize clients
dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")

# Configuration
QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
ENABLE_FINEGRAINED_QUOTAS = os.environ.get("ENABLE_FINEGRAINED_QUOTAS", "false").lower() == "true"

# Default limits (fallback when no policy defined)
MONTHLY_TOKEN_LIMIT = int(os.environ.get("MONTHLY_TOKEN_LIMIT", "300000000"))
WARNING_THRESHOLD_80 = int(os.environ.get("WARNING_THRESHOLD_80", "240000000"))
WARNING_THRESHOLD_90 = int(os.environ.get("WARNING_THRESHOLD_90", "270000000"))

# DynamoDB tables
quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE) if POLICIES_TABLE else None


def lambda_handler(event, context):
    """Check user cost/token usage against quotas and send alerts."""
    print(f"Starting quota monitoring check at {datetime.now(timezone.utc).isoformat()}")
    print(f"Fine-grained quotas: {'enabled' if ENABLE_FINEGRAINED_QUOTAS else 'disabled'}")

    now = datetime.now(timezone.utc)
    month_name = now.strftime("%B %Y")
    current_date = now.strftime("%Y-%m-%d")
    days_in_month = (
        31 if now.month in [1, 3, 5, 7, 8, 10, 12]
        else (30 if now.month != 2 else (29 if now.year % 4 == 0 else 28))
    )
    days_remaining = days_in_month - now.day

    print(f"Checking usage for {month_name} (day {now.day}/{days_in_month})")

    try:
        user_usage_data = get_monthly_usage()

        if not user_usage_data:
            print("No user metrics found for current month")
            return {"statusCode": 200, "body": json.dumps("No usage data found")}

        policies_cache = {}
        if ENABLE_FINEGRAINED_QUOTAS and policies_table:
            policies_cache = load_all_policies()
            print(f"Loaded {len(policies_cache)} policies")

        sent_alerts = get_sent_alerts(month_name)

        alerts_to_send = []
        stats = {"total_users": 0, "over_80": 0, "over_90": 0, "exceeded": 0, "daily_exceeded": 0}

        for email, usage in user_usage_data.items():
            stats["total_users"] += 1

            policy = resolve_user_quota(email, usage.get("groups", []), policies_cache)
            if policy is None:
                continue

            total_cost = float(usage.get("total_cost", 0))
            daily_cost = float(usage.get("daily_cost", 0))
            total_tokens = float(usage.get("total_tokens", 0))
            daily_tokens = float(usage.get("daily_tokens", 0))

            alerts = check_limits_and_generate_alerts(
                email=email,
                total_cost=total_cost,
                daily_cost=daily_cost,
                total_tokens=total_tokens,
                daily_tokens=daily_tokens,
                policy=policy,
                month_name=month_name,
                current_date=current_date,
                days_remaining=days_remaining,
                days_in_month=days_in_month,
                sent_alerts=sent_alerts,
            )

            # Stats based on cost or token limits
            monthly_cost_limit = policy.get("monthly_cost_limit", 0)
            if monthly_cost_limit and monthly_cost_limit > 0:
                pct = (total_cost / monthly_cost_limit) * 100
            else:
                monthly_token_limit = policy.get("monthly_token_limit", 0)
                pct = (total_tokens / monthly_token_limit) * 100 if monthly_token_limit > 0 else 0

            if pct > 100:
                stats["exceeded"] += 1
            elif pct > 90:
                stats["over_90"] += 1
            elif pct > 80:
                stats["over_80"] += 1

            for alert in alerts:
                alert_key = f"{email}#{alert['alert_type']}#{alert['alert_level']}"
                if alert_key not in sent_alerts:
                    alerts_to_send.append(alert)
                    record_sent_alert(month_name, email, alert["alert_type"], alert["alert_level"], alert)

        if alerts_to_send:
            send_alerts(alerts_to_send)
            print(f"Sent {len(alerts_to_send)} quota alerts")
        else:
            print("No new alerts to send")

        print(f"Summary - Total: {stats['total_users']}, Over 80%: {stats['over_80']}, Over 90%: {stats['over_90']}, Exceeded: {stats['exceeded']}")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "users_checked": stats["total_users"],
                "alerts_sent": len(alerts_to_send),
                "users_over_80": stats["over_80"],
                "users_over_90": stats["over_90"],
                "users_exceeded": stats["exceeded"],
            }),
        }

    except Exception as e:
        print(f"Error during quota monitoring: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"statusCode": 500, "body": json.dumps(f"Error: {str(e)}")}


def get_monthly_usage():
    """Query UserQuotaMetrics for all users in current month."""
    user_usage = {}
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        response = quota_table.scan(
            FilterExpression=Attr("sk").eq(f"MONTH#{month_prefix}"),
            ProjectionExpression="email, total_tokens, daily_tokens, daily_date, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_cost, daily_cost, #groups",
            ExpressionAttributeNames={"#groups": "groups"},
        )

        def _process_items(items):
            for item in items:
                email = item.get("email")
                if not email:
                    continue
                daily_tokens = float(item.get("daily_tokens", 0))
                daily_cost = float(item.get("daily_cost", 0))
                if item.get("daily_date") != current_date:
                    daily_tokens = 0
                    daily_cost = 0.0

                user_usage[email] = {
                    "total_tokens": float(item.get("total_tokens", 0)),
                    "daily_tokens": daily_tokens,
                    "daily_date": item.get("daily_date"),
                    "input_tokens": float(item.get("input_tokens", 0)),
                    "output_tokens": float(item.get("output_tokens", 0)),
                    "cache_read_tokens": float(item.get("cache_read_tokens", 0)),
                    "cache_write_tokens": float(item.get("cache_write_tokens", 0)),
                    "total_cost": float(item.get("total_cost", 0)),
                    "daily_cost": daily_cost,
                    "groups": item.get("groups", []),
                }

        _process_items(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            response = quota_table.scan(
                FilterExpression=Attr("sk").eq(f"MONTH#{month_prefix}"),
                ProjectionExpression="email, total_tokens, daily_tokens, daily_date, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_cost, daily_cost, #groups",
                ExpressionAttributeNames={"#groups": "groups"},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            _process_items(response.get("Items", []))

        print(f"Found {len(user_usage)} users with usage in {month_prefix}")

    except Exception as e:
        print(f"Error querying quota table: {str(e)}")
        raise

    return user_usage


def load_all_policies():
    policies = {}
    if not policies_table:
        return policies

    try:
        response = policies_table.scan(FilterExpression=Attr("sk").eq("CURRENT"))

        def _process(items):
            for item in items:
                pt = item.get("policy_type")
                ident = item.get("identifier")
                if pt and ident:
                    key = f"{pt}:{ident}"
                    policies[key] = {
                        "policy_type": pt,
                        "identifier": ident,
                        "monthly_cost_limit": float(item.get("monthly_cost_limit", 0)),
                        "daily_cost_limit": float(item.get("daily_cost_limit", 0)) if item.get("daily_cost_limit") else None,
                        "monthly_token_limit": int(item.get("monthly_token_limit", 0)),
                        "daily_token_limit": int(item.get("daily_token_limit", 0)) if item.get("daily_token_limit") else None,
                        "warning_threshold_80": float(item.get("warning_threshold_80", 0)),
                        "warning_threshold_90": float(item.get("warning_threshold_90", 0)),
                        "enforcement_mode": item.get("enforcement_mode", "alert"),
                        "enabled": item.get("enabled", True),
                    }

        _process(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            response = policies_table.scan(
                FilterExpression=Attr("sk").eq("CURRENT"),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            _process(response.get("Items", []))

    except Exception as e:
        print(f"Error loading policies: {str(e)}")

    return policies


def resolve_user_quota(email, groups, policies_cache):
    """Resolve effective policy. Precedence: user > group > default."""
    if not ENABLE_FINEGRAINED_QUOTAS:
        return {
            "policy_type": "default",
            "identifier": "environment",
            "monthly_cost_limit": 0,
            "monthly_token_limit": MONTHLY_TOKEN_LIMIT,
            "daily_token_limit": None,
            "warning_threshold_80": WARNING_THRESHOLD_80,
            "warning_threshold_90": WARNING_THRESHOLD_90,
            "enforcement_mode": "alert",
            "enabled": True,
        }

    # User-specific policy always wins
    user_key = f"user:{email}"
    if user_key in policies_cache:
        policy = policies_cache[user_key]
        if policy.get("enabled"):
            return policy

    # Group (most restrictive)
    group_policies = []
    for group in groups or []:
        group_key = f"group:{group}"
        if group_key in policies_cache:
            policy = policies_cache[group_key]
            if policy.get("enabled"):
                group_policies.append(policy)

    if group_policies:
        return min(group_policies, key=lambda p: p.get("monthly_cost_limit") or p.get("monthly_token_limit", float("inf")) or float("inf"))

    # Default
    default_key = "default:default"
    if default_key in policies_cache:
        policy = policies_cache[default_key]
        if policy.get("enabled"):
            return policy

    return None


def check_limits_and_generate_alerts(
    email, total_cost, daily_cost, total_tokens, daily_tokens, policy,
    month_name, current_date, days_remaining, days_in_month, sent_alerts
):
    """Check all limit types and generate appropriate alerts."""
    alerts = []
    policy_info = f"{policy['policy_type']}:{policy['identifier']}"
    enforcement_mode = policy.get('enforcement_mode', 'alert')

    monthly_cost_limit = policy.get("monthly_cost_limit", 0)

    # Cost-based alerts (primary)
    if monthly_cost_limit and monthly_cost_limit > 0:
        cost_pct = (total_cost / monthly_cost_limit) * 100
        threshold_80 = monthly_cost_limit * 0.8
        threshold_90 = monthly_cost_limit * 0.9

        daily_average_cost = total_cost / max(1, int(current_date.split("-")[2]))
        projected_cost = daily_average_cost * days_in_month

        alert_level = None
        if total_cost > monthly_cost_limit:
            alert_level = "exceeded"
        elif total_cost > threshold_90:
            alert_level = "critical"
        elif total_cost > threshold_80:
            alert_level = "warning"

        if alert_level:
            alert_key = f"{email}#monthly_cost#{alert_level}"
            if alert_key not in sent_alerts:
                alerts.append({
                    "user": email,
                    "alert_type": "monthly_cost",
                    "alert_level": alert_level,
                    "current_usage": round(total_cost, 2),
                    "limit": monthly_cost_limit,
                    "percentage": round(cost_pct, 1),
                    "month": month_name,
                    "days_remaining": days_remaining,
                    "daily_average": round(daily_average_cost, 2),
                    "projected_total": round(projected_cost, 2),
                    "policy_info": policy_info,
                    "enforcement_mode": enforcement_mode,
                    "unit": "USD",
                })

        # Daily cost check
        daily_cost_limit = policy.get("daily_cost_limit")
        if daily_cost_limit and daily_cost_limit > 0:
            daily_alert_level = None
            if daily_cost > daily_cost_limit:
                daily_alert_level = "exceeded"
            elif daily_cost > daily_cost_limit * 0.9:
                daily_alert_level = "critical"
            elif daily_cost > daily_cost_limit * 0.8:
                daily_alert_level = "warning"

            if daily_alert_level:
                alert_key = f"{email}#daily_cost#{current_date}#{daily_alert_level}"
                if alert_key not in sent_alerts:
                    alerts.append({
                        "user": email,
                        "alert_type": "daily_cost",
                        "alert_level": daily_alert_level,
                        "current_usage": round(daily_cost, 2),
                        "limit": daily_cost_limit,
                        "percentage": round(daily_cost / daily_cost_limit * 100, 1),
                        "date": current_date,
                        "policy_info": policy_info,
                        "enforcement_mode": enforcement_mode,
                        "unit": "USD",
                    })

    else:
        # Token-based alerts (fallback)
        monthly_limit = policy.get("monthly_token_limit", 0)
        if monthly_limit and monthly_limit > 0:
            monthly_pct = (total_tokens / monthly_limit) * 100
            daily_average = total_tokens / max(1, int(current_date.split("-")[2]))
            projected_total = daily_average * days_in_month

            alert_level = None
            if total_tokens > monthly_limit:
                alert_level = "exceeded"
            elif total_tokens > policy.get("warning_threshold_90", monthly_limit * 0.9):
                alert_level = "critical"
            elif total_tokens > policy.get("warning_threshold_80", monthly_limit * 0.8):
                alert_level = "warning"

            if alert_level:
                alert_key = f"{email}#monthly#{alert_level}"
                if alert_key not in sent_alerts:
                    alerts.append({
                        "user": email,
                        "alert_type": "monthly",
                        "alert_level": alert_level,
                        "current_usage": int(total_tokens),
                        "limit": monthly_limit,
                        "percentage": round(monthly_pct, 1),
                        "month": month_name,
                        "days_remaining": days_remaining,
                        "daily_average": int(daily_average),
                        "projected_total": int(projected_total),
                        "policy_info": policy_info,
                        "enforcement_mode": enforcement_mode,
                        "unit": "tokens",
                    })

        daily_limit = policy.get("daily_token_limit")
        if daily_limit and daily_limit > 0:
            daily_alert_level = None
            if daily_tokens > daily_limit:
                daily_alert_level = "exceeded"
            elif daily_tokens > (daily_limit * 0.9):
                daily_alert_level = "critical"
            elif daily_tokens > (daily_limit * 0.8):
                daily_alert_level = "warning"

            if daily_alert_level:
                alert_key = f"{email}#daily#{current_date}#{daily_alert_level}"
                if alert_key not in sent_alerts:
                    alerts.append({
                        "user": email,
                        "alert_type": "daily",
                        "alert_level": daily_alert_level,
                        "current_usage": int(daily_tokens),
                        "limit": daily_limit,
                        "percentage": round(daily_tokens / daily_limit * 100, 1),
                        "date": current_date,
                        "policy_info": policy_info,
                        "enforcement_mode": enforcement_mode,
                        "unit": "tokens",
                    })

    return alerts


def get_sent_alerts(month_name):
    sent_alerts = set()
    try:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        response = quota_table.query(
            KeyConditionExpression=Key("pk").eq("ALERTS")
            & Key("sk").begins_with(f"{month_prefix}#ALERT#")
        )

        def _process(items):
            for item in items:
                sk_parts = item["sk"].split("#")
                if len(sk_parts) >= 5:
                    email = sk_parts[2]
                    alert_type = sk_parts[3]
                    alert_level = sk_parts[4]
                    if alert_type in ("daily", "daily_cost") and len(sk_parts) >= 6:
                        date = sk_parts[5]
                        sent_alerts.add(f"{email}#{alert_type}#{date}#{alert_level}")
                    else:
                        sent_alerts.add(f"{email}#{alert_type}#{alert_level}")

        _process(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            response = quota_table.query(
                KeyConditionExpression=Key("pk").eq("ALERTS")
                & Key("sk").begins_with(f"{month_prefix}#ALERT#"),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            _process(response.get("Items", []))

        if sent_alerts:
            print(f"Found {len(sent_alerts)} alerts already sent this month")

    except Exception as e:
        print(f"Error checking sent alerts: {str(e)}")

    return sent_alerts


def record_sent_alert(month_name, email, alert_type, alert_level, alert_data):
    try:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")

        if alert_type in ("daily", "daily_cost"):
            date = alert_data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            sk = f"{month_prefix}#ALERT#{email}#{alert_type}#{alert_level}#{date}"
        else:
            sk = f"{month_prefix}#ALERT#{email}#{alert_type}#{alert_level}"

        quota_table.put_item(
            Item={
                "pk": "ALERTS",
                "sk": sk,
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "month": month_name,
                "email": email,
                "alert_type": alert_type,
                "alert_level": alert_level,
                "usage_at_alert": Decimal(str(alert_data.get("current_usage", 0))),
                "limit_at_alert": Decimal(str(alert_data.get("limit", 0))),
                "policy_info": alert_data.get("policy_info", ""),
                "unit": alert_data.get("unit", "tokens"),
                "ttl": int(datetime.now(timezone.utc).timestamp()) + (60 * 86400),
            }
        )
    except Exception as e:
        print(f"Error recording sent alert: {str(e)}")


def send_alerts(alerts):
    if not SNS_TOPIC_ARN:
        print("Warning: SNS_TOPIC_ARN not configured - skipping alert sending")
        return

    for alert in alerts:
        try:
            alert_type = alert.get("alert_type", "monthly")
            alert_level = alert["alert_level"]
            unit = alert.get("unit", "tokens")

            level_prefix = {
                "warning": "WARNING",
                "critical": "CRITICAL",
                "exceeded": "EXCEEDED",
            }.get(alert_level, "ALERT")

            type_label = {
                "monthly_cost": "Monthly Cost Quota",
                "daily_cost": "Daily Cost Quota",
                "monthly": "Monthly Token Quota",
                "daily": "Daily Token Quota",
            }.get(alert_type, "Quota")

            if unit == "USD":
                subject = f"Claude Code {level_prefix} - {type_label} - ${alert['current_usage']:.2f}/${alert['limit']:.2f}"
            else:
                subject = f"Claude Code {level_prefix} - {type_label} - {alert['percentage']:.0f}%"

            if "cost" in alert_type:
                message = format_cost_alert(alert)
            elif alert_type == "daily":
                message = format_daily_alert(alert)
            else:
                message = format_monthly_alert(alert)

            sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=subject,
                Message=message,
                MessageAttributes={
                    "user": {"DataType": "String", "StringValue": alert["user"]},
                    "alert_type": {"DataType": "String", "StringValue": alert_type},
                    "alert_level": {"DataType": "String", "StringValue": alert_level},
                    "percentage": {"DataType": "Number", "StringValue": str(alert["percentage"])},
                },
            )

            print(f"Sent {alert_type} {alert_level} alert for {alert['user']} ({alert['percentage']:.1f}%)")

        except Exception as e:
            print(f"Error sending alert for {alert['user']}: {str(e)}")


def format_cost_alert(alert):
    enforcement = alert.get('enforcement_mode', 'alert')
    user_email = alert['user']
    is_daily = "daily" in alert.get("alert_type", "")
    period = "Daily" if is_daily else "Monthly"

    return f"""
=====================================
CLAUDE CODE COST QUOTA ALERT
=====================================

USER: {user_email}
ALERT: {period} Cost Quota - {alert['alert_level'].upper()}
{'DATE: ' + alert.get('date', 'N/A') if is_daily else 'MONTH: ' + alert.get('month', 'N/A')}

-------------------------------------
CURRENT USAGE
-------------------------------------
{period} Cost: ${alert['current_usage']:.2f} / ${alert['limit']:.2f} ({alert['percentage']:.1f}%)
{f"Daily Average: ${alert.get('daily_average', 0):.2f}" if not is_daily else ""}
{f"Projected Monthly: ${alert.get('projected_total', 0):.2f}" if not is_daily else ""}
{f"Days Remaining: {alert.get('days_remaining', 'N/A')}" if not is_daily else ""}

Policy: {alert.get('policy_info', 'default')}
Enforcement: {enforcement}

-------------------------------------
ACTION REQUIRED
-------------------------------------
{"ACCESS IS BLOCKED until quota resets or admin unblocks." if enforcement == "block" and alert['alert_level'] == 'exceeded' else "User may soon exceed cost quota."}

=====================================
"""


def format_monthly_alert(alert):
    enforcement = alert.get('enforcement_mode', 'alert')
    user_email = alert['user']

    return f"""
=====================================
CLAUDE CODE QUOTA ALERT
=====================================

USER: {user_email}
ALERT: Monthly Token Quota - {alert['alert_level'].upper()}
MONTH: {alert.get('month', 'N/A')}

-------------------------------------
CURRENT USAGE
-------------------------------------
Monthly Tokens: {alert['current_usage']:,} / {alert['limit']:,} ({alert['percentage']:.1f}%)
Daily Average: {alert.get('daily_average', 0):,} tokens
Projected Monthly: {alert.get('projected_total', 0):,} tokens
Days Remaining: {alert.get('days_remaining', 'N/A')}

Policy: {alert.get('policy_info', 'default')}
Enforcement: {enforcement}

=====================================
"""


def format_daily_alert(alert):
    enforcement = alert.get('enforcement_mode', 'alert')
    user_email = alert['user']

    return f"""
=====================================
CLAUDE CODE QUOTA ALERT
=====================================

USER: {user_email}
ALERT: Daily Token Quota - {alert['alert_level'].upper()}
DATE: {alert.get('date', 'N/A')}

-------------------------------------
CURRENT USAGE
-------------------------------------
Daily Tokens: {alert['current_usage']:,} / {alert['limit']:,} ({alert['percentage']:.1f}%)

Policy: {alert.get('policy_info', 'default')}
Enforcement: {enforcement}

=====================================
"""
