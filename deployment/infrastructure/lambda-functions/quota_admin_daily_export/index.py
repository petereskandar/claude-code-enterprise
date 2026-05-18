import csv
import io
import json
import os
import boto3
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

QUOTA_TABLE     = os.environ.get("QUOTA_TABLE",     "UserQuotaMetrics")
POLICIES_TABLE  = os.environ.get("POLICIES_TABLE",  "QuotaPolicies")
DIRECTORY_TABLE = os.environ.get("DIRECTORY_TABLE", "UserDirectory")
EXPORT_BUCKET   = os.environ.get("EXPORT_BUCKET",   "")

quota_table     = dynamodb.Table(QUOTA_TABLE)
policies_table  = dynamodb.Table(POLICIES_TABLE)
directory_table = dynamodb.Table(DIRECTORY_TABLE)

CSV_HEADERS = [
    "UserID", "Email", "Nome Cognome",
    "II Livello", "III Livello", "IV Livello",
    "Profilo Utente",
    "Costo (USD)", "Limite (USD)", "Utilizzo %", "Token Totali",
]


def handler(event, context):
    today = datetime.now(timezone.utc)
    month = today.strftime("%Y-%m")
    date_str = today.strftime("%Y-%m-%d")

    current_date = today.strftime("%Y-%m-%d")

    users = _get_all_users_usage(month, current_date)
    policies = _load_all_policies()
    default_policy = policies.get("default:default")
    directory = _load_user_directory()

    rows = []
    for email, usage in users.items():
        groups = usage.get("groups", [])
        policy = _resolve_policy(email, groups, policies, default_policy)
        limit = (policy or {}).get("monthly_cost_limit", 0)
        cost = usage.get("total_cost", 0)
        pct = (cost / limit * 100) if limit > 0 else 0

        dir_entry = directory.get(email, {})
        rows.append([
            dir_entry.get("user_id", ""),
            email,
            dir_entry.get("nome_cognome", ""),
            dir_entry.get("II_livello", ""),
            dir_entry.get("III_livello", ""),
            dir_entry.get("IV_livello", ""),
            _user_profile_label(groups),
            f"{cost:.2f}",
            f"{limit:.2f}",
            f"{pct:.1f}",
            int(usage.get("total_tokens", 0)),
        ])

    rows.sort(key=lambda r: r[8], reverse=True)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    writer.writerows(rows)
    csv_content = buf.getvalue()

    s3_key = f"daily/{date_str}/usage_{date_str}.csv"
    s3_client.put_object(
        Bucket=EXPORT_BUCKET,
        Key=s3_key,
        Body=csv_content.encode("utf-8"),
        ContentType="text/csv",
        ServerSideEncryption="AES256",
    )

    print(f"Exported {len(rows)} users to s3://{EXPORT_BUCKET}/{s3_key}")
    return {"statusCode": 200, "rows": len(rows), "s3_key": s3_key}


def _user_profile_label(groups):
    g = " ".join(groups or []).lower()
    if "power"    in g: return "Power Users"
    if "standard" in g: return "Standard Users"
    if "basic"    in g: return "Basic Users"
    return ""


def _get_all_users_usage(month, current_date):
    users = {}
    try:
        response = quota_table.scan(
            FilterExpression=Attr("sk").eq(f"MONTH#{month}"),
            ProjectionExpression="pk, email, total_tokens, total_cost, #g",
            ExpressionAttributeNames={"#g": "groups"},
        )

        def _process(items):
            for item in items:
                email = item.get("email")
                if not email:
                    continue
                if email not in users:
                    users[email] = {"total_cost": 0, "total_tokens": 0, "groups": []}
                users[email]["total_cost"] += float(item.get("total_cost", 0))
                users[email]["total_tokens"] += float(item.get("total_tokens", 0))
                if not users[email]["groups"]:
                    users[email]["groups"] = item.get("groups", [])

        _process(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = quota_table.scan(
                FilterExpression=Attr("sk").eq(f"MONTH#{month}"),
                ProjectionExpression="pk, email, total_tokens, total_cost, #g",
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
                pt    = item.get("policy_type", "")
                ident = item.get("identifier", "")
                if pt and ident:
                    policies[f"{pt}:{ident}"] = {
                        "policy_type": pt,
                        "identifier": ident,
                        "monthly_cost_limit": float(item.get("monthly_cost_limit", 0)),
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
    if user_key in all_policies and all_policies[user_key].get("enabled", True):
        return all_policies[user_key]

    if groups:
        group_policies = [
            all_policies[f"group:{g}"]
            for g in groups
            if f"group:{g}" in all_policies and all_policies[f"group:{g}"].get("enabled", True)
        ]
        if group_policies:
            return min(group_policies, key=lambda p: p.get("monthly_cost_limit") or float("inf"))

    if default_policy and default_policy.get("enabled", True):
        return default_policy
    return None


def _load_user_directory():
    directory = {}
    try:
        response = directory_table.scan(
            ProjectionExpression="email, responsabile, nome_cognome, user_id, II_livello, III_livello, IV_livello",
        )
        for item in response.get("Items", []):
            email = item.get("email", "").lower()
            if email:
                directory[email] = {
                    "responsabile": item.get("responsabile", ""),
                    "nome_cognome": item.get("nome_cognome", ""),
                    "user_id":      item.get("user_id", ""),
                    "II_livello":   item.get("II_livello", ""),
                    "III_livello":  item.get("III_livello", ""),
                    "IV_livello":   item.get("IV_livello", ""),
                }
        while "LastEvaluatedKey" in response:
            response = directory_table.scan(
                ProjectionExpression="email, responsabile, nome_cognome, user_id, II_livello, III_livello, IV_livello",
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            for item in response.get("Items", []):
                email = item.get("email", "").lower()
                if email:
                    directory[email] = {
                        "responsabile": item.get("responsabile", ""),
                        "nome_cognome": item.get("nome_cognome", ""),
                        "user_id":      item.get("user_id", ""),
                        "II_livello":   item.get("II_livello", ""),
                        "III_livello":  item.get("III_livello", ""),
                        "IV_livello":   item.get("IV_livello", ""),
                    }
    except Exception as e:
        print(f"Error loading user directory: {e}")
    return directory
