// Configuration - these will be replaced at deploy time
var CONFIG = {
  API_BASE: "__API_ENDPOINT__",
  CLIENT_ID: "__AZURE_CLIENT_ID__",
  TENANT_ID: "__AZURE_TENANT_ID__"
};

var state = {
  token: null,
  email: null,
  currentTab: "overview",
  usersPage: 1,
  usersPageSize: 20,
  usersTotal: 0,
  usersSearch: ""
};

// ============================================================
// Auth
// ============================================================
function getAuthUrl() {
  var redirectUri = window.location.origin + window.location.pathname;
  var nonce = Math.random().toString(36).substring(2);
  return "https://login.microsoftonline.com/" + CONFIG.TENANT_ID +
    "/oauth2/v2.0/authorize?client_id=" + CONFIG.CLIENT_ID +
    "&response_type=id_token&scope=openid%20profile%20email" +
    "&redirect_uri=" + encodeURIComponent(redirectUri) +
    "&response_mode=fragment&nonce=" + nonce +
    "&prompt=select_account";
}

function parseJwt(token) {
  var base64Url = token.split(".")[1];
  var base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
  var jsonPayload = decodeURIComponent(atob(base64).split("").map(function(c) {
    return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
  }).join(""));
  return JSON.parse(jsonPayload);
}

function initAuth() {
  document.getElementById("login-btn").href = getAuthUrl();

  if (window.location.hash) {
    var params = new URLSearchParams(window.location.hash.substring(1));
    var idToken = params.get("id_token");
    if (idToken) {
      try {
        var claims = parseJwt(idToken);
        state.token = idToken;
        state.email = claims.email || claims.preferred_username || "";
        sessionStorage.setItem("admin_token", idToken);
        sessionStorage.setItem("admin_email", state.email);
        window.history.replaceState(null, "", window.location.pathname);
        showDashboard();
      } catch (e) {
        showError("Token parsing failed: " + e.message);
      }
    }
  } else if (sessionStorage.getItem("admin_token")) {
    state.token = sessionStorage.getItem("admin_token");
    state.email = sessionStorage.getItem("admin_email") || "";
    showDashboard();
  }
}

function showDashboard() {
  document.getElementById("login-section").style.display = "none";
  document.getElementById("dashboard-section").style.display = "block";
  document.getElementById("banner-email").textContent = state.email;
  document.getElementById("logout-btn").style.display = "flex";
  loadOverview();
}

function logout() {
  sessionStorage.clear();
  state.token = null;
  state.email = null;
  var redirectUri = window.location.origin + window.location.pathname;
  window.location.href = "https://login.microsoftonline.com/" + CONFIG.TENANT_ID + "/oauth2/v2.0/logout?post_logout_redirect_uri=" + encodeURIComponent(redirectUri);
}

function showError(msg) {
  document.getElementById("login-section").innerHTML =
    '<div class="error">' + msg + "</div>";
}

// ============================================================
// API calls
// ============================================================
function apiCall(path, method, body) {
  var opts = {
    method: method || "GET",
    headers: {
      "Authorization": "Bearer " + state.token,
      "Content-Type": "application/json"
    }
  };
  if (body) opts.body = JSON.stringify(body);

  return fetch(CONFIG.API_BASE + path, opts).then(function(resp) {
    if (resp.status === 401) {
      sessionStorage.clear();
      window.location.href = getAuthUrl();
      return Promise.reject(new Error("Unauthorized"));
    }
    if (resp.status === 403) {
      document.getElementById("dashboard-section").innerHTML =
        '<div style="text-align:center;padding:60px 20px;">' +
        '<h2 style="color:#c62828;margin-bottom:12px;">Accesso Negato</h2>' +
        '<p style="color:#5d5e61;">Il tuo account (' + state.email + ') non è autorizzato ad accedere a questa console.</p>' +
        '<p style="color:#5d5e61;margin-top:8px;">Contatta un amministratore per richiedere l\'accesso.</p></div>';
      return Promise.reject(new Error("Forbidden"));
    }
    if (!resp.ok) {
      return Promise.reject(new Error("API " + resp.status));
    }
    return resp.json();
  });
}

// ============================================================
// Tabs
// ============================================================
function initTabs() {
  var tabs = document.querySelectorAll(".tab");
  tabs.forEach(function(tab) {
    tab.addEventListener("click", function() {
      tabs.forEach(function(t) { t.classList.remove("active"); });
      tab.classList.add("active");
      document.querySelectorAll(".tab-content").forEach(function(c) { c.classList.remove("active"); });
      var target = tab.getAttribute("data-tab");
      document.getElementById("tab-" + target).classList.add("active");
      state.currentTab = target;

      if (target === "overview") loadOverview();
      else if (target === "users") loadUsers();
      else if (target === "groups") loadGroups();
      else if (target === "policies") loadPolicies();
    });
  });
}

// ============================================================
// Overview
// ============================================================
function loadOverview() {
  apiCall("/api/overview").then(function(data) {
    document.getElementById("stat-total-users").textContent = data.total_users || 0;
    document.getElementById("stat-total-cost").textContent = "$" + (data.total_cost || 0).toFixed(2);
    document.getElementById("stat-over-quota").textContent = data.over_quota || 0;
    document.getElementById("stat-blocked").textContent = data.blocked || 0;

    var html = "<h3>Top Consumers</h3>";
    if (data.top_consumers && data.top_consumers.length > 0) {
      html += '<table><thead><tr><th>Email</th><th>Costo Mese</th><th>Utilizzo</th></tr></thead><tbody>';
      data.top_consumers.forEach(function(u) {
        var pct = u.percentage || 0;
        var cls = pct > 100 ? "critical" : (pct > 80 ? "warning" : "ok");
        html += "<tr>";
        html += "<td>" + escHtml(u.email) + "</td>";
        html += "<td>$" + (u.total_cost || 0).toFixed(2) + "</td>";
        html += '<td><div class="progress-bar"><div class="progress-fill ' + cls + '" style="width:' + Math.min(pct, 100) + '%"></div></div> ' + pct.toFixed(0) + "%</td>";
        html += "</tr>";
      });
      html += "</tbody></table>";
    } else {
      html += '<div class="empty-state">Nessun dato disponibile</div>';
    }
    document.getElementById("top-consumers").innerHTML = html;
  }).catch(function(e) {
    document.getElementById("top-consumers").innerHTML = '<div class="error">Errore: ' + e.message + "</div>";
  });
}

// ============================================================
// Users
// ============================================================
function loadUsers() {
  var params = "?page=" + state.usersPage + "&page_size=" + state.usersPageSize;
  if (state.usersSearch) params += "&search=" + encodeURIComponent(state.usersSearch);

  apiCall("/api/users" + params).then(function(data) {
    state.usersTotal = data.total || 0;
    var tbody = document.getElementById("users-tbody");

    if (!data.users || data.users.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-state">Nessun utente trovato</td></tr>';
      renderPagination(0);
      return;
    }

    var html = "";
    data.users.forEach(function(u) {
      var pct = u.percentage || 0;
      var statusCls = pct > 100 ? "badge-critical" : (pct > 90 ? "badge-warning" : "badge-ok");
      var statusText = pct > 100 ? "EXCEEDED" : (pct > 90 ? "CRITICAL" : (pct > 80 ? "WARNING" : "OK"));
      var policyCls = u.policy_type === "user" ? "badge-user" : (u.policy_type === "group" ? "badge-group" : "badge-default");

      html += "<tr>";
      html += "<td>" + escHtml(u.email) + "</td>";
      html += '<td><span class="badge ' + policyCls + '">' + (u.policy_type || "default") + "</span></td>";
      html += "<td>$" + (u.total_cost || 0).toFixed(2) + "</td>";
      html += "<td>$" + (u.limit || 0).toFixed(2) + "</td>";
      html += '<td><div class="progress-bar"><div class="progress-fill ' + (pct > 100 ? "critical" : (pct > 80 ? "warning" : "ok")) + '" style="width:' + Math.min(pct, 100) + '%"></div></div> ' + pct.toFixed(0) + "%</td>";
      html += '<td><span class="badge ' + statusCls + '">' + statusText + "</span></td>";
      html += '<td><button class="detail-btn" onclick="openUserDetail(\'' + escAttr(u.email) + '\')">Dettagli</button></td>';
      html += "</tr>";
    });
    tbody.innerHTML = html;
    renderPagination(data.total);
  }).catch(function(e) {
    document.getElementById("users-tbody").innerHTML = '<tr><td colspan="7" class="error">Errore: ' + e.message + "</td></tr>";
  });
}

function renderPagination(total) {
  var totalPages = Math.ceil(total / state.usersPageSize) || 1;
  var html = '<button ' + (state.usersPage <= 1 ? "disabled" : "") + ' onclick="changePage(-1)">&laquo; Prec</button>';
  html += '<span class="page-info">Pagina ' + state.usersPage + " di " + totalPages + " (" + total + " utenti)</span>";
  html += '<button ' + (state.usersPage >= totalPages ? "disabled" : "") + ' onclick="changePage(1)">Succ &raquo;</button>';
  document.getElementById("users-pagination").innerHTML = html;
}

function changePage(delta) {
  state.usersPage += delta;
  if (state.usersPage < 1) state.usersPage = 1;
  loadUsers();
}

function searchUsers() {
  state.usersSearch = document.getElementById("user-search").value.trim();
  state.usersPage = 1;
  loadUsers();
}

// ============================================================
// User Detail
// ============================================================
function openUserDetail(email) {
  var modal = document.getElementById("user-detail-modal");
  modal.style.display = "flex";
  document.getElementById("modal-user-email").textContent = email;
  document.getElementById("modal-body").innerHTML = '<div class="loading">Caricamento...</div>';

  apiCall("/api/users/" + encodeURIComponent(email)).then(function(data) {
    var html = '';

    // Usage section
    html += '<div class="detail-section"><h4>Consumi Mese Corrente</h4>';
    html += '<div class="detail-grid">';
    html += detailItem("Costo Totale", "$" + (data.total_cost || 0).toFixed(2));
    html += detailItem("Costo Giornaliero", "$" + (data.daily_cost || 0).toFixed(2));
    html += detailItem("Token Totali", formatNumber(data.total_tokens || 0));
    html += detailItem("Token Giornalieri", formatNumber(data.daily_tokens || 0));
    html += detailItem("Input Tokens", formatNumber(data.input_tokens || 0));
    html += detailItem("Output Tokens", formatNumber(data.output_tokens || 0));
    html += detailItem("Cache Read", formatNumber(data.cache_read_tokens || 0));
    html += detailItem("Cache Write", formatNumber(data.cache_write_tokens || 0));
    html += "</div></div>";

    // Policy section
    html += '<div class="detail-section"><h4>Policy Applicata</h4>';
    html += '<div class="detail-grid">';
    html += detailItem("Tipo", data.policy_type || "default");
    html += detailItem("Identificatore", data.policy_identifier || "default");
    html += detailItem("Limite Mensile", data.monthly_cost_limit ? "$" + data.monthly_cost_limit.toFixed(2) : "N/A");
    html += detailItem("Limite Giornaliero", data.daily_cost_limit ? "$" + data.daily_cost_limit.toFixed(2) : "N/A");
    html += detailItem("Enforcement", data.enforcement_mode || "alert");
    html += detailItem("Utilizzo", (data.percentage || 0).toFixed(1) + "%");
    html += "</div></div>";

    // Groups section
    if (data.groups && data.groups.length > 0) {
      html += '<div class="detail-section"><h4>Gruppi</h4>';
      html += "<p>" + data.groups.join(", ") + "</p></div>";
    }

    // Actions
    html += '<div class="detail-section" style="margin-top:24px;">';
    html += '<button class="action-btn" onclick="openPolicyForUser(\'' + escAttr(email) + '\')">Imposta Quota Personalizzata</button>';
    html += "</div>";

    document.getElementById("modal-body").innerHTML = html;
  }).catch(function(e) {
    document.getElementById("modal-body").innerHTML = '<div class="error">Errore: ' + e.message + "</div>";
  });
}

function detailItem(label, value) {
  return '<div class="detail-item"><div class="label">' + label + '</div><div class="value">' + value + "</div></div>";
}

// ============================================================
// Groups
// ============================================================
function loadGroups() {
  apiCall("/api/groups").then(function(data) {
    var tbody = document.getElementById("groups-tbody");
    if (!data.groups || data.groups.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-state">Nessun gruppo trovato</td></tr>';
      return;
    }

    var html = "";
    data.groups.forEach(function(g) {
      html += "<tr>";
      html += "<td><strong>" + escHtml(g.name) + "</strong></td>";
      html += "<td>" + (g.user_count || 0) + "</td>";
      html += "<td>$" + (g.total_cost || 0).toFixed(2) + "</td>";
      html += "<td>" + (g.monthly_cost_limit ? "$" + g.monthly_cost_limit.toFixed(2) : "N/A") + "</td>";
      html += "<td>" + (g.enforcement_mode || "N/A") + "</td>";
      html += '<td><button class="edit-btn" onclick="editGroupPolicy(\'' + escAttr(g.name) + '\')">Modifica</button></td>';
      html += "</tr>";
    });
    tbody.innerHTML = html;
  }).catch(function(e) {
    document.getElementById("groups-tbody").innerHTML = '<tr><td colspan="6" class="error">Errore: ' + e.message + "</td></tr>";
  });
}

// ============================================================
// Policies
// ============================================================
function loadPolicies() {
  apiCall("/api/policies").then(function(data) {
    var tbody = document.getElementById("policies-tbody");
    if (!data.policies || data.policies.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-state">Nessuna policy configurata</td></tr>';
      return;
    }

    var html = "";
    data.policies.forEach(function(p) {
      var typeCls = p.policy_type === "user" ? "badge-user" : (p.policy_type === "group" ? "badge-group" : "badge-default");
      html += "<tr>";
      html += '<td><span class="badge ' + typeCls + '">' + p.policy_type + "</span></td>";
      html += "<td>" + escHtml(p.identifier) + "</td>";
      html += "<td>" + (p.monthly_cost_limit ? "$" + p.monthly_cost_limit.toFixed(2) : "-") + "</td>";
      html += "<td>" + (p.daily_cost_limit ? "$" + p.daily_cost_limit.toFixed(2) : "-") + "</td>";
      html += "<td>" + (p.enforcement_mode || "alert") + "</td>";
      html += '<td><span class="badge ' + (p.enabled ? "badge-ok" : "badge-critical") + '">' + (p.enabled ? "ON" : "OFF") + "</span></td>";
      html += "<td>";
      html += '<button class="edit-btn" onclick="editPolicy(\'' + escAttr(p.policy_type) + "', '" + escAttr(p.identifier) + '\')">Modifica</button>';
      html += '<button class="delete-btn" onclick="deletePolicy(\'' + escAttr(p.policy_type) + "', '" + escAttr(p.identifier) + '\')">Elimina</button>';
      html += "</td>";
      html += "</tr>";
    });
    tbody.innerHTML = html;
  }).catch(function(e) {
    document.getElementById("policies-tbody").innerHTML = '<tr><td colspan="7" class="error">Errore: ' + e.message + "</td></tr>";
  });
}

// ============================================================
// Policy CRUD
// ============================================================
function openPolicyModal(data) {
  var modal = document.getElementById("policy-modal");
  modal.style.display = "flex";
  document.getElementById("policy-modal-title").textContent = data ? "Modifica Policy" : "Nuova Policy";

  document.getElementById("policy-type").value = data ? data.policy_type : "user";
  document.getElementById("policy-identifier").value = data ? data.identifier : "";
  document.getElementById("policy-monthly-cost").value = data ? (data.monthly_cost_limit || "") : "";
  document.getElementById("policy-daily-cost").value = data ? (data.daily_cost_limit || "") : "";
  document.getElementById("policy-monthly-tokens").value = data ? (data.monthly_token_limit || "") : "";
  document.getElementById("policy-daily-tokens").value = data ? (data.daily_token_limit || "") : "";
  document.getElementById("policy-enforcement").value = data ? (data.enforcement_mode || "alert") : "alert";
  document.getElementById("policy-enabled").checked = data ? data.enabled !== false : true;

  if (data) {
    document.getElementById("policy-type").disabled = true;
    document.getElementById("policy-identifier").disabled = true;
  } else {
    document.getElementById("policy-type").disabled = false;
    document.getElementById("policy-identifier").disabled = false;
  }
}

function closePolicyModal() {
  document.getElementById("policy-modal").style.display = "none";
}

function openPolicyForUser(email) {
  document.getElementById("user-detail-modal").style.display = "none";
  openPolicyModal({
    policy_type: "user",
    identifier: email,
    monthly_cost_limit: null,
    daily_cost_limit: null,
    enforcement_mode: "block",
    enabled: true
  });
  document.getElementById("policy-type").disabled = true;
  document.getElementById("policy-identifier").disabled = true;
}

function editPolicy(type, identifier) {
  apiCall("/api/policies").then(function(data) {
    var policy = (data.policies || []).find(function(p) {
      return p.policy_type === type && p.identifier === identifier;
    });
    if (policy) openPolicyModal(policy);
  });
}

function editGroupPolicy(groupName) {
  apiCall("/api/policies").then(function(data) {
    var policy = (data.policies || []).find(function(p) {
      return p.policy_type === "group" && p.identifier === groupName;
    });
    openPolicyModal(policy || { policy_type: "group", identifier: groupName, enforcement_mode: "block", enabled: true });
    document.getElementById("policy-type").disabled = true;
    document.getElementById("policy-identifier").disabled = true;
  });
}

function savePolicy(e) {
  e.preventDefault();
  var type = document.getElementById("policy-type").value;
  var identifier = document.getElementById("policy-identifier").value.trim();

  if (!identifier) { alert("Identificatore obbligatorio"); return; }

  var body = {
    monthly_cost_limit: parseFloat(document.getElementById("policy-monthly-cost").value) || 0,
    daily_cost_limit: parseFloat(document.getElementById("policy-daily-cost").value) || 0,
    monthly_token_limit: parseInt(document.getElementById("policy-monthly-tokens").value) || 0,
    daily_token_limit: parseInt(document.getElementById("policy-daily-tokens").value) || 0,
    enforcement_mode: document.getElementById("policy-enforcement").value,
    enabled: document.getElementById("policy-enabled").checked
  };

  apiCall("/api/policies/" + encodeURIComponent(type) + "/" + encodeURIComponent(identifier), "PUT", body)
    .then(function() {
      closePolicyModal();
      loadPolicies();
      if (state.currentTab === "users") loadUsers();
      if (state.currentTab === "groups") loadGroups();
    })
    .catch(function(e) { alert("Errore: " + e.message); });
}

function deletePolicy(type, identifier) {
  if (!confirm("Eliminare la policy per " + type + ":" + identifier + "?")) return;

  apiCall("/api/policies/" + encodeURIComponent(type) + "/" + encodeURIComponent(identifier), "DELETE")
    .then(function() { loadPolicies(); })
    .catch(function(e) { alert("Errore: " + e.message); });
}

// ============================================================
// Helpers
// ============================================================
function escHtml(s) {
  var div = document.createElement("div");
  div.textContent = s || "";
  return div.innerHTML;
}

function escAttr(s) {
  return (s || "").replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

function formatNumber(n) {
  return Number(n).toLocaleString("it-IT");
}

// ============================================================
// Init
// ============================================================
document.addEventListener("DOMContentLoaded", function() {
  initAuth();
  initTabs();

  // Search
  document.getElementById("user-search-btn").addEventListener("click", searchUsers);
  document.getElementById("user-search").addEventListener("keypress", function(e) {
    if (e.key === "Enter") searchUsers();
  });

  // Modals close
  document.getElementById("modal-close").addEventListener("click", function() {
    document.getElementById("user-detail-modal").style.display = "none";
  });
  document.getElementById("policy-modal-close").addEventListener("click", closePolicyModal);
  document.getElementById("policy-cancel").addEventListener("click", closePolicyModal);

  // Backdrops
  document.querySelectorAll(".modal-backdrop").forEach(function(bd) {
    bd.addEventListener("click", function() {
      bd.closest(".modal").style.display = "none";
    });
  });

  // Policy form
  document.getElementById("policy-form").addEventListener("submit", savePolicy);
  document.getElementById("add-policy-btn").addEventListener("click", function() {
    openPolicyModal(null);
  });
});
