# ABOUTME: Lambda function for real-time quota checking before credential issuance
# ABOUTME: Returns allowed/blocked status based on cost-based quota policy and current usage
# ABOUTME: Policy precedence: user-specific > group > default. Personal policy always wins.
# ABOUTME: Supports both cost-based (primary) and token-based (fallback) limits.

import json
import boto3
import os
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key, Attr

# Initialize clients
dynamodb = boto3.resource("dynamodb")

# Configuration
QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE", "QuotaPolicies")
MISSING_EMAIL_ENFORCEMENT = os.environ.get("MISSING_EMAIL_ENFORCEMENT", "block")
ERROR_HANDLING_MODE = os.environ.get("ERROR_HANDLING_MODE", "fail_closed")

# DynamoDB tables
quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    """Real-time quota check for credential issuance."""
    try:
        authorizer_context = event.get("requestContext", {}).get("authorizer", {})
        jwt_claims = authorizer_context.get("jwt", {}).get("claims", {})
        email = jwt_claims.get("email")
        groups = extract_groups_from_claims(jwt_claims)

        if not email:
            print(f"JWT missing email claim. Available claims: {list(jwt_claims.keys())}")
            allow_missing_email = MISSING_EMAIL_ENFORCEMENT != "block"
            return build_response(200, {
                "error": "No email claim in JWT token",
                "allowed": allow_missing_email,
                "reason": "missing_email_claim",
                "message": "JWT token does not contain email claim" + (" - quota check skipped" if allow_missing_email else " - access denied for security")
            })

        # 1. Resolve effective quota policy
        policy = resolve_quota_for_user(email, groups)

        if policy is None:
            return build_response(200, {
                "allowed": True,
                "reason": "no_policy",
                "enforcement_mode": None,
                "usage": None,
                "policy": None,
                "unblock_status": None,
                "message": "No quota policy configured - unlimited access"
            })

        # 2. Check for active unblock override
        unblock_status = get_unblock_status(email)
        if unblock_status and unblock_status.get("is_unblocked"):
            return build_response(200, {
                "allowed": True,
                "reason": "unblocked",
                "enforcement_mode": policy.get("enforcement_mode", "alert"),
                "usage": get_user_usage_summary(email, policy),
                "policy": {"type": policy.get("policy_type"), "identifier": policy.get("identifier")},
                "unblock_status": unblock_status,
                "message": f"Access granted - temporarily unblocked until {unblock_status.get('expires_at')}"
            })

        # 3. Get current usage
        usage = get_user_usage(email)
        usage_summary = build_usage_summary(usage, policy)

        # 4. Check enforcement mode
        enforcement_mode = policy.get("enforcement_mode", "alert")
        if enforcement_mode != "block":
            return build_response(200, {
                "allowed": True,
                "reason": "within_quota",
                "enforcement_mode": enforcement_mode,
                "usage": usage_summary,
                "policy": {"type": policy.get("policy_type"), "identifier": policy.get("identifier")},
                "unblock_status": {"is_unblocked": False},
                "message": "Access granted - enforcement mode is alert-only"
            })

        # 5. Check cost limits (primary) then token limits (fallback)
        monthly_cost_limit = policy.get("monthly_cost_limit", 0)
        daily_cost_limit = policy.get("daily_cost_limit")
        total_cost = usage.get("total_cost", 0)
        daily_cost = usage.get("daily_cost", 0)

        policy_info = {"type": policy.get("policy_type"), "identifier": policy.get("identifier")}
        unblock_info = {"is_unblocked": False}

        # Cost-based check (primary)
        if monthly_cost_limit and monthly_cost_limit > 0:
            if total_cost >= monthly_cost_limit:
                return build_response(200, {
                    "allowed": False,
                    "reason": "monthly_cost_exceeded",
                    "enforcement_mode": enforcement_mode,
                    "usage": usage_summary,
                    "policy": policy_info,
                    "unblock_status": unblock_info,
                    "message": f"Monthly cost quota exceeded: ${total_cost:.2f} / ${monthly_cost_limit:.2f} ({total_cost/monthly_cost_limit*100:.1f}%). Contact your administrator."
                })

            if daily_cost_limit and daily_cost_limit > 0 and daily_cost >= daily_cost_limit:
                return build_response(200, {
                    "allowed": False,
                    "reason": "daily_cost_exceeded",
                    "enforcement_mode": enforcement_mode,
                    "usage": usage_summary,
                    "policy": policy_info,
                    "unblock_status": unblock_info,
                    "message": f"Daily cost quota exceeded: ${daily_cost:.2f} / ${daily_cost_limit:.2f} ({daily_cost/daily_cost_limit*100:.1f}%). Resets at UTC midnight."
                })

        # Token-based check (fallback if no cost limit)
        if not monthly_cost_limit or monthly_cost_limit <= 0:
            monthly_tokens = usage.get("total_tokens", 0)
            daily_tokens = usage.get("daily_tokens", 0)
            monthly_token_limit = policy.get("monthly_token_limit", 0)
            daily_token_limit = policy.get("daily_token_limit")

            if monthly_token_limit > 0 and monthly_tokens >= monthly_token_limit:
                return build_response(200, {
                    "allowed": False,
                    "reason": "monthly_exceeded",
                    "enforcement_mode": enforcement_mode,
                    "usage": usage_summary,
                    "policy": policy_info,
                    "unblock_status": unblock_info,
                    "message": f"Monthly token quota exceeded: {int(monthly_tokens):,} / {int(monthly_token_limit):,} tokens ({monthly_tokens/monthly_token_limit*100:.1f}%)."
                })

            if daily_token_limit and daily_token_limit > 0 and daily_tokens >= daily_token_limit:
                return build_response(200, {
                    "allowed": False,
                    "reason": "daily_exceeded",
                    "enforcement_mode": enforcement_mode,
                    "usage": usage_summary,
                    "policy": policy_info,
                    "unblock_status": unblock_info,
                    "message": f"Daily token quota exceeded: {int(daily_tokens):,} / {int(daily_token_limit):,} tokens."
                })

        # All checks passed
        return build_response(200, {
            "allowed": True,
            "reason": "within_quota",
            "enforcement_mode": enforcement_mode,
            "usage": usage_summary,
            "policy": policy_info,
            "unblock_status": unblock_info,
            "message": "Access granted - within quota limits"
        })

    except Exception as e:
        print(f"Error during quota check: {str(e)}")
        import traceback
        traceback.print_exc()
        allow_on_error = ERROR_HANDLING_MODE != "fail_closed"
        return build_response(200, {
            "allowed": allow_on_error,
            "reason": "check_failed",
            "enforcement_mode": None,
            "usage": None,
            "policy": None,
            "unblock_status": None,
            "message": f"Quota check failed ({ERROR_HANDLING_MODE}): {str(e)}"
        })


def build_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization"
        },
        "body": json.dumps(body, cls=DecimalEncoder)
    }


def extract_groups_from_claims(claims: dict) -> list:
    """Extract group memberships from JWT token claims."""
    groups = []

    if "groups" in claims:
        claim_groups = claims["groups"]
        if isinstance(claim_groups, list):
            groups.extend(claim_groups)
        elif isinstance(claim_groups, str):
            groups.extend([g.strip() for g in claim_groups.split(",") if g.strip()])

    if "cognito:groups" in claims:
        claim_groups = claims["cognito:groups"]
        if isinstance(claim_groups, list):
            groups.extend(claim_groups)
        elif isinstance(claim_groups, str):
            groups.extend([g.strip() for g in claim_groups.split(",") if g.strip()])

    if "custom:department" in claims:
        department = claims["custom:department"]
        if department:
            groups.append(f"department:{department}")

    return list(set(groups))


def resolve_quota_for_user(email: str, groups: list) -> dict | None:
    """
    Resolve effective quota policy. Precedence: user > group > default.
    Personal policy ALWAYS wins.
    """
    user_policy = get_policy("user", email)
    if user_policy and user_policy.get("enabled", True):
        return user_policy

    if groups:
        group_policies = []
        for group in groups:
            group_policy = get_policy("group", group)
            if group_policy and group_policy.get("enabled", True):
                group_policies.append(group_policy)
        if group_policies:
            # Most restrictive = lowest cost limit (or token limit as fallback)
            return min(group_policies, key=lambda p: p.get("monthly_cost_limit") or p.get("monthly_token_limit", float("inf")) or float("inf"))

    default_policy = get_policy("default", "default")
    if default_policy and default_policy.get("enabled", True):
        return default_policy

    return None


def get_policy(policy_type: str, identifier: str) -> dict | None:
    pk = f"POLICY#{policy_type}#{identifier}"
    try:
        response = policies_table.get_item(Key={"pk": pk, "sk": "CURRENT"})
        item = response.get("Item")
        if not item:
            return None
        return {
            "policy_type": item.get("policy_type"),
            "identifier": item.get("identifier"),
            "monthly_cost_limit": float(item.get("monthly_cost_limit", 0)),
            "daily_cost_limit": float(item.get("daily_cost_limit", 0)) if item.get("daily_cost_limit") else None,
            "monthly_token_limit": int(item.get("monthly_token_limit", 0)),
            "daily_token_limit": int(item.get("daily_token_limit", 0)) if item.get("daily_token_limit") else None,
            "warning_threshold_80": float(item.get("warning_threshold_80", 0)),
            "warning_threshold_90": float(item.get("warning_threshold_90", 0)),
            "enforcement_mode": item.get("enforcement_mode", "alert"),
            "enabled": item.get("enabled", True),
        }
    except Exception as e:
        print(f"Error getting policy {policy_type}:{identifier}: {e}")
        return None


def get_unblock_status(email: str) -> dict:
    pk = f"USER#{email}"
    sk = "UNBLOCK#CURRENT"
    try:
        response = quota_table.get_item(Key={"pk": pk, "sk": sk})
        item = response.get("Item")
        if not item:
            return {"is_unblocked": False}
        expires_at = item.get("expires_at")
        if expires_at:
            expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires_dt:
                return {"is_unblocked": False, "expired": True}
        return {
            "is_unblocked": True,
            "expires_at": expires_at,
            "unblocked_by": item.get("unblocked_by"),
            "unblocked_at": item.get("unblocked_at"),
            "reason": item.get("reason"),
            "duration_type": item.get("duration_type")
        }
    except Exception as e:
        print(f"Error checking unblock status for {email}: {e}")
        return {"is_unblocked": False, "error": str(e)}


def get_user_usage(email: str) -> dict:
    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    current_date = now.strftime("%Y-%m-%d")
    pk = f"USER#{email}"
    sk = f"MONTH#{month_prefix}"

    try:
        response = quota_table.get_item(Key={"pk": pk, "sk": sk})
        item = response.get("Item")
        if not item:
            return {
                "total_tokens": 0, "daily_tokens": 0, "daily_date": current_date,
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "total_cost": 0.0, "daily_cost": 0.0,
            }

        daily_date = item.get("daily_date")
        daily_tokens = float(item.get("daily_tokens", 0))
        daily_cost = float(item.get("daily_cost", 0))
        if daily_date != current_date:
            daily_tokens = 0
            daily_cost = 0.0

        return {
            "total_tokens": float(item.get("total_tokens", 0)),
            "daily_tokens": daily_tokens,
            "daily_date": daily_date,
            "input_tokens": float(item.get("input_tokens", 0)),
            "output_tokens": float(item.get("output_tokens", 0)),
            "cache_read_tokens": float(item.get("cache_read_tokens", 0)),
            "cache_write_tokens": float(item.get("cache_write_tokens", 0)),
            "total_cost": float(item.get("total_cost", 0)),
            "daily_cost": daily_cost,
        }
    except Exception as e:
        print(f"Error getting usage for {email}: {e}")
        return {
            "total_tokens": 0, "daily_tokens": 0, "daily_date": current_date,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_cost": 0.0, "daily_cost": 0.0,
        }


def build_usage_summary(usage: dict, policy: dict) -> dict:
    total_cost = usage.get("total_cost", 0)
    daily_cost = usage.get("daily_cost", 0)
    monthly_cost_limit = policy.get("monthly_cost_limit", 0)
    daily_cost_limit = policy.get("daily_cost_limit")

    monthly_tokens = usage.get("total_tokens", 0)
    daily_tokens = usage.get("daily_tokens", 0)
    monthly_token_limit = policy.get("monthly_token_limit", 0)
    daily_token_limit = policy.get("daily_token_limit")

    summary = {
        "monthly_cost": round(total_cost, 4),
        "monthly_cost_limit": monthly_cost_limit,
        "monthly_cost_percent": round(total_cost / monthly_cost_limit * 100, 1) if monthly_cost_limit > 0 else 0,
        "daily_cost": round(daily_cost, 4),
        "monthly_tokens": int(monthly_tokens),
        "monthly_token_limit": monthly_token_limit,
        "monthly_token_percent": round(monthly_tokens / monthly_token_limit * 100, 1) if monthly_token_limit > 0 else 0,
        "daily_tokens": int(daily_tokens),
    }

    if daily_cost_limit:
        summary["daily_cost_limit"] = daily_cost_limit
        summary["daily_cost_percent"] = round(daily_cost / daily_cost_limit * 100, 1) if daily_cost_limit > 0 else 0

    if daily_token_limit:
        summary["daily_token_limit"] = daily_token_limit
        summary["daily_token_percent"] = round(daily_tokens / daily_token_limit * 100, 1) if daily_token_limit > 0 else 0

    return summary


def get_user_usage_summary(email: str, policy: dict) -> dict:
    usage = get_user_usage(email)
    return build_usage_summary(usage, policy)
