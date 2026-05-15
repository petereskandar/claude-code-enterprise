import json
import os
import boto3
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key, Attr

dynamodb = boto3.resource("dynamodb")

QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE", "QuotaPolicies")
DIRECTORY_TABLE = os.environ.get("DIRECTORY_TABLE", "UserDirectory")

quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE)
directory_table = dynamodb.Table(DIRECTORY_TABLE)


def handler(event, context):
    path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    params = event.get("queryStringParameters") or {}

    # Extract email from JWT claims (set by API Gateway JWT authorizer)
    jwt_claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    email = (jwt_claims.get("email") or jwt_claims.get("preferred_username") or "").lower()

    if not email:
        return _response(401, {"error": "Unauthorized: missing email claim in token"})

    try:
        if path == "/api/my-usage" and method == "GET":
            return handle_my_usage(email, params)
        elif path == "/api/my-models" and method == "GET":
            return handle_my_models(email, params)
        elif path == "/api/my-available-months" and method == "GET":
            return handle_available_months(email)
        else:
            return _response(404, {"error": "Not found"})
    except Exception as e:
        print(f"Error handling {method} {path}: {e}")
        import traceback
        traceback.print_exc()
        return _response(500, {"error": str(e)})


def handle_my_usage(email, params):
    month = params.get("month")
    month_from = params.get("from")
    month_to = params.get("to")

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    months = _resolve_months(month, month_from, month_to)

    total_cost = 0.0
    total_tokens = 0.0
    input_tokens = 0.0
    output_tokens = 0.0
    cache_read_tokens = 0.0
    cache_write_tokens = 0.0
    daily_cost = 0.0
    daily_tokens = 0.0
    groups = []
    monthly_breakdown = []

    for m in months:
        try:
            response = quota_table.get_item(Key={"pk": f"USER#{email}", "sk": f"MONTH#{m}"})
            item = response.get("Item")
        except Exception:
            item = None
        if not item:
            monthly_breakdown.append({"month": m, "cost": 0, "tokens": 0})
            continue

        m_cost = float(item.get("total_cost", 0))
        m_tokens = float(item.get("total_tokens", 0))
        total_cost += m_cost
        total_tokens += m_tokens
        input_tokens += float(item.get("input_tokens", 0))
        output_tokens += float(item.get("output_tokens", 0))
        cache_read_tokens += float(item.get("cache_read_tokens", 0))
        cache_write_tokens += float(item.get("cache_write_tokens", 0))
        if not groups:
            groups = item.get("groups", [])

        monthly_breakdown.append({"month": m, "cost": m_cost, "tokens": m_tokens})

        if m == months[-1]:
            d_cost = float(item.get("daily_cost", 0))
            d_tokens = float(item.get("daily_tokens", 0))
            if item.get("daily_date") == current_date:
                daily_cost = d_cost
                daily_tokens = d_tokens

    # Resolve policy
    policies = _load_all_policies()
    default_policy = policies.get("default:default")
    policy = _resolve_policy(email, groups, policies, default_policy)

    monthly_cost_limit = (policy or {}).get("monthly_cost_limit", 0)
    limit_for_range = monthly_cost_limit * len(months) if monthly_cost_limit > 0 else 0
    daily_cost_limit = (policy or {}).get("daily_cost_limit")
    pct = (total_cost / limit_for_range * 100) if limit_for_range > 0 else 0

    # Directory info
    dir_entry = _get_user_directory_entry(email)

    return _response(200, {
        "email": email,
        "nome_cognome": dir_entry.get("nome_cognome", ""),
        "responsabile": dir_entry.get("responsabile", ""),
        "II_livello": dir_entry.get("II_livello", ""),
        "III_livello": dir_entry.get("III_livello", ""),
        "IV_livello": dir_entry.get("IV_livello", ""),
        "total_cost": total_cost,
        "daily_cost": daily_cost,
        "total_tokens": total_tokens,
        "daily_tokens": daily_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "groups": groups,
        "monthly_cost_limit": limit_for_range,
        "daily_cost_limit": daily_cost_limit,
        "enforcement_mode": (policy or {}).get("enforcement_mode", "alert"),
        "percentage": pct,
        "months": months,
        "monthly_breakdown": monthly_breakdown,
    })


def handle_my_models(email, params):
    month = params.get("month")
    month_from = params.get("from")
    month_to = params.get("to")

    months = _resolve_months(month, month_from, month_to)
    models_agg = {}

    for m in months:
        try:
            response = quota_table.get_item(Key={"pk": f"USER#{email}", "sk": f"MONTH#{m}"})
            item = response.get("Item")
        except Exception:
            item = None
        if not item:
            continue

        models_map = item.get("models", {})
        for model_id, tokens in models_map.items():
            if model_id not in models_agg:
                models_agg[model_id] = {
                    "model": model_id,
                    "input": 0, "output": 0,
                    "cache_read": 0, "cache_write": 0,
                    "cost": 0,
                }
            models_agg[model_id]["input"] += float(tokens.get("input", 0))
            models_agg[model_id]["output"] += float(tokens.get("output", 0))
            models_agg[model_id]["cache_read"] += float(tokens.get("cache_read", 0))
            models_agg[model_id]["cache_write"] += float(tokens.get("cache_write", 0))
            models_agg[model_id]["cost"] += float(tokens.get("cost", 0))

    models_list = sorted(models_agg.values(), key=lambda x: x["cost"], reverse=True)
    total_cost = sum(m["cost"] for m in models_list)

    for m in models_list:
        m["percentage"] = (m["cost"] / total_cost * 100) if total_cost > 0 else 0

    return _response(200, {
        "models": models_list,
        "total_cost": total_cost,
        "months": months,
    })


def handle_available_months(email):
    months = set()
    try:
        response = quota_table.query(
            KeyConditionExpression=Key("pk").eq(f"USER#{email}") & Key("sk").begins_with("MONTH#"),
            ProjectionExpression="sk",
        )
        for item in response.get("Items", []):
            sk = item.get("sk", "")
            if sk.startswith("MONTH#"):
                months.add(sk.replace("MONTH#", ""))
    except Exception as e:
        print(f"Error fetching available months: {e}")

    sorted_months = sorted(months, reverse=True)
    return _response(200, {"months": sorted_months})


# ============================================================
# Helpers
# ============================================================

def _resolve_months(month, month_from, month_to):
    if month_from and month_to:
        months = []
        current = month_from
        while current <= month_to:
            months.append(current)
            y, m = int(current[:4]), int(current[5:7])
            m += 1
            if m > 12:
                m = 1
                y += 1
            current = f"{y:04d}-{m:02d}"
        return months
    elif month:
        return [month]
    else:
        return [datetime.now(timezone.utc).strftime("%Y-%m")]


def _load_all_policies():
    policies = {}
    try:
        response = policies_table.scan(FilterExpression=Attr("sk").eq("CURRENT"))

        def _process(items):
            for item in items:
                pt = item.get("policy_type", "")
                ident = item.get("identifier", "")
                if pt and ident:
                    policies[f"{pt}:{ident}"] = {
                        "policy_type": pt,
                        "identifier": ident,
                        "monthly_cost_limit": float(item.get("monthly_cost_limit", 0)),
                        "daily_cost_limit": float(item.get("daily_cost_limit", 0)) if item.get("daily_cost_limit") else None,
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
        print(f"Error loading policies: {e}")
    return policies


def _resolve_policy(email, groups, all_policies, default_policy):
    user_key = f"user:{email}"
    if user_key in all_policies:
        p = all_policies[user_key]
        if p.get("enabled", True):
            return p

    if groups:
        group_policies = []
        for g in groups:
            gk = f"group:{g}"
            if gk in all_policies:
                p = all_policies[gk]
                if p.get("enabled", True):
                    group_policies.append(p)
        if group_policies:
            return min(group_policies, key=lambda p: p.get("monthly_cost_limit") or float("inf"))

    if default_policy and default_policy.get("enabled", True):
        return default_policy
    return None


def _get_user_directory_entry(email):
    try:
        response = directory_table.get_item(Key={"email": email.lower()})
        item = response.get("Item", {})
        return {
            "responsabile": item.get("responsabile", ""),
            "nome_cognome": item.get("nome_cognome", ""),
            "II_livello": item.get("II_livello", ""),
            "III_livello": item.get("III_livello", ""),
            "IV_livello": item.get("IV_livello", ""),
        }
    except Exception:
        return {}


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(body, default=_decimal_default),
    }


def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
