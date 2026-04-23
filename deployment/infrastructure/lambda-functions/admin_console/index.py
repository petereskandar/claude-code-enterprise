# ABOUTME: Lambda function for admin console SPA
# ABOUTME: Serves the SPA HTML and provides REST API for user/quota management

import os
import json
import base64
import boto3
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key, Attr

dynamodb = boto3.resource("dynamodb")
bedrock_client = boto3.client("bedrock")

POLICIES_TABLE = os.environ["POLICIES_TABLE"]
QUOTA_TABLE = os.environ["QUOTA_TABLE"]

policies_table = dynamodb.Table(POLICIES_TABLE)
quota_table = dynamodb.Table(QUOTA_TABLE)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def json_response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def extract_admin_email(event):
    oidc_data = event.get("headers", {}).get("x-amzn-oidc-data", "")
    if oidc_data:
        try:
            parts = oidc_data.split(".")
            if len(parts) == 3:
                payload_b64 = parts[1]
                padding = 4 - len(payload_b64) % 4
                if padding != 4:
                    payload_b64 += "=" * padding
                payload = json.loads(base64.b64decode(payload_b64))
                email = payload.get("email") or payload.get("preferred_username") or payload.get("upn")
                if email:
                    return email
        except Exception:
            pass
    return event.get("headers", {}).get("x-amzn-oidc-identity", "admin@unknown")


def lambda_handler(event, context):
    import uuid

    try:
        path = event.get("path", "/")
        method = event.get("httpMethod", "GET")

        if path.startswith("/api/"):
            return handle_api(path, method, event)

        admin_email = extract_admin_email(event)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": generate_spa(admin_email),
        }
    except Exception as e:
        import traceback

        error_id = str(uuid.uuid4())
        print(f"ERROR_ID={error_id}: {traceback.format_exc()}")
        return json_response(500, {"error": "Internal server error", "error_id": error_id})


def handle_api(path, method, event):
    if path == "/api/users" and method == "GET":
        return api_list_users(event)
    elif path == "/api/users" and method == "POST":
        return api_create_user(event)
    elif path.startswith("/api/users/") and method == "GET":
        email = _extract_email_from_path(path, "/api/users/")
        return api_get_user(email)
    elif path.startswith("/api/users/") and method == "PUT":
        email = _extract_email_from_path(path, "/api/users/")
        return api_update_user(email, event)
    elif path.startswith("/api/users/") and method == "DELETE":
        email = _extract_email_from_path(path, "/api/users/")
        return api_delete_user(email)
    return json_response(404, {"error": "Not found"})


def _extract_email_from_path(path, prefix):
    from urllib.parse import unquote

    return unquote(path[len(prefix) :].strip("/"))


# ---- API handlers ----


def api_list_users(event):
    """List all users with their quota policies and current usage."""
    qs = event.get("queryStringParameters") or {}
    filter_text = (qs.get("q") or "").lower()

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")

    policies = {}
    resp = policies_table.scan(FilterExpression=Attr("sk").eq("CURRENT") & Attr("policy_type").eq("user"))
    for item in resp.get("Items", []):
        policies[item["identifier"]] = item
    while "LastEvaluatedKey" in resp:
        resp = policies_table.scan(
            FilterExpression=Attr("sk").eq("CURRENT") & Attr("policy_type").eq("user"),
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        for item in resp.get("Items", []):
            policies[item["identifier"]] = item

    usage_map = {}
    resp = quota_table.scan(FilterExpression=Attr("sk").eq(f"MONTH#{current_month}"))
    for item in resp.get("Items", []):
        email = item.get("email")
        if email:
            usage_map[email] = item
    while "LastEvaluatedKey" in resp:
        resp = quota_table.scan(
            FilterExpression=Attr("sk").eq(f"MONTH#{current_month}"),
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        for item in resp.get("Items", []):
            email = item.get("email")
            if email:
                usage_map[email] = item

    all_emails = set(policies.keys())

    users = []
    for email in sorted(all_emails):
        policy = policies.get(email, {})
        usage = usage_map.get(email, {})
        user = {
            "email": email,
            "monthly_token_limit": int(policy.get("monthly_token_limit", 0)),
            "daily_token_limit": int(policy.get("daily_token_limit", 0)) if policy.get("daily_token_limit") else None,
            "enforcement_mode": policy.get("enforcement_mode", "alert"),
            "enabled": policy.get("enabled", True),
            "total_tokens": float(usage.get("total_tokens", 0)),
            "daily_tokens": float(usage.get("daily_tokens", 0)),
            "has_policy": email in policies,
        }
        if filter_text and filter_text not in email.lower():
            continue
        users.append(user)

    return json_response(200, {"users": users, "month": current_month})


def api_get_user(email):
    """Get single user detail: policy + usage."""
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")

    policy_resp = policies_table.get_item(Key={"pk": f"POLICY#user#{email}", "sk": "CURRENT"})
    policy = policy_resp.get("Item", {})

    usage_resp = quota_table.get_item(Key={"pk": f"USER#{email}", "sk": f"MONTH#{current_month}"})
    usage = usage_resp.get("Item", {})

    profiles = _get_user_inference_profiles(email)

    return json_response(200, {
        "email": email,
        "policy": {
            "monthly_token_limit": int(policy.get("monthly_token_limit", 0)),
            "daily_token_limit": int(policy.get("daily_token_limit", 0)) if policy.get("daily_token_limit") else None,
            "enforcement_mode": policy.get("enforcement_mode", "alert"),
            "enabled": policy.get("enabled", True),
            "warning_threshold_80": int(policy.get("warning_threshold_80", 0)),
            "warning_threshold_90": int(policy.get("warning_threshold_90", 0)),
            "exists": bool(policy),
        },
        "usage": {
            "total_tokens": float(usage.get("total_tokens", 0)),
            "daily_tokens": float(usage.get("daily_tokens", 0)),
            "input_tokens": float(usage.get("input_tokens", 0)),
            "output_tokens": float(usage.get("output_tokens", 0)),
            "estimated_cost": float(usage.get("estimated_cost", 0)),
        },
        "inference_profiles": profiles,
        "month": current_month,
    })


def api_create_user(event):
    """Create a new user quota policy."""
    body = json.loads(event.get("body") or "{}")
    email = body.get("email", "").strip()
    if not email:
        return json_response(400, {"error": "email is required"})

    monthly_limit = int(body.get("monthly_token_limit", 225000000))
    daily_limit = body.get("daily_token_limit")
    enforcement = body.get("enforcement_mode", "alert")

    now_iso = datetime.now(timezone.utc).isoformat()
    item = {
        "pk": f"POLICY#user#{email}",
        "sk": "CURRENT",
        "policy_type": "user",
        "identifier": email,
        "monthly_token_limit": monthly_limit,
        "warning_threshold_80": int(monthly_limit * 0.8),
        "warning_threshold_90": int(monthly_limit * 0.9),
        "enforcement_mode": enforcement,
        "enabled": True,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    if daily_limit is not None:
        item["daily_token_limit"] = int(daily_limit)

    try:
        policies_table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except policies_table.meta.client.exceptions.ConditionalCheckFailedException:
        return json_response(409, {"error": f"Policy already exists for {email}"})

    return json_response(201, {"message": "User policy created", "email": email})


def api_update_user(email, event):
    """Update user quota policy. Set monthly_token_limit=0 to block."""
    body = json.loads(event.get("body") or "{}")

    update_parts = ["#updated_at = :ts"]
    expr_values = {":ts": datetime.now(timezone.utc).isoformat()}
    expr_names = {"#updated_at": "updated_at"}

    if "monthly_token_limit" in body:
        ml = int(body["monthly_token_limit"])
        update_parts.append("monthly_token_limit = :ml")
        update_parts.append("warning_threshold_80 = :w80")
        update_parts.append("warning_threshold_90 = :w90")
        expr_values[":ml"] = ml
        expr_values[":w80"] = int(ml * 0.8)
        expr_values[":w90"] = int(ml * 0.9)

    if "daily_token_limit" in body:
        update_parts.append("daily_token_limit = :dl")
        expr_values[":dl"] = int(body["daily_token_limit"])

    if "enforcement_mode" in body:
        update_parts.append("enforcement_mode = :em")
        expr_values[":em"] = body["enforcement_mode"]

    if "enabled" in body:
        update_parts.append("#enabled = :en")
        expr_values[":en"] = bool(body["enabled"])
        expr_names["#enabled"] = "enabled"

    try:
        policies_table.update_item(
            Key={"pk": f"POLICY#user#{email}", "sk": "CURRENT"},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames=expr_names if expr_names else None,
            ConditionExpression="attribute_exists(pk)",
        )
    except policies_table.meta.client.exceptions.ConditionalCheckFailedException:
        return json_response(404, {"error": f"No policy found for {email}"})

    tagged_profiles = 0
    ml_val = int(body.get("monthly_token_limit", -1))
    em_val = body.get("enforcement_mode", "")
    if ml_val == 0 and em_val == "block":
        tagged_profiles = _set_user_profiles_status(email, "disabled")
    elif ml_val > 0:
        tagged_profiles = _set_user_profiles_status(email, "enabled")

    return json_response(200, {"message": "Policy updated", "email": email, "tagged_profiles": tagged_profiles})


def api_delete_user(email):
    """Delete user policy and their inference profiles."""
    policies_table.delete_item(Key={"pk": f"POLICY#user#{email}", "sk": "CURRENT"})

    deleted_profiles = 0
    try:
        paginator = bedrock_client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="APPLICATION"):
            for summary in page.get("inferenceProfileSummaries", []):
                arn = summary.get("inferenceProfileArn")
                if not arn:
                    continue
                try:
                    tags = bedrock_client.list_tags_for_resource(resourceARN=arn).get("tags", [])
                    tag_email = next((t["value"] for t in tags if t.get("key") == "user.email"), None)
                    if tag_email == email:
                        bedrock_client.delete_inference_profile(inferenceProfileIdentifier=arn)
                        deleted_profiles += 1
                except Exception as e:
                    print(f"Warning: could not process profile {arn}: {e}")
    except Exception as e:
        print(f"Error cleaning up inference profiles for {email}: {e}")

    return json_response(200, {"message": f"User {email} deleted", "deleted_profiles": deleted_profiles})


def _get_user_inference_profiles(email):
    """Get inference profile status for a user."""
    profiles = []
    try:
        paginator = bedrock_client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="APPLICATION"):
            for summary in page.get("inferenceProfileSummaries", []):
                arn = summary.get("inferenceProfileArn")
                if not arn:
                    continue
                try:
                    tags = bedrock_client.list_tags_for_resource(resourceARN=arn).get("tags", [])
                    tag_email = next((t["value"] for t in tags if t.get("key") == "user.email"), None)
                    if tag_email == email:
                        status = next((t["value"] for t in tags if t.get("key") == "status"), "unknown")
                        profiles.append({"arn": arn, "name": summary.get("inferenceProfileName", ""), "status": status})
                except Exception:
                    pass
    except Exception as e:
        print(f"Error listing profiles for {email}: {e}")
    return profiles


def _set_user_profiles_status(email, status):
    """Tag all inference profiles for a user with the given status."""
    count = 0
    try:
        paginator = bedrock_client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="APPLICATION"):
            for summary in page.get("inferenceProfileSummaries", []):
                arn = summary.get("inferenceProfileArn")
                if not arn:
                    continue
                try:
                    tags = bedrock_client.list_tags_for_resource(resourceARN=arn).get("tags", [])
                    tag_email = next((t["value"] for t in tags if t.get("key") == "user.email"), None)
                    if tag_email == email:
                        bedrock_client.tag_resource(resourceARN=arn, tags=[{"key": "status", "value": status}])
                        count += 1
                except Exception as e:
                    print(f"Warning: could not tag profile {arn}: {e}")
    except Exception as e:
        print(f"Error setting profile status for {email}: {e}")
    print(f"Tagged {count} profile(s) as {status} for {email}")
    return count


def generate_spa(admin_email):
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Admin Console</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f0f2f5; color:#333; }}
.topbar {{ background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); color:#fff; padding:16px 32px; display:flex; justify-content:space-between; align-items:center; }}
.topbar h1 {{ font-size:20px; display:flex; align-items:center; gap:8px; }}
.topbar .admin {{ font-size:14px; opacity:0.8; }}
.container {{ max-width:1100px; margin:24px auto; padding:0 16px; }}
.toolbar {{ display:flex; gap:12px; margin-bottom:20px; align-items:center; flex-wrap:wrap; }}
.toolbar input {{ flex:1; min-width:200px; padding:10px 16px; border:1px solid #d1d5db; border-radius:8px; font-size:14px; }}
.toolbar input:focus {{ outline:none; border-color:#667eea; box-shadow:0 0 0 3px rgba(102,126,234,0.15); }}
.btn {{ padding:10px 20px; border:none; border-radius:8px; font-size:14px; font-weight:600; cursor:pointer; transition:all .2s; }}
.btn-primary {{ background:linear-gradient(135deg,#667eea,#764ba2); color:#fff; }}
.btn-primary:hover {{ transform:translateY(-1px); box-shadow:0 4px 12px rgba(102,126,234,0.3); }}
.btn-danger {{ background:#ef4444; color:#fff; }}
.btn-danger:hover {{ background:#dc2626; }}
.btn-secondary {{ background:#e5e7eb; color:#374151; }}
.btn-secondary:hover {{ background:#d1d5db; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
th {{ background:#f9fafb; text-align:left; padding:12px 16px; font-size:13px; color:#6b7280; text-transform:uppercase; letter-spacing:0.5px; border-bottom:1px solid #e5e7eb; }}
td {{ padding:12px 16px; border-bottom:1px solid #f3f4f6; font-size:14px; }}
tr:hover td {{ background:#f9fafb; }}
tr {{ cursor:pointer; }}
.badge {{ display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; }}
.badge-green {{ background:#d1fae5; color:#065f46; }}
.badge-red {{ background:#fee2e2; color:#991b1b; }}
.badge-yellow {{ background:#fef3c7; color:#92400e; }}
.badge-gray {{ background:#f3f4f6; color:#6b7280; }}
.modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:100; justify-content:center; align-items:center; }}
.modal-overlay.active {{ display:flex; }}
.modal {{ background:#fff; border-radius:16px; padding:32px; width:90%; max-width:560px; max-height:90vh; overflow-y:auto; box-shadow:0 20px 60px rgba(0,0,0,0.3); }}
.modal h2 {{ margin-bottom:20px; font-size:20px; }}
.form-group {{ margin-bottom:16px; }}
.form-group label {{ display:block; font-size:13px; font-weight:600; color:#374151; margin-bottom:4px; }}
.form-group input, .form-group select {{ width:100%; padding:10px 12px; border:1px solid #d1d5db; border-radius:8px; font-size:14px; }}
.form-group input:focus, .form-group select:focus {{ outline:none; border-color:#667eea; }}
.form-actions {{ display:flex; gap:12px; justify-content:flex-end; margin-top:24px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:24px; }}
.stat-card {{ background:#fff; padding:20px; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
.stat-card .label {{ font-size:13px; color:#6b7280; }}
.stat-card .value {{ font-size:28px; font-weight:700; margin-top:4px; }}
.usage-bar {{ height:6px; background:#e5e7eb; border-radius:3px; margin-top:4px; overflow:hidden; }}
.usage-bar-fill {{ height:100%; border-radius:3px; transition:width .3s; }}
.toast {{ position:fixed; bottom:24px; right:24px; padding:12px 24px; border-radius:8px; color:#fff; font-weight:600; z-index:200; animation:slideIn .3s; }}
.toast-success {{ background:#10b981; }}
.toast-error {{ background:#ef4444; }}
@keyframes slideIn {{ from {{ transform:translateY(20px); opacity:0; }} to {{ transform:translateY(0); opacity:1; }} }}
.empty {{ text-align:center; padding:48px; color:#9ca3af; }}
.loading {{ text-align:center; padding:48px; color:#6b7280; }}
</style>
</head>
<body>
<div class="topbar">
  <h1>&#9881; Claude Code Admin Console</h1>
  <span class="admin">Signed in as {admin_email}</span>
</div>
<div class="container">
  <div class="stats" id="stats"></div>
  <div class="toolbar">
    <input type="text" id="searchInput" placeholder="Filter by email..." />
    <button class="btn btn-primary" onclick="openCreateModal()">+ New User</button>
  </div>
  <div id="userTable"><div class="loading">Loading users...</div></div>
</div>
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h2 id="modalTitle">New User</h2>
    <div class="form-group"><label for="mEmail">Email</label><input type="email" id="mEmail" placeholder="user@company.com" /></div>
    <div class="form-group"><label for="mMonthly">Monthly Token Limit</label><input type="number" id="mMonthly" value="225000000" /></div>
    <div class="form-group"><label for="mDaily">Daily Token Limit (optional)</label><input type="number" id="mDaily" placeholder="Leave empty for no daily limit" /></div>
    <div class="form-group"><label for="mEnforcement">Enforcement Mode</label><select id="mEnforcement"><option value="alert">Alert</option><option value="block">Block</option></select></div>
    <div class="form-actions">
      <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="btnDelete" style="display:none" onclick="deleteUser()">Delete User</button>
      <button class="btn btn-primary" id="btnBlock" style="display:none" onclick="blockUser()">Block (Quota 0)</button>
      <button class="btn btn-primary" id="btnSave" onclick="saveUser()">Save</button>
    </div>
  </div>
</div>
<script>
let allUsers=[],editingEmail=null;
async function api(p,o){{const r=await fetch(p,o);if(!r.ok){{const e=await r.json().catch(()=>({{}}));throw new Error(e.error||r.statusText)}}return r.json()}}
function toast(m,t){{const e=document.createElement('div');e.className='toast toast-'+t;e.textContent=m;document.body.appendChild(e);setTimeout(()=>e.remove(),3000)}}
function fmtTokens(n){{if(n>=1e9)return(n/1e9).toFixed(1)+'B';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(0)+'K';return String(n)}}
function usagePct(u,l){{if(!l)return 0;return Math.min(100,(u/l)*100)}}
function barColor(p){{if(p>=100)return'#ef4444';if(p>=90)return'#f59e0b';if(p>=80)return'#eab308';return'#10b981'}}
async function loadUsers(){{try{{const d=await api('/api/users');allUsers=d.users;renderStats();renderTable()}}catch(e){{document.getElementById('userTable').innerHTML='<div class="empty">Error: '+e.message+'</div>'}}}}
function renderStats(){{const t=allUsers.length,b=allUsers.filter(u=>!u.enabled).length,o=allUsers.filter(u=>u.monthly_token_limit>0&&u.total_tokens>=u.monthly_token_limit).length,a=allUsers.filter(u=>u.total_tokens>0).length;document.getElementById('stats').innerHTML='<div class="stat-card"><div class="label">Total Users</div><div class="value">'+t+'</div></div><div class="stat-card"><div class="label">Active This Month</div><div class="value">'+a+'</div></div><div class="stat-card"><div class="label">Over Quota</div><div class="value" style="color:#ef4444">'+o+'</div></div><div class="stat-card"><div class="label">Blocked</div><div class="value" style="color:#f59e0b">'+b+'</div></div>'}}
function renderTable(){{const q=document.getElementById('searchInput').value.toLowerCase();const f=allUsers.filter(u=>u.email.toLowerCase().includes(q));if(!f.length){{document.getElementById('userTable').innerHTML='<div class="empty">No users found</div>';return}}let h='<table><thead><tr><th>Email</th><th>Monthly Limit</th><th>Usage</th><th>Enforcement</th><th>Status</th></tr></thead><tbody>';for(const u of f){{const p=usagePct(u.total_tokens,u.monthly_token_limit),c=barColor(p),s=!u.enabled?'<span class="badge badge-red">Blocked</span>':u.monthly_token_limit>0&&u.total_tokens>=u.monthly_token_limit?'<span class="badge badge-yellow">Over Quota</span>':u.has_policy?'<span class="badge badge-green">Active</span>':'<span class="badge badge-gray">No Policy</span>';h+='<tr onclick="openEditModal(&apos;'+u.email+'&apos;)"><td>'+u.email+'</td><td>'+(u.monthly_token_limit?fmtTokens(u.monthly_token_limit):'\u2014')+'</td><td style="min-width:140px">'+fmtTokens(u.total_tokens)+(u.monthly_token_limit?' / '+fmtTokens(u.monthly_token_limit):'')+'<div class="usage-bar"><div class="usage-bar-fill" style="width:'+p+'%;background:'+c+'"></div></div></td><td>'+u.enforcement_mode+'</td><td>'+s+'</td></tr>'}}h+='</tbody></table>';document.getElementById('userTable').innerHTML=h}}
function openCreateModal(){{editingEmail=null;document.getElementById('modalTitle').textContent='New User';document.getElementById('mEmail').value='';document.getElementById('mEmail').disabled=false;document.getElementById('mMonthly').value='225000000';document.getElementById('mDaily').value='';document.getElementById('mEnforcement').value='alert';document.getElementById('btnDelete').style.display='none';document.getElementById('btnBlock').style.display='none';document.getElementById('modal').classList.add('active')}}
async function openEditModal(email){{editingEmail=email;document.getElementById('modalTitle').textContent='Edit: '+email;document.getElementById('mEmail').value=email;document.getElementById('mEmail').disabled=true;document.getElementById('btnDelete').style.display='inline-block';document.getElementById('btnBlock').style.display='inline-block';try{{const d=await api('/api/users/'+encodeURIComponent(email));document.getElementById('mMonthly').value=d.policy.monthly_token_limit||225000000;document.getElementById('mDaily').value=d.policy.daily_token_limit||'';document.getElementById('mEnforcement').value=d.policy.enforcement_mode||'alert'}}catch(e){{document.getElementById('mMonthly').value=225000000;document.getElementById('mDaily').value='';document.getElementById('mEnforcement').value='alert'}}document.getElementById('modal').classList.add('active')}}
function closeModal(){{document.getElementById('modal').classList.remove('active')}}
async function saveUser(){{const email=document.getElementById('mEmail').value.trim(),monthly=parseInt(document.getElementById('mMonthly').value)||0,dv=document.getElementById('mDaily').value.trim(),daily=dv?parseInt(dv):null,enf=document.getElementById('mEnforcement').value;if(!email){{toast('Email is required','error');return}}try{{if(editingEmail){{const b={{monthly_token_limit:monthly,enforcement_mode:enf}};if(daily!==null)b.daily_token_limit=daily;await api('/api/users/'+encodeURIComponent(email),{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});toast('User updated','success')}}else{{const b={{email,monthly_token_limit:monthly,enforcement_mode:enf}};if(daily!==null)b.daily_token_limit=daily;await api('/api/users',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});toast('User created','success')}}closeModal();loadUsers()}}catch(e){{toast(e.message,'error')}}}}
async function blockUser(){{if(!editingEmail)return;if(!confirm('Block '+editingEmail+'? This sets their quota to 0.'))return;try{{await api('/api/users/'+encodeURIComponent(editingEmail),{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{monthly_token_limit:0,enforcement_mode:'block'}})}});toast('User blocked','success');closeModal();loadUsers()}}catch(e){{toast(e.message,'error')}}}}
async function deleteUser(){{if(!editingEmail)return;if(!confirm('Delete '+editingEmail+'? This removes their policy and inference profiles.'))return;try{{await api('/api/users/'+encodeURIComponent(editingEmail),{{method:'DELETE'}});toast('User deleted','success');closeModal();loadUsers()}}catch(e){{toast(e.message,'error')}}}}
document.getElementById('searchInput').addEventListener('input',renderTable);loadUsers();
</script>
</body>
</html>'''
