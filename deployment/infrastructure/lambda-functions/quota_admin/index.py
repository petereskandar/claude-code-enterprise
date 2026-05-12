import json
import os
import boto3
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key, Attr
from urllib.parse import unquote

dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")

QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE", "QuotaPolicies")
METRICS_TABLE = os.environ.get("METRICS_TABLE", "ClaudeCodeMetrics")
DIRECTORY_TABLE = os.environ.get("DIRECTORY_TABLE", "UserDirectory")
ADMIN_EMAILS_PARAM = os.environ.get("ADMIN_EMAILS_PARAM", "/claude-code/quota/admin-emails")

quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE)
metrics_table = dynamodb.Table(METRICS_TABLE)
directory_table = dynamodb.Table(DIRECTORY_TABLE)

_admin_emails_cache = None
_admin_emails_cache_time = 0
_ADMIN_CACHE_TTL = 300


def handler(event, context):
    path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    caller_email = _get_caller_email(event)
    if not caller_email:
        return _response(401, {"error": "Unauthorized"})
    if not _is_admin(caller_email):
        return _response(403, {"error": "Forbidden - not an admin"})

    try:
        if path == "/api/overview" and method == "GET":
            return handle_overview()
        elif path == "/api/users" and method == "GET":
            params = event.get("queryStringParameters") or {}
            return handle_users(params)
        elif path.startswith("/api/users/") and method == "GET":
            email = unquote(path.split("/api/users/")[1])
            return handle_user_detail(email)
        elif path == "/api/groups" and method == "GET":
            return handle_groups()
        elif path == "/api/policies" and method == "GET":
            return handle_policies()
        elif path.startswith("/api/policies/") and method == "PUT":
            parts = path.split("/api/policies/")[1].split("/", 1)
            if len(parts) == 2:
                ptype = unquote(parts[0])
                identifier = unquote(parts[1])
                body = json.loads(event.get("body", "{}"))
                return handle_policy_put(ptype, identifier, body)
            return _response(400, {"error": "Invalid path"})
        elif path.startswith("/api/policies/") and method == "DELETE":
            parts = path.split("/api/policies/")[1].split("/", 1)
            if len(parts) == 2:
                ptype = unquote(parts[0])
                identifier = unquote(parts[1])
                return handle_policy_delete(ptype, identifier)
            return _response(400, {"error": "Invalid path"})
        else:
            return _response(404, {"error": "Not found"})
    except Exception as e:
        print(f"Error handling {method} {path}: {e}")
        import traceback
        traceback.print_exc()
        return _response(500, {"error": str(e)})


def handle_overview():
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    users = _get_all_users_usage(month_prefix, current_date)
    policies = _load_all_policies()
    default_policy = policies.get("default:default")

    total_cost = 0
    over_quota = 0
    blocked = 0

    user_costs = []
    for email, usage in users.items():
        cost = usage.get("total_cost", 0)
        total_cost += cost
        groups = usage.get("groups", [])
        policy = _resolve_policy(email, groups, policies, default_policy)
        limit = (policy or {}).get("monthly_cost_limit", 0)
        pct = (cost / limit * 100) if limit > 0 else 0
        if pct > 100:
            over_quota += 1
        enforcement = (policy or {}).get("enforcement_mode", "alert")
        if pct > 100 and enforcement == "block":
            blocked += 1
        user_costs.append({"email": email, "total_cost": cost, "percentage": pct})

    user_costs.sort(key=lambda x: x["total_cost"], reverse=True)

    return _response(200, {
        "total_users": len(users),
        "total_cost": total_cost,
        "over_quota": over_quota,
        "blocked": blocked,
        "top_consumers": user_costs[:10],
    })


def handle_users(params):
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 20))
    search = params.get("search", "").lower().strip()

    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    all_users = _get_all_users_usage(month_prefix, current_date)
    policies = _load_all_policies()
    default_policy = policies.get("default:default")
    directory = _load_user_directory()

    user_list = []
    for email, usage in all_users.items():
        if search and search not in email.lower():
            continue
        groups = usage.get("groups", [])
        policy = _resolve_policy(email, groups, policies, default_policy)
        limit = (policy or {}).get("monthly_cost_limit", 0)
        cost = usage.get("total_cost", 0)
        pct = (cost / limit * 100) if limit > 0 else 0
        policy_type = (policy or {}).get("policy_type", "default")

        dir_entry = directory.get(email, {})
        user_list.append({
            "email": email,
            "total_cost": cost,
            "limit": limit,
            "percentage": pct,
            "policy_type": policy_type,
            "iii_livello": dir_entry.get("iii_livello", ""),
            "business_unit": dir_entry.get("business_unit", ""),
        })

    user_list.sort(key=lambda x: x["total_cost"], reverse=True)
    total = len(user_list)
    start = (page - 1) * page_size
    end = start + page_size

    return _response(200, {
        "users": user_list[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


def handle_user_detail(email):
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        response = quota_table.get_item(Key={"pk": f"USER#{email}", "sk": f"MONTH#{month_prefix}"})
        item = response.get("Item")
    except Exception:
        item = None

    if not item:
        return _response(404, {"error": "User not found for current month"})

    daily_cost = float(item.get("daily_cost", 0))
    daily_tokens = float(item.get("daily_tokens", 0))
    if item.get("daily_date") != current_date:
        daily_cost = 0.0
        daily_tokens = 0.0

    groups = item.get("groups", [])
    policies = _load_all_policies()
    default_policy = policies.get("default:default")
    policy = _resolve_policy(email, groups, policies, default_policy)

    monthly_cost_limit = (policy or {}).get("monthly_cost_limit", 0)
    daily_cost_limit = (policy or {}).get("daily_cost_limit")
    total_cost = float(item.get("total_cost", 0))
    pct = (total_cost / monthly_cost_limit * 100) if monthly_cost_limit > 0 else 0

    dir_entry = _get_user_directory_entry(email)

    return _response(200, {
        "email": email,
        "total_cost": total_cost,
        "daily_cost": daily_cost,
        "total_tokens": float(item.get("total_tokens", 0)),
        "daily_tokens": daily_tokens,
        "input_tokens": float(item.get("input_tokens", 0)),
        "output_tokens": float(item.get("output_tokens", 0)),
        "cache_read_tokens": float(item.get("cache_read_tokens", 0)),
        "cache_write_tokens": float(item.get("cache_write_tokens", 0)),
        "groups": groups,
        "iii_livello": dir_entry.get("iii_livello", ""),
        "business_unit": dir_entry.get("business_unit", ""),
        "nome_cognome": dir_entry.get("nome_cognome", ""),
        "policy_type": (policy or {}).get("policy_type", "default"),
        "policy_identifier": (policy or {}).get("identifier", "default"),
        "monthly_cost_limit": monthly_cost_limit,
        "daily_cost_limit": daily_cost_limit,
        "enforcement_mode": (policy or {}).get("enforcement_mode", "alert"),
        "percentage": pct,
    })


def handle_groups():
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    all_users = _get_all_users_usage(month_prefix, current_date)
    policies = _load_all_policies()

    groups_map = {}
    for email, usage in all_users.items():
        user_groups = usage.get("groups", [])
        for g in user_groups:
            if g not in groups_map:
                groups_map[g] = {"name": g, "user_count": 0, "total_cost": 0}
            groups_map[g]["user_count"] += 1
            groups_map[g]["total_cost"] += usage.get("total_cost", 0)

    for g_name, g_data in groups_map.items():
        policy_key = f"group:{g_name}"
        if policy_key in policies:
            p = policies[policy_key]
            g_data["monthly_cost_limit"] = p.get("monthly_cost_limit", 0)
            g_data["enforcement_mode"] = p.get("enforcement_mode", "alert")
        else:
            g_data["monthly_cost_limit"] = None
            g_data["enforcement_mode"] = None

    groups_list = sorted(groups_map.values(), key=lambda x: x["total_cost"], reverse=True)

    return _response(200, {"groups": groups_list})


def handle_policies():
    policies = _load_all_policies()
    policy_list = []
    for key, p in policies.items():
        policy_list.append({
            "policy_type": p["policy_type"],
            "identifier": p["identifier"],
            "monthly_cost_limit": p.get("monthly_cost_limit", 0),
            "daily_cost_limit": p.get("daily_cost_limit"),
            "monthly_token_limit": p.get("monthly_token_limit", 0),
            "daily_token_limit": p.get("daily_token_limit"),
            "enforcement_mode": p.get("enforcement_mode", "alert"),
            "enabled": p.get("enabled", True),
        })

    policy_list.sort(key=lambda x: (x["policy_type"], x["identifier"]))
    return _response(200, {"policies": policy_list})


def handle_policy_put(ptype, identifier, body):
    if ptype not in ("user", "group", "default"):
        return _response(400, {"error": "Invalid policy type"})

    now = datetime.now(timezone.utc).isoformat()
    item = {
        "pk": f"{ptype}:{identifier}",
        "sk": "CURRENT",
        "policy_type": ptype,
        "identifier": identifier,
        "monthly_cost_limit": Decimal(str(body.get("monthly_cost_limit", 0))),
        "daily_cost_limit": Decimal(str(body.get("daily_cost_limit", 0))) if body.get("daily_cost_limit") else None,
        "monthly_token_limit": int(body.get("monthly_token_limit", 0)),
        "daily_token_limit": int(body.get("daily_token_limit", 0)) if body.get("daily_token_limit") else None,
        "enforcement_mode": body.get("enforcement_mode", "alert"),
        "enabled": body.get("enabled", True),
        "updated_at": now,
    }

    item = {k: v for k, v in item.items() if v is not None}
    policies_table.put_item(Item=item)

    return _response(200, {"message": "Policy saved", "policy_type": ptype, "identifier": identifier})


def handle_policy_delete(ptype, identifier):
    policies_table.delete_item(Key={"pk": f"{ptype}:{identifier}", "sk": "CURRENT"})
    return _response(200, {"message": "Policy deleted", "policy_type": ptype, "identifier": identifier})


# ============================================================
# Helpers
# ============================================================

def _get_caller_email(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return claims.get("email") or claims.get("preferred_username") or ""


def _is_admin(email):
    import time
    global _admin_emails_cache, _admin_emails_cache_time
    now = time.time()
    if _admin_emails_cache is None or (now - _admin_emails_cache_time) > _ADMIN_CACHE_TTL:
        try:
            response = ssm_client.get_parameter(Name=ADMIN_EMAILS_PARAM)
            raw = response["Parameter"]["Value"]
            _admin_emails_cache = [e.strip().lower() for e in raw.split(",") if e.strip()]
            _admin_emails_cache_time = now
        except Exception as e:
            print(f"Error reading admin emails: {e}")
            if _admin_emails_cache is None:
                _admin_emails_cache = []
    return email.lower() in _admin_emails_cache


def _get_all_users_usage(month_prefix, current_date):
    users = {}
    try:
        response = quota_table.scan(
            FilterExpression=Attr("sk").eq(f"MONTH#{month_prefix}"),
            ProjectionExpression="pk, email, total_tokens, daily_tokens, daily_date, total_cost, daily_cost, #g",
            ExpressionAttributeNames={"#g": "groups"},
        )

        def _process(items):
            for item in items:
                email = item.get("email")
                if not email:
                    continue
                daily_cost = float(item.get("daily_cost", 0))
                daily_tokens = float(item.get("daily_tokens", 0))
                if item.get("daily_date") != current_date:
                    daily_cost = 0
                    daily_tokens = 0
                users[email] = {
                    "total_cost": float(item.get("total_cost", 0)),
                    "daily_cost": daily_cost,
                    "total_tokens": float(item.get("total_tokens", 0)),
                    "daily_tokens": daily_tokens,
                    "groups": item.get("groups", []),
                }

        _process(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = quota_table.scan(
                FilterExpression=Attr("sk").eq(f"MONTH#{month_prefix}"),
                ProjectionExpression="pk, email, total_tokens, daily_tokens, daily_date, total_cost, daily_cost, #g",
                ExpressionAttributeNames={"#g": "groups"},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            _process(response.get("Items", []))
    except Exception as e:
        print(f"Error fetching users: {e}")
    return users


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
                        "monthly_token_limit": int(item.get("monthly_token_limit", 0)),
                        "daily_token_limit": int(item.get("daily_token_limit", 0)) if item.get("daily_token_limit") else None,
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


def _load_user_directory():
    directory = {}
    try:
        response = directory_table.scan(
            ProjectionExpression="email, iii_livello, business_unit",
        )
        for item in response.get("Items", []):
            email = item.get("email", "").lower()
            if email:
                directory[email] = {
                    "iii_livello": item.get("iii_livello", ""),
                    "business_unit": item.get("business_unit", ""),
                }
        while "LastEvaluatedKey" in response:
            response = directory_table.scan(
                ProjectionExpression="email, iii_livello, business_unit",
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            for item in response.get("Items", []):
                email = item.get("email", "").lower()
                if email:
                    directory[email] = {
                        "iii_livello": item.get("iii_livello", ""),
                        "business_unit": item.get("business_unit", ""),
                    }
    except Exception as e:
        print(f"Error loading user directory: {e}")
    return directory


def _get_user_directory_entry(email):
    try:
        response = directory_table.get_item(Key={"email": email.lower()})
        item = response.get("Item", {})
        return {
            "iii_livello": item.get("iii_livello", ""),
            "business_unit": item.get("business_unit", ""),
            "nome_cognome": item.get("nome_cognome", ""),
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
            "Access-Control-Allow-Methods": "GET,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=_decimal_default),
    }


def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
