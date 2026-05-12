var CONFIG = {
  API_BASE: "__API_ENDPOINT__"
};

var state = {
  email: "peter.eskandar@posteitaliane.it",
  availableMonths: [],
};

// ============================================================
// Init
// ============================================================
document.addEventListener("DOMContentLoaded", function() {
  document.getElementById("banner-email").textContent = state.email;

  document.getElementById("filter-mode").addEventListener("change", toggleFilterMode);
  document.getElementById("filter-apply-btn").addEventListener("click", loadAll);

  loadAvailableMonths().then(loadAll);
});

function loadAll() {
  loadUsage();
  loadModels();
}

// ============================================================
// API
// ============================================================
function apiCall(path) {
  return fetch(CONFIG.API_BASE + path).then(function(resp) {
    if (!resp.ok) return Promise.reject(new Error("API " + resp.status));
    return resp.json();
  });
}

// ============================================================
// Filters
// ============================================================
function loadAvailableMonths() {
  return apiCall("/api/my-available-months?email=" + encodeURIComponent(state.email)).then(function(data) {
    state.availableMonths = data.months || [];
    populateSelects();
  }).catch(function() {
    var now = new Date();
    state.availableMonths = [now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0")];
    populateSelects();
  });
}

function populateSelects() {
  var ids = ["filter-month", "filter-from", "filter-to"];
  ids.forEach(function(id) {
    var sel = document.getElementById(id);
    sel.innerHTML = "";
    state.availableMonths.forEach(function(m) {
      var opt = document.createElement("option");
      opt.value = m;
      opt.textContent = formatMonth(m);
      sel.appendChild(opt);
    });
  });
  if (state.availableMonths.length > 0) {
    document.getElementById("filter-month").value = state.availableMonths[0];
  }
  if (state.availableMonths.length > 1) {
    document.getElementById("filter-from").value = state.availableMonths[state.availableMonths.length - 1];
    document.getElementById("filter-to").value = state.availableMonths[0];
  }
}

function formatMonth(m) {
  var parts = m.split("-");
  var names = ["Gen","Feb","Mar","Apr","Mag","Giu","Lug","Ago","Set","Ott","Nov","Dic"];
  return names[parseInt(parts[1]) - 1] + " " + parts[0];
}

function toggleFilterMode() {
  var mode = document.getElementById("filter-mode").value;
  var single = mode === "single";
  document.getElementById("filter-month").style.display = single ? "" : "none";
  document.getElementById("filter-from").style.display = single ? "none" : "";
  document.getElementById("filter-range-sep").style.display = single ? "none" : "";
  document.getElementById("filter-to").style.display = single ? "none" : "";
}

function getParams() {
  var mode = document.getElementById("filter-mode").value;
  var p = "email=" + encodeURIComponent(state.email) + "&";
  if (mode === "single") {
    p += "month=" + document.getElementById("filter-month").value;
  } else {
    p += "from=" + document.getElementById("filter-from").value + "&to=" + document.getElementById("filter-to").value;
  }
  return p;
}

// ============================================================
// Usage
// ============================================================
function loadUsage() {
  apiCall("/api/my-usage?" + getParams()).then(function(data) {
    // Summary cards
    document.getElementById("s-total-cost").textContent = "$" + (data.total_cost || 0).toFixed(2);
    document.getElementById("s-total-tokens").textContent = formatNum(data.total_tokens || 0);
    document.getElementById("s-daily-cost").textContent = "$" + (data.daily_cost || 0).toFixed(2);

    var pct = data.percentage || 0;
    document.getElementById("s-percentage").textContent = pct.toFixed(1) + "%";

    // Quota bar
    var fillEl = document.getElementById("quota-fill");
    fillEl.style.width = Math.min(pct, 100) + "%";
    fillEl.className = "quota-fill" + (pct > 100 ? " critical" : (pct > 80 ? " warning" : ""));
    document.getElementById("quota-limit-label").textContent = "Limite: " + (data.monthly_cost_limit ? "$" + data.monthly_cost_limit.toFixed(2) : "N/A");

    var infoText = "Enforcement: " + (data.enforcement_mode || "alert");
    if (data.daily_cost_limit) infoText += " | Limite giornaliero: $" + data.daily_cost_limit.toFixed(2);
    document.getElementById("quota-info").textContent = infoText;

    // Token breakdown chart
    renderTokenChart(data);

    // Monthly trend
    if (data.monthly_breakdown && data.monthly_breakdown.length > 1) {
      document.getElementById("trend-card").style.display = "";
      renderTrend(data.monthly_breakdown);
    } else {
      document.getElementById("trend-card").style.display = "none";
    }
  }).catch(function(e) {
    document.getElementById("s-total-cost").textContent = "Errore";
    console.error(e);
  });
}

function renderTokenChart(data) {
  var types = [
    { label: "Input", value: data.input_tokens || 0, color: "#3b82f6" },
    { label: "Output", value: data.output_tokens || 0, color: "#ef4444" },
    { label: "Cache Read", value: data.cache_read_tokens || 0, color: "#10b981" },
    { label: "Cache Write", value: data.cache_write_tokens || 0, color: "#f59e0b" },
  ];

  var max = Math.max.apply(null, types.map(function(t) { return t.value; })) || 1;
  var html = "";

  types.forEach(function(t) {
    if (t.value <= 0) return;
    var w = (t.value / max * 100);
    html += '<div class="bar-item">';
    html += '<div class="bar-label">' + t.label + '</div>';
    html += '<div class="bar-track"><div class="bar-fill" style="width:' + w + '%;background:' + t.color + '"></div></div>';
    html += '<div class="bar-value">' + formatNum(t.value) + '</div>';
    html += '</div>';
  });

  document.getElementById("token-chart").innerHTML = html || '<div class="loading">Nessun dato</div>';
}

function renderTrend(breakdown) {
  var maxCost = Math.max.apply(null, breakdown.map(function(b) { return b.cost; })) || 1;
  var html = "";

  breakdown.forEach(function(b) {
    var h = (b.cost / maxCost * 130);
    html += '<div class="trend-bar-wrap">';
    html += '<div class="trend-value">$' + b.cost.toFixed(0) + '</div>';
    html += '<div class="trend-bar" style="height:' + h + 'px"></div>';
    html += '<div class="trend-label">' + formatMonth(b.month) + '</div>';
    html += '</div>';
  });

  document.getElementById("trend-chart").innerHTML = html;
}

// ============================================================
// Models
// ============================================================
function loadModels() {
  apiCall("/api/my-models?" + getParams()).then(function(data) {
    var tbody = document.getElementById("models-tbody");
    var models = data.models || [];

    if (models.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="loading">Nessun dato</td></tr>';
      document.getElementById("model-chart").innerHTML = '<div class="loading">Nessun dato</div>';
      return;
    }

    // Table
    var html = "";
    models.forEach(function(m) {
      html += "<tr>";
      html += "<td><strong>" + shortModel(m.model) + "</strong></td>";
      html += "<td>" + formatNum(m.input) + "</td>";
      html += "<td>" + formatNum(m.output) + "</td>";
      html += "<td>" + formatNum(m.cache_read) + "</td>";
      html += "<td>" + formatNum(m.cache_write) + "</td>";
      html += "<td>$" + m.cost.toFixed(2) + "</td>";
      html += "<td>" + m.percentage.toFixed(1) + "%</td>";
      html += "</tr>";
    });
    tbody.innerHTML = html;

    // Model cost chart
    var maxCost = models[0].cost || 1;
    var colors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444", "#6366f1", "#ec4899"];
    var chartHtml = "";
    models.slice(0, 7).forEach(function(m, i) {
      var w = (m.cost / maxCost * 100);
      chartHtml += '<div class="bar-item">';
      chartHtml += '<div class="bar-label">' + shortModel(m.model) + '</div>';
      chartHtml += '<div class="bar-track"><div class="bar-fill" style="width:' + w + '%;background:' + colors[i % colors.length] + '"></div></div>';
      chartHtml += '<div class="bar-value">$' + m.cost.toFixed(2) + '</div>';
      chartHtml += '</div>';
    });
    document.getElementById("model-chart").innerHTML = chartHtml;
  }).catch(function(e) {
    document.getElementById("models-tbody").innerHTML = '<tr><td colspan="7" class="loading">Errore: ' + e.message + '</td></tr>';
  });
}

// ============================================================
// Helpers
// ============================================================
function formatNum(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "K";
  return Math.round(n).toLocaleString("it-IT");
}

function shortModel(id) {
  var parts = id.split(".");
  var last = parts[parts.length - 1] || id;
  if (last.length > 20) last = last.substring(0, 18) + "..";
  return last;
}
