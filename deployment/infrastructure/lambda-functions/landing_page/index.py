import json, os, boto3
from datetime import datetime, timezone

BUCKET = os.environ["BUCKET_NAME"]
EXPIRATION = int(os.environ.get("URL_EXPIRATION", "3600"))
TENANT_ID = os.environ["AZURE_TENANT_ID"]
CLIENT_ID = os.environ["AZURE_CLIENT_ID"]
s3 = boto3.client("s3")

def _login_page(api_base):
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Claude Code - Poste Italiane</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f5f5;
      min-height: 100vh;
      color: #333;
    }}
    .topbar {{
      background: #0047bb;
      height: 5px;
      width: 100%;
    }}
    .banner {{
      background: #0047bb;
      padding: 0 40px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 70px;
    }}
    .banner-right {{
      display: flex;
      align-items: center;
      gap: 12px;
      color: white;
      font-size: 0.85em;
    }}
    .banner-email {{
      opacity: 0.9;
    }}
    .logout-icon-btn {{
      display: flex;
      align-items: center;
      justify-content: center;
      width: 36px;
      height: 36px;
      background: rgba(255,255,255,0.15);
      border: 1px solid rgba(255,255,255,0.3);
      border-radius: 50%;
      color: white;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .logout-icon-btn:hover {{
      background: rgba(186,27,27,0.9);
      border-color: rgba(186,27,27,0.9);
    }}
    .banner-logo {{
      display: flex;
      align-items: center;
      gap: 14px;
    }}
    .banner-logo .logo-box {{
      background: #eedc00;
      width: 44px;
      height: 44px;
      border-radius: 4px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 900;
      font-size: 1.3em;
      color: #0047bb;
      letter-spacing: -1px;
    }}
    .banner-logo .logo-text {{
      color: white;
      font-size: 1.2em;
      font-weight: 600;
      letter-spacing: 0.3px;
    }}
    .banner-logo .logo-text span {{
      color: #eedc00;
    }}
    .main {{
      max-width: 960px;
      margin: 0 auto;
      padding: 40px 20px;
    }}
    .page-title {{
      margin-bottom: 8px;
    }}
    .page-title h1 {{
      font-size: 1.6em;
      font-weight: 700;
      color: #0047bb;
    }}
    .page-title p {{
      color: #5d5e61;
      font-size: 0.95em;
      margin-top: 4px;
    }}
    .divider {{
      height: 3px;
      background: linear-gradient(90deg, #0047bb 0%, #eedc00 100%);
      margin: 20px 0 30px 0;
      border-radius: 2px;
    }}
    .card {{
      background: #fff;
      border: 1px solid #c6c6c9;
      border-radius: 8px;
      padding: 30px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }}
    .login-card {{
      text-align: center;
      padding: 60px 30px;
    }}
    .login-card p {{
      color: #5d5e61;
      margin-bottom: 28px;
      font-size: 1em;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      text-align: left; padding: 12px 16px;
      border-bottom: 2px solid #0047bb;
      color: #0047bb; font-weight: 600; font-size: 0.82em;
      text-transform: uppercase; letter-spacing: 0.5px;
    }}
    td {{ padding: 14px 16px; border-bottom: 1px solid #e2e2e6; vertical-align: middle; color: #1a1c1e; }}
    tr:hover td {{ background: #f2f8ff; }}
    .pkg-name {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.88em; color: #1a1c1e; }}
    .platform-badge {{ display: inline-block; padding: 3px 10px; border-radius: 3px; font-size: 0.78em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; }}
    .platform-badge.windows {{ background: #0078d4; color: white; }}
    .platform-badge.linux-x64 {{ background: #e95420; color: white; }}
    .platform-badge.linux-arm64 {{ background: #6f42c1; color: white; }}
    .platform-badge.macos {{ background: #555555; color: white; }}
    .platform-badge.other {{ background: #909194; color: white; }}
    .download-btn {{
      display: inline-block; padding: 7px 18px;
      background: #0047bb;
      color: white; text-decoration: none; border-radius: 4px;
      font-weight: 600; font-size: 0.85em;
      transition: background 0.15s;
    }}
    .download-btn:hover {{ background: #00297a; }}
    .login-btn {{
      display: inline-block; padding: 13px 36px;
      background: #0047bb;
      color: white; border: none; border-radius: 4px;
      font-weight: 600; font-size: 1em; cursor: pointer;
      text-decoration: none;
      transition: background 0.15s;
    }}
    .login-btn:hover {{ background: #00297a; }}
    .footer {{
      text-align: center; margin-top: 40px; color: #909194; font-size: 0.82em;
      border-top: 1px solid #e2e2e6; padding-top: 20px;
    }}
    .os-section {{
      background: #fff;
      border: 1px solid #c6c6c9;
      border-radius: 8px;
      padding: 24px 30px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.06);
      margin-bottom: 24px;
    }}
    .os-header {{
      margin-bottom: 16px;
    }}
    .os-header .platform-badge {{
      font-size: 0.9em;
      padding: 5px 14px;
    }}
    .empty-state {{ text-align: center; padding: 40px; color: #909194; }}
    .loading {{ text-align: center; padding: 40px; color: #909194; }}
    .error {{ text-align: center; padding: 20px; color: #ba1b1b; font-weight: 500; }}
  </style>
</head>
<body>
  <div class="topbar"></div>
  <div class="banner">
    <div class="banner-logo">
      <div class="logo-box">PI</div>
      <div class="logo-text">Poste Italiane &nbsp;|&nbsp; <span>Developer Portal</span></div>
    </div>
    <div class="banner-right" id="banner-right" style="display:none;">
      <span class="banner-email" id="banner-email"></span>
      <button class="logout-icon-btn" onclick="logout()" title="Logout">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      </button>
    </div>
  </div>
  <div class="main">
    <div class="page-title">
      <h1>Claude Code con Amazon Bedrock</h1>
      <p>Portale di distribuzione pacchetti per sviluppatori</p>
    </div>
    <div class="divider"></div>
    <div id="login-section" class="card login-card">
      <p>Accedi con il tuo account aziendale per scaricare i pacchetti</p>
      <a class="login-btn" id="login-btn" href="#">Sign in with Entra ID</a>
    </div>
    <div id="content-section" style="display:none;">
      <div id="packages-card">
        <div class="loading">Caricamento pacchetti...</div>
      </div>
    </div>
    <div class="footer">
      <p id="footer-text"></p>
    </div>
  </div>
  <script>
    var clientId = "{CLIENT_ID}";
    var tenantId = "{TENANT_ID}";
    var apiBase = "{api_base}";
    var redirectUri = window.location.origin + window.location.pathname;
    var nonce = Math.random().toString(36).substring(2);

    var authUrl = "https://login.microsoftonline.com/" + tenantId +
      "/oauth2/v2.0/authorize?client_id=" + clientId +
      "&response_type=id_token&scope=openid%20profile%20email" +
      "&redirect_uri=" + encodeURIComponent(redirectUri) +
      "&response_mode=fragment&nonce=" + nonce +
      "&prompt=select_account";

    document.getElementById("login-btn").href = authUrl;

    function parseJwt(token) {{
      var base64Url = token.split('.')[1];
      var base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
      var jsonPayload = decodeURIComponent(atob(base64).split('').map(function(c) {{
        return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
      }}).join(''));
      return JSON.parse(jsonPayload);
    }}

    function loadPackages(token, email) {{
      document.getElementById("login-section").style.display = "none";
      document.getElementById("content-section").style.display = "block";
      document.getElementById("banner-right").style.display = "flex";
      document.getElementById("banner-email").textContent = email;

      fetch(apiBase + "/packages", {{
        headers: {{ "Authorization": "Bearer " + token }}
      }}).then(function(resp) {{
        if (resp.status === 401) {{
          sessionStorage.removeItem("id_token");
          sessionStorage.removeItem("user_email");
          window.location.href = authUrl;
          return;
        }}
        if (!resp.ok) throw new Error("API returned " + resp.status);
        return resp.json();
      }}).then(function(data) {{
        if (!data) return;
        renderPackages(data.packages);
      }}).catch(function(e) {{
        document.getElementById("packages-card").innerHTML =
          '<div class="error">Failed to load packages: ' + e.message + '</div>';
      }});
    }}

    function renderTable(pkgs) {{
      var html = '<table><thead><tr><th>Pacchetto</th><th>Dimensione</th><th>Aggiornato</th><th></th></tr></thead><tbody>';
      pkgs.forEach(function(p) {{
        html += '<tr>';
        html += '<td class="pkg-name">' + p.name + '</td>';
        html += '<td>' + p.size + ' MB</td>';
        html += '<td>' + p.modified + '</td>';
        html += '<td><a href="' + p.url + '" class="download-btn">Download</a></td>';
        html += '</tr>';
      }});
      html += '</tbody></table>';
      return html;
    }}

    function renderSection(title, badgeCls, pkgs) {{
      return '<div class="os-section">'
        + '<div class="os-header"><span class="platform-badge ' + badgeCls + '">' + title + '</span></div>'
        + renderTable(pkgs)
        + '</div>';
    }}

    function renderPackages(packages) {{
      if (!packages || packages.length === 0) {{
        document.getElementById("packages-card").innerHTML = '<div class="empty-state">Nessun pacchetto disponibile</div>';
        return;
      }}
      var windows = [], linux = [], macos = [], other = [];
      packages.forEach(function(p) {{
        var n = p.name.toLowerCase();
        if (n.indexOf("windows") >= 0) windows.push(p);
        else if (n.indexOf("macos") >= 0) macos.push(p);
        else if (n.indexOf("linux") >= 0) linux.push(p);
        else other.push(p);
      }});
      var html = '';
      if (windows.length > 0) html += renderSection("Windows", "windows", windows);
      if (linux.length > 0) html += renderSection("Linux", "linux-x64", linux);
      if (macos.length > 0) html += renderSection("macOS", "macos", macos);
      if (other.length > 0) html += renderSection("Other", "other", other);
      document.getElementById("packages-card").innerHTML = html;
      document.getElementById("footer-text").textContent = "I link scadono dopo " + Math.floor({EXPIRATION}/60) + " minuti";
    }}

    function logout() {{
      sessionStorage.removeItem("id_token");
      sessionStorage.removeItem("user_email");
      window.location.href = "https://login.microsoftonline.com/" + tenantId + "/oauth2/v2.0/logout?post_logout_redirect_uri=" + encodeURIComponent(redirectUri);
    }}

    // Check if we have a token in the URL fragment (redirect back from Entra ID)
    if (window.location.hash) {{
      var params = new URLSearchParams(window.location.hash.substring(1));
      var idToken = params.get("id_token");
      if (idToken) {{
        try {{
          var claims = parseJwt(idToken);
          var email = claims.email || claims.preferred_username || "";
          sessionStorage.setItem("id_token", idToken);
          sessionStorage.setItem("user_email", email);
          window.history.replaceState(null, "", window.location.pathname);
          loadPackages(idToken, email);
        }} catch (e) {{
          document.getElementById("login-section").innerHTML =
            '<div class="error">Token parsing failed: ' + e.message + '</div>';
        }}
      }}
    }} else if (sessionStorage.getItem("id_token")) {{
      // Already have a token from a previous login in this session
      loadPackages(sessionStorage.getItem("id_token"), sessionStorage.getItem("user_email") || "");
    }}
  </script>
</body>
</html>"""

def _packages_json():
    objects = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix="packages/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET, "Key": key},
                ExpiresIn=EXPIRATION,
            )
            name = key.split("/")[-1]
            size_mb = round(obj["Size"] / 1024 / 1024, 1)
            modified = obj["LastModified"].strftime("%Y-%m-%d %H:%M")
            objects.append({"name": name, "url": url, "size": size_mb, "modified": modified, "key": key})
    return sorted(objects, key=lambda x: x["key"], reverse=True)

def handler(event, context):
    path = event.get("rawPath", event.get("path", "/"))
    # Strip stage prefix if present
    if "/prod/" in path:
        path = path.split("/prod")[-1] or "/"
    elif path == "/prod":
        path = "/"

    if path == "/packages":
        packages = _packages_json()
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"packages": packages}),
        }
    else:
        # Serve login page - determine API base URL from request
        domain = event.get("requestContext", {}).get("domainName", "")
        stage = event.get("requestContext", {}).get("stage", "prod")
        api_base = f"https://{domain}/{stage}" if domain else ""
        html = _login_page(api_base)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html"},
            "body": html,
        }
