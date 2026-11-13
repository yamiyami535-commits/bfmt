import { api, authHeaders, escapeHtml, formatDateTime } from '../utils.js';

// ─── State ────────────────────────────────────────────────────────────────────
let schedData        = [];     // raw rows from last fetch
let schedFiltered    = [];     // after filter/search applied
let schedPage        = 1;
const SCHED_PAGE_SIZE = 25;
let schedRefreshTimer = null;
let schedInFlight     = null;

// ─── SQL Queries (Read-only, Escaped for Backend Driver Integration) ──────────

const SQL = {

  // Summary counts — escaped string formatting structures with %%
  summary: `
SELECT
  COUNT(*)                                                        AS total,
  SUM(CompletionStatus = 'COMPLETED')                             AS completed,
  SUM(CompletionStatus = 'RUN_FAILED')                            AS failed,
  SUM(CompletionStatus = 'RUNNING')                               AS running,
  SUM(CompletionStatus = 'PENDING')                               AS pending,
  SUM(
    CompletionStatus = 'ASSIGNED'
    AND AssignedDateTime IS NOT NULL
    AND TIMESTAMPDIFF(MINUTE, STR_TO_DATE(AssignedDateTime, '%%c/%%e/%%Y %%H:%%i:%%s'), NOW()) > 10
  )                                                               AS stuck,
  ROUND(
    SUM(CompletionStatus = 'RUN_FAILED') * 100.0 / NULLIF(COUNT(*), 0), 1
  )                                                               AS fail_pct,
  ROUND(AVG(HandlingTime), 0)                                     AS avg_handling_sec
FROM bot_scheduler
WHERE ScheduleDateTime >= DATE_SUB(NOW(), INTERVAL 24 HOUR);
`,

  // Stuck tasks list — escaped format strings bounded to tracking window
  stuckTasks: `
SELECT id, ScheduleName, RunnerName, AssignedDateTime, HandlingTime
FROM bot_scheduler
WHERE CompletionStatus = 'ASSIGNED'
  AND AssignedDateTime IS NOT NULL
  AND ScheduleDateTime >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
  AND TIMESTAMPDIFF(MINUTE, STR_TO_DATE(AssignedDateTime, '%%c/%%e/%%Y %%H:%%i:%%s'), NOW()) > 10
ORDER BY STR_TO_DATE(AssignedDateTime, '%%c/%%e/%%Y %%H:%%i:%%s') ASC
LIMIT 100;
`,

  // Recent errors bounded to tracking window
  recentErrors: `
SELECT id, ScheduleName, RunnerName, CompletionRemarks, CompletionDateTime
FROM bot_scheduler
WHERE CompletionStatus = 'RUN_FAILED'
  AND ScheduleDateTime >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
ORDER BY id DESC
LIMIT 30;
`,

  // Failure rate grouped by bot name — drives Overview bar chart
  byBot: `
SELECT
  ScheduleName,
  COUNT(*)                                                          AS total,
  SUM(CompletionStatus = 'COMPLETED')                               AS completed,
  SUM(CompletionStatus = 'RUN_FAILED')                              AS failed,
  SUM(CompletionStatus = 'ASSIGNED'
      AND AssignedDateTime IS NOT NULL
      AND TIMESTAMPDIFF(MINUTE, STR_TO_DATE(AssignedDateTime, '%%c/%%e/%%Y %%H:%%i:%%s'), NOW()) > 10
  )                                                                 AS stuck,
  ROUND(SUM(CompletionStatus = 'RUN_FAILED') * 100.0 / NULLIF(COUNT(*), 0), 1) AS fail_pct
FROM bot_scheduler
WHERE ScheduleDateTime >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
GROUP BY ScheduleName
ORDER BY fail_pct DESC, total DESC
LIMIT 50;
`,

  // Top distinct failure reasons with count — drives Overview causes table
  topFailureCauses: `
SELECT
  TRIM(CompletionRemarks)  AS reason,
  ScheduleName,
  COUNT(*)                 AS cnt
FROM bot_scheduler
WHERE CompletionStatus = 'RUN_FAILED'
  AND ScheduleDateTime >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
  AND CompletionRemarks IS NOT NULL
  AND TRIM(CompletionRemarks) != ''
GROUP BY TRIM(CompletionRemarks), ScheduleName
ORDER BY cnt DESC
LIMIT 30;
`,

  // Main interactive listing configuration target profile context
  mainTable: `
SELECT 
  id,
  ScheduleName,
  BotType,
  RunnerName,
  Bot_Input,
  CompletionStatus,
  CompletionRemarks,
  ScheduleDateTime,
  StartDateTime,
  CompletionDateTime,
  HandlingTime
FROM bot_scheduler
WHERE ScheduleDateTime >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
ORDER BY id DESC
LIMIT 500;
`
};

// ─── Utility Data-Extraction Normalizer Helper ─────────────────────────────────

/**
 * Ensures any nested payload array variant (items, rows, data) 
 * gets returned consistently as a flattened list array.
 */
function extractArray(response) {
  if (!response) return [];
  if (Array.isArray(response)) return response;
  if (Array.isArray(response.items)) return response.items;
  if (Array.isArray(response.rows)) return response.rows;
  if (Array.isArray(response.data)) return response.data;
  if (typeof response === 'object') return [response]; // Fallback for raw single-row objects
  return [];
}

// ─── Core Metric Retrieval Operations Loop Engine Pipeline ────────────────────

async function loadAll() {
  if (schedInFlight) return;
  schedInFlight = true;
  setLoadingIndicator(true);

  try {
    // 1. Fetch KPI metrics row counts
    const metricsRes = await fetchSection(SQL.summary);
    const metricsData = extractArray(metricsRes);
    const m = metricsData[0] || { total: 0, completed: 0, failed: 0, running: 0, pending: 0, stuck: 0, fail_pct: 0, avg_handling_sec: 0 };
    renderHeroTiles(m);

    // 2. Fetch Stuck Tasks Queue
    const stuckRes = await fetchSection(SQL.stuckTasks);
    const stuckRows = extractArray(stuckRes);
    renderStuckPane(stuckRows, m.stuck || 0);

    // 3. Fetch Recent Failures Queue
    const errorRes = await fetchSection(SQL.recentErrors);
    const errorRows = extractArray(errorRes);
    renderErrorsPane(errorRows);

    // 4. Fetch by-bot failure rates — Overview bar chart
    const byBotRes = await fetchSection(SQL.byBot);
    const byBotRows = extractArray(byBotRes);
    renderByBotChart(byBotRows);

    // 5. Fetch top failure causes — Overview table
    const causesRes = await fetchSection(SQL.topFailureCauses);
    const causesRows = extractArray(causesRes);
    renderTopFailureCauses(causesRows);

    // 6. Fetch full queue for main table + filtering
    const mainRes = await fetchSection(SQL.mainTable);
    schedData = extractArray(mainRes);
    
    applyClientFiltering();

  } catch (err) {
    console.error("Error loading scheduler metrics sequentially:", err);
  } finally {
    schedInFlight = false;
    setLoadingIndicator(false);
  }
}

async function fetchSection(sqlQuery) {
  return await api("/api/admin/query", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ sql: sqlQuery })
  });
}

function setLoadingIndicator(show) {
  const bar = document.getElementById("loadBar");
  if (bar) bar.classList.toggle("hidden", !show);
}

// ─── Layout Interface Render Engine Handlers ───────────────────────────────────

function renderHeroTiles(m) {
  updateText('schedTileTotal', m.total ?? 0);
  updateText('schedTileCompleted', m.completed ?? 0);
  updateText('schedTileFailed', m.failed ?? 0);
  updateText('schedTileRunning', m.running ?? 0);
  updateText('schedTilePending', m.pending ?? 0);
  updateText('schedTileStuck', m.stuck ?? 0);
  updateText('schedTileFailPct', (m.fail_pct ?? 0) + "%");
  updateText('schedTileAvgTime', (m.avg_handling_sec ?? 0) + "s");

  // Sync contextual styling updates based on active alerts
  const stuckTile = document.getElementById('schedTileStuckCard');
  if (stuckTile) {
    stuckTile.style.borderColor = (m.stuck > 0) ? "var(--dt-bad)" : "";
    stuckTile.style.background = (m.stuck > 0) ? "rgba(239,68,68,0.04)" : "";
  }
}

function renderStuckPane(rows, count) {
  const badge = document.getElementById("schedStuckBadge");
  if (badge) {
    if (count > 0) {
      badge.textContent = count;
      badge.style.display = "inline-flex";
    } else {
      badge.style.display = "none";
    }
  }

  const tbody = document.getElementById("schedStuckTbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted" style="text-align:center; padding:2rem;">No stuck operational threads currently detected.</td></tr>`;
    return;
  }

  rows.forEach(r => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">#${r.id}</td>
      <td><strong>${escapeHtml(r.ScheduleName || '—')}</strong></td>
      <td class="mono">${escapeHtml(r.RunnerName || '—')}</td>
      <td class="mono">${escapeHtml(r.AssignedDateTime || '—')}</td>
      <td class="mono text-bad" style="font-weight:600">${escapeHtml(r.HandlingTime || 0)}s</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderErrorsPane(rows) {
  const tbody = document.getElementById("schedErrorsTbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted" style="text-align:center; padding:2rem;">No system or runtime errors logs caught over tracking window.</td></tr>`;
    return;
  }

  rows.forEach(r => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">#${r.id}</td>
      <td><strong>${escapeHtml(r.ScheduleName || '—')}</strong></td>
      <td class="mono">${escapeHtml(r.RunnerName || '—')}</td>
      <td class="text-bad" style="font-family:var(--font-mono); font-size:0.8rem; max-width:400px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(r.CompletionRemarks)}">
        ${escapeHtml(r.CompletionRemarks || 'Exit with unspecified failure')}
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function renderByBotChart(rows) {
  const container = document.getElementById("schedByBot");
  if (!container) return;

  if (!rows.length) {
    container.innerHTML = `<p class="muted" style="font-style:italic">No bot activity in the last 24 hours.</p>`;
    return;
  }

  const maxTotal = Math.max(...rows.map(r => Number(r.total) || 0), 1);
  container.innerHTML = "";

  rows.forEach(r => {
    const total     = Number(r.total)     || 0;
    const completed = Number(r.completed) || 0;
    const failed    = Number(r.failed)    || 0;
    const stuck     = Number(r.stuck)     || 0;
    const failPct   = Number(r.fail_pct)  || 0;

    // Bar track width is proportional to this bot's share of the busiest bot
    // Segments inside the bar represent ok / fail / stuck as % of this bot's total
    const trackW  = total ? (total / maxTotal * 100).toFixed(1) : 0;
    const okW     = total ? (completed / total * 100).toFixed(1) : 0;
    const failW   = total ? (failed    / total * 100).toFixed(1) : 0;
    const stuckW  = total ? (stuck     / total * 100).toFixed(1) : 0;
    // Remaining = PENDING / RUNNING / other — shown as muted fill
    const otherW  = Math.max(0, 100 - parseFloat(okW) - parseFloat(failW) - parseFloat(stuckW)).toFixed(1);

    const pctColor = failPct > 20 ? "var(--dt-bad)" : failPct > 5 ? "var(--dt-warn)" : "var(--dt-ok)";

    const row = document.createElement("div");
    row.className = "sched-bot-row";
    row.innerHTML = `
      <span class="sched-bot-name" title="${escapeHtml(r.ScheduleName)}">${escapeHtml(r.ScheduleName || '—')}</span>
      <div class="sched-bot-bar-track" style="position:relative">
        <div style="display:flex;height:100%;width:${trackW}%">
          <div class="sched-bot-bar-seg" style="width:${okW}%;background:var(--dt-ok)"></div>
          <div class="sched-bot-bar-seg" style="width:${failW}%;background:var(--dt-bad)"></div>
          <div class="sched-bot-bar-seg" style="width:${stuckW}%;background:var(--dt-warn)"></div>
          <div class="sched-bot-bar-seg" style="width:${otherW}%;background:var(--border);opacity:0.6"></div>
        </div>
      </div>
      <span class="sched-bot-pct" style="color:${pctColor}">${failPct}%</span>
      <span class="sched-bot-total muted">${total}</span>
    `;
    container.appendChild(row);
  });
}

function renderTopFailureCauses(rows) {
  const tbody = document.getElementById("schedFailRows");
  if (!tbody) return;
  tbody.innerHTML = "";

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted" style="text-align:center;padding:1.5rem;font-style:italic">No failures in the last 24 hours. ✓</td></tr>`;
    return;
  }

  rows.forEach(r => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-family:var(--font-mono);font-size:0.79rem;max-width:480px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${escapeHtml(r.reason)}">${escapeHtml(r.reason || '—')}</td>
      <td class="mono">${escapeHtml(r.ScheduleName || '—')}</td>
      <td style="text-align:right;font-weight:700;font-variant-numeric:tabular-nums">${escapeHtml(String(r.cnt ?? '—'))}</td>
    `;
    tbody.appendChild(tr);
  });
}

function applyClientFiltering() {
  const searchInput = document.getElementById("schedSearch");
  const statusSel = document.getElementById("schedFilterStatus");
  
  const query = searchInput ? searchInput.value.toLowerCase().trim() : "";
  const filterStatus = statusSel ? statusSel.value : "ALL";

  schedFiltered = schedData.filter(r => {
    if (filterStatus !== "ALL" && r.CompletionStatus !== filterStatus) return false;
    if (query !== "") {
      const s1 = String(r.ScheduleName || "").toLowerCase();
      const s2 = String(r.RunnerName || "").toLowerCase();
      const s3 = String(r.id || "").toLowerCase();
      if (!s1.includes(query) && !s2.includes(query) && !s3.includes(query)) return false;
    }
    return true;
  });

  schedPage = 1;
  renderMainTablePage();
}

function renderMainTablePage() {
  const tbody = document.getElementById("schedMainTbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const total = schedFiltered.length;
  const start = (schedPage - 1) * SCHED_PAGE_SIZE;
  const end = Math.min(start + SCHED_PAGE_SIZE, total);
  const pageRows = schedFiltered.slice(start, end);

  const info = document.getElementById("schedPagerInfo");
  if (info) {
    if (total === 0) {
      info.textContent = "No records matched the filter query.";
    } else {
      info.textContent = `Showing row offsets ${start + 1}–${end} of total tracked index match collection size (${total})`;
    }
  }

  renderPagerButtons(total);

  if (pageRows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted" style="text-align:center; padding:3rem;">No tracking data records discovered inside targeted parameters criteria match sets.</td></tr>`;
    return;
  }

  pageRows.forEach(r => {
    const tr = document.createElement("tr");
    
    let statusClass = "sched-badge";
    if (r.CompletionStatus === "COMPLETED") statusClass += " badge-ok";
    else if (r.CompletionStatus === "RUN_FAILED") statusClass += " badge-bad";
    else if (r.CompletionStatus === "RUNNING") statusClass += " badge-warn";
    else statusClass += " badge-neutral";

    tr.innerHTML = `
      <td class="mono">#${r.id}</td>
      <td><span class="${statusClass}">${escapeHtml(r.CompletionStatus || 'UNKNOWN')}</span></td>
      <td><strong>${escapeHtml(r.ScheduleName || '—')}</strong></td>
      <td class="mono" style="font-size:0.78rem">${escapeHtml(r.BotType || '—')}</td>
      <td class="mono">${escapeHtml(r.RunnerName || '—')}</td>
      <td class="mono muted" style="max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(r.Bot_Input)}">
        ${escapeHtml(r.Bot_Input || '—')}
      </td>
      <td class="mono">${escapeHtml(r.ScheduleDateTime || '—')}</td>
      <td class="mono" style="font-weight:600">${r.HandlingTime ? r.HandlingTime + 's' : '—'}</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderPagerButtons(totalItems) {
  const wrap = document.getElementById("schedPagerBtns");
  if (!wrap) return;
  wrap.innerHTML = "";

  const maxPages = Math.ceil(totalItems / SCHED_PAGE_SIZE);
  if (maxPages <= 1) return;

  const btnPrev = document.createElement("button");
  btnPrev.textContent = "◀ Prev";
  btnPrev.disabled = (schedPage === 1);
  btnPrev.onclick = () => { if (schedPage > 1) { schedPage--; renderMainTablePage(); } };
  wrap.appendChild(btnPrev);

  const spacer = document.createElement("span");
  spacer.textContent = ` Page ${schedPage} of ${maxPages} `;
  spacer.className = "mono font-bold";
  wrap.appendChild(spacer);

  const btnNext = document.createElement("button");
  btnNext.textContent = "Next ▶";
  btnNext.disabled = (schedPage === maxPages);
  btnNext.onclick = () => { if (schedPage < maxPages) { schedPage++; renderMainTablePage(); } };
  wrap.appendChild(btnNext);
}

function updateText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ─── Application Event Listeners Framework Hooks ─────────────────────────────

export function startSchedRefresh() {
  loadAll();
  if (!schedRefreshTimer) {
    schedRefreshTimer = setInterval(loadAll, 30000);
  }
}

export function stopSchedRefresh() {
  if (schedRefreshTimer) { 
    clearInterval(schedRefreshTimer); 
    schedRefreshTimer = null; 
  }
}

export function initSchedulerEvents() {
  const btnRefresh = document.getElementById('btnSchedRefresh');
  if (btnRefresh) btnRefresh.onclick = loadAll;

  const searchInput = document.getElementById("schedSearch");
  if (searchInput) searchInput.oninput = applyClientFiltering;

  const statusSel = document.getElementById("schedFilterStatus");
  if (statusSel) statusSel.onchange = applyClientFiltering;

  const tabBar = document.getElementById('schedTabBar');
  if (tabBar) {
    tabBar.addEventListener('click', e => {
      const tab = e.target.closest('.sched-tab');
      if (!tab) return;
      const target = tab.dataset.schedtab;
      document.querySelectorAll('.sched-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.sched-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      const pane = document.getElementById('schedPane-' + target);
      if (pane) pane.classList.add('active');
    });
  }

  document.querySelectorAll('.sched-sql-copy').forEach(btn => {
    const key = btn.dataset.sql;
    // Populate the adjacent <pre> block with the actual SQL text
    const pre = btn.closest('.card-pad')?.querySelector('.sched-sql-block');
    if (pre && SQL[key]) pre.textContent = SQL[key].trim();

    btn.onclick = () => {
      if (!SQL[key]) return;
      navigator.clipboard.writeText(SQL[key]).catch(() => {});
      const orig = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => { btn.textContent = orig; }, 1500);
    };
  });
}