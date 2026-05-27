// Live arbitrage dashboard. Subscribes to /ws, renders 5 tables.

const t = (k) => window.I18N.t(k);

const TABS = ["divergence", "funding", "basis", "cross_perp", "cross_spot"];

const sortState = {};
const lastRows = {};

const statusEl = document.getElementById("status");
const updatedEl = document.getElementById("updated");
const errorsEl = document.getElementById("errors");
const statsEl = document.getElementById("stats");
const searchEl = document.getElementById("search");
const minVolEl = document.getElementById("min-volume");

let payload = null;
let searchTerm = "";
let minVolume = Number(minVolEl.value);

// --- tabs ---
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
  });
});

// --- sortable headers ---
TABS.forEach((tab) => {
  const section = document.getElementById(tab);
  const defaultHeader = section.querySelector("th.desc, th.asc");
  if (defaultHeader) {
    sortState[tab] = {
      key: defaultHeader.dataset.sort,
      dir: defaultHeader.classList.contains("asc") ? "asc" : "desc",
    };
  } else {
    const firstSortable = section.querySelector("th[data-sort]");
    sortState[tab] = { key: firstSortable?.dataset.sort, dir: "desc" };
  }
  section.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      const cur = sortState[tab];
      if (cur.key === key) cur.dir = cur.dir === "desc" ? "asc" : "desc";
      else { cur.key = key; cur.dir = "desc"; }
      renderTab(tab);
    });
  });
});

// --- filters ---
let searchTimer = null;
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchTerm = searchEl.value.trim().toUpperCase();
    TABS.forEach(renderTab);
  }, 120);
});
minVolEl.addEventListener("change", () => {
  minVolume = Number(minVolEl.value);
  TABS.forEach(renderTab);
});

// --- formatting (locale-aware via I18N) ---
let isOnline = false;
let lastStats = null;
let lastErrors = null;

function setStatus(online) {
  isOnline = online;
  statusEl.className = "badge " + (online ? "online" : "offline");
  statusEl.textContent = online ? t("status_connected") : t("status_disconnected");
}

function setUpdated(ms) {
  if (!ms) { updatedEl.textContent = ""; return; }
  const locale = window.I18N.getLang() === "zh" ? "zh-CN" : "en-US";
  updatedEl.textContent = `${t("updated_at")} ${new Date(ms).toLocaleTimeString(locale, { hour12: false })}`;
}

function setStats(s) {
  lastStats = s;
  if (!s) { statsEl.textContent = ""; return; }
  const fmt = window.I18N.t("stats");
  statsEl.textContent = typeof fmt === "function" ? fmt(s) : "";
}

function setErrors(errs) {
  lastErrors = errs;
  if (!errs || !Object.keys(errs).length) { errorsEl.textContent = ""; return; }
  const fmt = window.I18N.t("errors");
  errorsEl.textContent = typeof fmt === "function" ? fmt(Object.keys(errs).join(", ")) : "";
}

function fmtNum(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(2);
  if (a >= 1) return v.toFixed(4);
  if (a >= 0.0001) return v.toFixed(6);
  if (a > 0) return v.toExponential(3);
  return "0";
}

function fmtPct(v, digits = 3) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
  return `<span class="${cls}">${v >= 0 ? "+" : ""}${v.toFixed(digits)}%</span>`;
}

function fmtRate(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const pct = v * 100;
  const cls = pct > 0 ? "pos" : pct < 0 ? "neg" : "";
  return `<span class="${cls}">${pct >= 0 ? "+" : ""}${pct.toFixed(4)}%</span>`;
}

function fmtDays(v) {
  if (v === null || v === undefined) return "—";
  return v.toFixed(1) + "d";
}

function fmtVol(v) {
  if (!v) return "—";
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toFixed(0);
}

// --- sort + filter ---
function sortRows(rows, key, dir) {
  const sorted = [...rows];
  sorted.sort((a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === "number" && typeof bv === "number") {
      return dir === "desc" ? bv - av : av - bv;
    }
    return dir === "desc"
      ? String(bv ?? "").localeCompare(String(av ?? ""))
      : String(av ?? "").localeCompare(String(bv ?? ""));
  });
  return sorted;
}

function rowMatchesSearch(r) {
  if (!searchTerm) return true;
  return (r.symbol || "").toUpperCase().includes(searchTerm);
}

function rowMatchesVolume(r) {
  if (!minVolume) return true;
  if (r.volume_usd !== undefined) return r.volume_usd >= minVolume;
  if (r.min_volume_usd !== undefined) return r.min_volume_usd >= minVolume;
  return true;
}

function rowKey(tab, r) {
  switch (tab) {
    case "divergence": return `${r.symbol}|${r.long_ex}|${r.short_ex}`;
    case "funding":
    case "basis":      return `${r.exchange}|${r.symbol}`;
    default:           return `${r.symbol}|${r.cheapest_ex}|${r.richest_ex}`;
  }
}

function renderTab(tab) {
  const section = document.getElementById(tab);
  const tbody = section.querySelector("tbody");
  const data = ((payload && payload[tab]) || [])
    .filter(rowMatchesSearch).filter(rowMatchesVolume);
  const { key, dir } = sortState[tab];

  section.querySelectorAll("th[data-sort]").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === key) th.classList.add(`sort-${dir}`);
  });

  const rows = sortRows(data, key, dir);
  const prev = lastRows[tab] || {};
  const next = {};

  const body = rows.map((r) => {
    const k = rowKey(tab, r);
    next[k] = r;
    return renderRow(tab, r, prev[k]);
  }).join("");
  tbody.innerHTML = body || `<tr><td colspan="20" class="empty">${t("empty")}</td></tr>`;
  lastRows[tab] = next;
}

function cell(prev, cur, html, num = true) {
  const cls = num ? "num" : "";
  if (prev === undefined || prev === cur) return `<td class="${cls}">${html}</td>`;
  return `<td class="${cls} flash">${html}</td>`;
}

function renderRow(tab, r, prev) {
  switch (tab) {
    case "divergence":
      return `<tr>
        <td><strong>${r.symbol}</strong></td>
        <td>${r.long_ex}</td>
        ${cell(prev?.long_rate, r.long_rate, fmtRate(r.long_rate))}
        <td class="num">${r.long_interval_h}</td>
        <td>${r.short_ex}</td>
        ${cell(prev?.short_rate, r.short_rate, fmtRate(r.short_rate))}
        <td class="num">${r.short_interval_h}</td>
        ${cell(prev?.spread_per_8h_pct, r.spread_per_8h_pct, fmtPct(r.spread_per_8h_pct, 3))}
        ${cell(prev?.spread_apr_pct, r.spread_apr_pct, fmtPct(r.spread_apr_pct, 1))}
        <td class="num muted">${fmtVol(r.min_volume_usd)}</td>
      </tr>`;
    case "funding":
      return `<tr>
        <td>${r.exchange}</td>
        <td><strong>${r.symbol}</strong></td>
        ${cell(prev?.spot, r.spot, fmtNum(r.spot))}
        ${cell(prev?.perp, r.perp, fmtNum(r.perp))}
        ${cell(prev?.basis_pct, r.basis_pct, fmtPct(r.basis_pct, 3))}
        ${cell(prev?.funding_rate, r.funding_rate, fmtRate(r.funding_rate))}
        <td class="num">${r.funding_interval_hours}</td>
        ${cell(prev?.funding_apr_pct, r.funding_apr_pct, fmtPct(r.funding_apr_pct, 2))}
        <td class="num muted">${fmtVol(r.volume_usd)}</td>
      </tr>`;
    case "basis":
      return `<tr>
        <td>${r.exchange}</td>
        <td><strong>${r.symbol}</strong></td>
        ${cell(prev?.spot, r.spot, fmtNum(r.spot))}
        ${cell(prev?.future_price, r.future_price, fmtNum(r.future_price))}
        <td class="num">${fmtDays(r.days_to_expiry)}</td>
        ${cell(prev?.basis_pct, r.basis_pct, fmtPct(r.basis_pct, 3))}
        ${cell(prev?.annualized_pct, r.annualized_pct, fmtPct(r.annualized_pct, 2))}
        <td class="num muted">${fmtVol(r.volume_usd)}</td>
      </tr>`;
    case "cross_perp":
    case "cross_spot":
      return `<tr>
        <td><strong>${r.symbol}</strong></td>
        <td>${r.cheapest_ex}</td>
        ${cell(prev?.cheapest_ask, r.cheapest_ask, fmtNum(r.cheapest_ask))}
        <td>${r.richest_ex}</td>
        ${cell(prev?.richest_bid, r.richest_bid, fmtNum(r.richest_bid))}
        ${cell(prev?.spread_pct, r.spread_pct, fmtPct(r.spread_pct, 3))}
        <td class="num muted">${fmtVol(r.min_volume_usd)}</td>
      </tr>`;
    default:
      return "";
  }
}

function applyPayload(p) {
  payload = p;
  setUpdated(p.generated_at_ms);
  setStats(p.stats);
  setErrors(p.errors);
  TABS.forEach(renderTab);
}

// --- WS lifecycle ---
let ws = null;
let reconnectTimer = null;
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => setStatus(true);
  ws.onclose = () => {
    setStatus(false);
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 3000);
  };
  ws.onerror = () => setStatus(false);
  ws.onmessage = (ev) => {
    try { applyPayload(JSON.parse(ev.data)); }
    catch (e) { console.error("bad ws message", e); }
  };
}

// --- language toggle ---
document.getElementById("lang-toggle").addEventListener("click", () => {
  window.I18N.setLang(window.I18N.getLang() === "zh" ? "en" : "zh");
});

// Re-render dynamic strings + tables whenever the language flips.
window.onLanguageChange = () => {
  setStatus(isOnline);
  if (payload) setUpdated(payload.generated_at_ms);
  setStats(lastStats);
  setErrors(lastErrors);
  TABS.forEach(renderTab);
};

fetch("/api/snapshot")
  .then((r) => r.ok ? r.json() : null)
  .then((p) => { if (p && !payload) applyPayload(p); })
  .catch(() => {});

connect();
