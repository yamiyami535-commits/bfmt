import { api, authHeaders, escapeHtml, isSessionExpiredError } from '../utils.js';
import { initTicketInsert } from './ticketinsert.js';

const QB_COLUMNS = {
  rpa_runner:    ["RunnerName","RunnerReadableName","RunnerStatus","TaskBotStatus","QC_Status","QC_Time","ControlRoomIP"],
  bot_scheduler: ["id","RunnerName","ScheduleID","ScheduleName","BotType","Bot_Input","CompletionStatus","CompletionRemarks"],
};

// Global Execution State Matrices
let fullQueryRowsCache = [];
let currentUserRole = "user";
let pendingChanges = {};
let lastSelectedRowIndex = null;

// Tracks the table and raw SQL used by the most recent successful query,
// so that saveChangesBatch() and post-save runQuery() always operate on
// the same context regardless of whether the query came from the QB form
// or from Ticket Lookup.
let activeQueryContext = { table: null, sql: null, source: "builder" }; // source: "builder" | "ticketlookup"

// Client Pagination Tracking
let qbCurrentPage = 1;
const qbPageSize = 10;
const QB_COLUMN_WIDTHS_KEY = "qb_results_column_widths_v1";

function loadQbColumnWidths() {
  try {
    const value = JSON.parse(localStorage.getItem(QB_COLUMN_WIDTHS_KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (_) {
    return {};
  }
}

function saveQbColumnWidths(widths) {
  try { localStorage.setItem(QB_COLUMN_WIDTHS_KEY, JSON.stringify(widths)); } catch (_) {}
}

function defaultQbColumnWidth(name) {
  const col = String(name).toLowerCase();
  if (col === "bot_input") return 420;
  if (col.includes("remarks") || col.includes("message")) return 320;
  if (col === "id") return 88;
  return 190;
}

function attachQbColumnResizers(table, columns) {
  if (!table) return;
  const colElements = table.querySelectorAll("colgroup col");
  const widths = loadQbColumnWidths();
  table.querySelectorAll(".qb-column-resizer").forEach(handle => {
    handle.addEventListener("pointerdown", event => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      const colIndex = Number(handle.dataset.columnIndex);
      const colElement = colElements[colIndex];
      const columnName = columns[colIndex - 1];
      if (!colElement || !columnName) return;

      const startX = event.clientX;
      const startWidth = colElement.getBoundingClientRect().width;
      let currentWidth = startWidth;
      document.body.classList.add("qb-column-resizing");

      const onMove = moveEvent => {
        currentWidth = Math.max(96, Math.min(1400, startWidth + moveEvent.clientX - startX));
        colElement.style.width = `${currentWidth}px`;
      };
      const onStop = () => {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onStop);
        document.removeEventListener("pointercancel", onStop);
        document.body.classList.remove("qb-column-resizing");
        widths[columnName] = Math.round(currentWidth);
        saveQbColumnWidths(widths);
      };

      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onStop, { once: true });
      document.addEventListener("pointercancel", onStop, { once: true });
    });
  });
}

function resetQbColumnWidths() {
  try { localStorage.removeItem(QB_COLUMN_WIDTHS_KEY); } catch (_) {}
  renderGridPage();
}

// ─── Ticket Lookup State ───────────────────────────────────────────────────────
const TICKET_PATTERN = /\b\d{8}-\d{4,6}-\d{1,6}\b/g;
let tlIds = [];

export async function initQueryBuilder() {
  window.qbExportAllRecords = qbExportAllRecords;
  const condBox = document.getElementById("qbConditions");
  if (condBox && condBox.children.length === 0) {
    addCondition();
  }

  try {
    const userProfile = await api("/api/admin/me", { method: "GET", headers: authHeaders() });
    currentUserRole = (userProfile.role || "user").toLowerCase();

    const operationalBadge = document.getElementById("qbRoleNotice");
    if (operationalBadge) {
      if (currentUserRole === "admin") {
        operationalBadge.innerHTML = `🟢 <span style="color:#34d399;font-weight:600">Admin Clearance</span>: Full direct interactive grid CRUD editing activated.`;
      } else {
        operationalBadge.innerHTML = `🟡 <span style="color:#f59e0b;font-weight:600">User Clearance</span>: Grid operations restricted to automated Read (SELECT) and Update (UPDATE) queries.`;
      }
    }

    // Reveal admin-only nav items (Ticket Lookup, Ticket Insert)
    if (currentUserRole === "admin") {
      document.querySelectorAll(".admin-only").forEach(el => {
        el.style.display = "";
      });
    }
  } catch (err) {
    console.error("Failed security framework identification routing loops:", err);
  }

  const elements = [
    "qbTable", "qbLogic", "qbOrderBy", "qbOrderDir", "qbLimit",
    "qbGroupBy", "qbGroupToggle", "qbOrderToggle", "qbLimitToggle"
  ];

  elements.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === "checkbox" || el.tagName === "SELECT") {
      el.onchange = () => {
        if (id === "qbTable") updateColOptions();
        else updatePreview();
      };
    } else {
      el.oninput = () => updatePreview();
    }
  });

  const colsSelect = document.getElementById("qbCols");
  if (colsSelect) {
    colsSelect.onchange = () => updatePreview();
  }

  // Expose global execution vectors to window
  window.qbAddCondition    = addCondition;
  window.qbRemoveCondition = removeCondition;
  window.qbToggleValueInput = toggleValueInput;
  window.qbRunQuery        = runQuery;
  window.qbCopySQL         = copySQL;
  window.qbExportCurrent   = () => downloadCSV(fullQueryRowsCache, "full_result_set");
  window.qbResetColumnWidths = resetQbColumnWidths;
  window.qbReset           = reset;
  window.qbApplyBulkEdit   = applyBulkEdit;
  window.qbSaveChangesBatch = saveChangesBatch;
  window.qbPrevPage        = qbPrevPage;
  window.qbNextPage        = qbNextPage;

  updateColOptions();
  updatePreview();

  // ── Init Ticket Lookup (admin only — elements may not exist for non-admins) ──
  initTicketLookup();

  // ── Init Ticket Insert (admin only — elements may not exist for non-admins) ──
  initTicketInsert();
}

// ═══════════════════════════════════════════════════════════════════════════════
// TICKET LOOKUP
// ═══════════════════════════════════════════════════════════════════════════════

function initTicketLookup() {
  document.getElementById("tlExtractBtn") ?.addEventListener("click", tlExtract);
  document.getElementById("tlClearAllBtn")?.addEventListener("click", tlClearAll);
  document.getElementById("tlClearIdsBtn")?.addEventListener("click", tlClearIds);
  document.getElementById("tlCopySqlBtn") ?.addEventListener("click", tlCopySql);
  document.getElementById("tlCopyListBtn")?.addEventListener("click", tlCopyList);
  document.getElementById("tlRunBtn")     ?.addEventListener("click", tlRunInQB);
  document.getElementById("tlField")      ?.addEventListener("input",  tlRebuildSQL);

  // Enforce bot_scheduler exclusively on the UI select element dropdown
  const tlTableSelect = document.getElementById("tlTable");
  if (tlTableSelect) {
    tlTableSelect.innerHTML = '<option value="bot_scheduler">bot_scheduler</option>';
    tlTableSelect.disabled = true;
  }

  tlRebuildSQL();
}

function tlExtract() {
  const raw    = document.getElementById("tlPaste")?.value || "";
  const found  = raw.match(TICKET_PATTERN) || [];
  const unique = [...new Set(found)];
  const newIds = unique.filter(id => !tlIds.includes(id));
  tlIds = [...tlIds, ...newIds];

  const badge = document.getElementById("tlExtractBadge");
  if (badge) {
    if (unique.length === 0) {
      badge.textContent = "No ticket IDs found";
      badge.className   = "tl-badge tl-badge--warn";
    } else {
      badge.textContent = `${newIds.length} new · ${tlIds.length} total`;
      badge.className   = "tl-badge tl-badge--ok";
    }
    badge.style.display = "";
  }

  if (newIds.length > 0) {
    const paste = document.getElementById("tlPaste");
    if (paste) paste.value = "";
  }

  tlRenderChips();
  tlRebuildSQL();
}

function tlRenderChips() {
  const wrap    = document.getElementById("tlChipWrap");
  const section = document.getElementById("tlIdsSection");
  const count   = document.getElementById("tlIdCount");
  if (!wrap || !section) return;

  if (tlIds.length === 0) {
    section.style.display = "none";
    return;
  }

  section.style.display = "";
  if (count) count.textContent = tlIds.length;

  wrap.innerHTML = tlIds.map((id, i) => `
    <span class="tl-chip">
      ${id}
      <button type="button" class="tl-chip__remove" data-i="${i}" title="Remove">×</button>
    </span>
  `).join("");

  wrap.onclick = (e) => {
    const btn = e.target.closest(".tl-chip__remove");
    if (!btn) return;
    tlIds.splice(parseInt(btn.dataset.i, 10), 1);
    tlRenderChips();
    tlRebuildSQL();
    tlSyncBadge();
  };

  const listEl = document.getElementById("tlPlainList");
  if (listEl) listEl.textContent = tlIds.join("\n");
}

function tlSyncBadge() {
  const badge = document.getElementById("tlExtractBadge");
  if (!badge) return;
  if (tlIds.length === 0) { badge.style.display = "none"; return; }
  badge.textContent = `${tlIds.length} ticket ID${tlIds.length !== 1 ? "s" : ""}`;
  badge.className   = "tl-badge tl-badge--ok";
  badge.style.display = "";
}

function tlBuildSQL() {
  const table = "bot_scheduler"; 
  if (tlIds.length === 0) return "";

  // Explicit target definition using selected configuration parameters (prefixed for joining)
  const targetCols = "bs.id, bs.RunnerName, bs.ScheduleID, bs.ScheduleName, bs.BotType, bs.Bot_Input, bs.CompletionStatus, bs.CompletionRemarks";
  
  // Exact deterministic match using indexed structural parameter formatted for subquery mapping
  const formattedTicketSet = tlIds.map(id => `      '${id}'`).join(",\n");

  return `SELECT ${targetCols}
FROM ${table} bs
INNER JOIN (
    SELECT
        JSON_VALUE(Bot_Input, '$.sTicketID') AS TicketID,
        MAX(id) AS LatestID
    FROM ${table}
    WHERE JSON_VALUE(Bot_Input, '$.sTicketID') IN (
${formattedTicketSet}
    )
    GROUP BY JSON_VALUE(Bot_Input, '$.sTicketID')
) t
ON bs.id = t.LatestID`;
}

function tlRebuildSQL() {
  const box     = document.getElementById("tlSqlBox");
  const copyBtn = document.getElementById("tlCopySqlBtn");
  const runBtn  = document.getElementById("tlRunBtn");
  if (!box) return;

  if (tlIds.length === 0) {
    box.innerHTML = `<span class="tl-empty">Paste and extract ticket IDs above to generate a query.</span>`;
    if (copyBtn) copyBtn.disabled = true;
    if (runBtn)  runBtn.disabled  = true;
    return;
  }

  if (copyBtn) copyBtn.disabled = false;
  if (runBtn)  runBtn.disabled  = false;

  const sql = tlBuildSQL();
  const hl  = sql
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\b(SELECT|FROM|WHERE|IN|AND|OR|ORDER BY|LIMIT|GROUP BY|LIKE|CONCAT|INNER JOIN|ON|AS|MAX)\b/g,
      '<span class="tl-kw">$1</span>')
    .replace(/'([^']*)'/g, `<span class="tl-str">'$1'</span>`);
  box.innerHTML = hl;
}

function tlClearAll() {
  tlIds = [];
  const paste = document.getElementById("tlPaste");
  if (paste) paste.value = "";
  tlRenderChips();
  tlRebuildSQL();
  const badge = document.getElementById("tlExtractBadge");
  if (badge) badge.style.display = "none";
}

function tlClearIds() {
  tlIds = [];
  tlRenderChips();
  tlRebuildSQL();
  tlSyncBadge();
}

function tlCopySql() {
  if (tlIds.length === 0) return;
  navigator.clipboard.writeText(tlBuildSQL()).catch(() => {});
  const btn = document.getElementById("tlCopySqlBtn");
  if (!btn) return;
  const orig = btn.textContent;
  btn.textContent = "Copied!";
  setTimeout(() => { btn.textContent = orig; }, 1600);
}

function tlCopyList() {
  navigator.clipboard.writeText(tlIds.join("\n")).catch(() => {});
  const btn = document.getElementById("tlCopyListBtn");
  if (!btn) return;
  const orig = btn.textContent;
  btn.textContent = "Copied!";
  setTimeout(() => { btn.textContent = orig; }, 1600);
}

function tlRunInQB() {
  if (tlIds.length === 0) return;
  const sql     = tlBuildSQL();
  const preview = document.getElementById("qbSqlPreview");
  if (preview) preview.innerText = sql;

  // Mark that the next query execution originates from Ticket Lookup so
  // saveChangesBatch() and post-save refresh use the correct table/SQL.
  activeQueryContext = { table: "bot_scheduler", sql, source: "ticketlookup" };

  // Switch to the Query Builder inner tab (we're already in the QB view)
  document.querySelectorAll(".qb-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".qb-pane").forEach(p => p.classList.remove("active"));
  const builderTab  = document.querySelector('.qb-tab[data-qbtab="builder"]');
  const builderPane = document.getElementById("qbPane-builder");
  if (builderTab)  builderTab.classList.add("active");
  if (builderPane) builderPane.classList.add("active");

  // Give the DOM a tick then fire the query
  requestAnimationFrame(() => {
    if (typeof window.qbRunQuery === "function") window.qbRunQuery();
  });
}

// ─── Query Builder ─────────────────────────────────────────────────────────────

function updateColOptions() {
  const tbl  = document.getElementById("qbTable").value;
  const cols = QB_COLUMNS[tbl] || [];

  const sel = document.getElementById("qbCols");
  if (sel) sel.innerHTML = cols.map(c => `<option selected>${escapeHtml(c)}</option>`).join("");

  const ord = document.getElementById("qbOrderBy");
  if (ord) ord.innerHTML = cols.map(c => `<option>${escapeHtml(c)}</option>`).join("");

  const grp = document.getElementById("qbGroupBy");
  if (grp) grp.innerHTML = cols.map(c => `<option>${escapeHtml(c)}</option>`).join("");

  updateConditionFieldOptions();
  updatePreview();
}

function updateConditionFieldOptions() {
  const tbl  = document.getElementById("qbTable").value;
  const cols = QB_COLUMNS[tbl] || [];
  document.querySelectorAll(".qb-cond-field").forEach(sel => {
    const cur = sel.value;
    sel.innerHTML = cols.map(c => `<option${c === cur ? " selected" : ""}>${escapeHtml(c)}</option>`).join("");
  });
}

function getSelectedCols() {
  const selectEl = document.getElementById("qbCols");
  if (!selectEl) return [];
  return Array.from(selectEl.selectedOptions).map(o => o.value);
}

function addCondition() {
  const tbl   = document.getElementById("qbTable").value;
  const cols  = QB_COLUMNS[tbl] || [];
  const logic = document.getElementById("qbLogic").value;
  const isFirst = document.getElementById("qbConditions").children.length === 0;

  const div = document.createElement("div");
  div.className = "qb-cond-row";
  div.style.cssText = "display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap";

  const badge = isFirst ? "WHERE" : logic;
  div.innerHTML = `
    <span style="font-size:0.72rem;font-weight:700;background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:2px 8px;color:var(--muted);min-width:40px;text-align:center">${badge}</span>
    <select class="qb-cond-field" onchange="window.qbUpdatePreview ? window.qbUpdatePreview() : null" style="padding:0.35rem 0.55rem;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:inherit;font-size:0.82rem">
      ${cols.map(c => `<option>${escapeHtml(c)}</option>`).join("")}
    </select>
    <select class="qb-cond-op" onchange="qbToggleValueInput(this);window.qbUpdatePreview ? window.qbUpdatePreview() : null" style="padding:0.35rem 0.55rem;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:inherit;font-size:0.82rem">
      <option>=</option><option>!=</option><option>LIKE</option><option>&gt;</option><option>&lt;</option><option>IS NULL</option><option>IS NOT NULL</option>
    </select>
    <input type="text" class="qb-cond-val" value="" placeholder="value" oninput="window.qbUpdatePreview ? window.qbUpdatePreview() : null" style="padding:0.35rem 0.55rem;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:inherit;font-size:0.82rem;width:130px" />
    <button type="button" onclick="qbRemoveCondition(this)" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:1rem;padding:0 0.3rem;line-height:1" title="Remove">✕</button>
  `;
  document.getElementById("qbConditions").appendChild(div);
  updatePreview();
}

function toggleValueInput(opSel) {
  const row      = opSel.closest(".qb-cond-row");
  const valInput = row.querySelector(".qb-cond-val");
  const noVal    = opSel.value === "IS NULL" || opSel.value === "IS NOT NULL";
  valInput.style.display = noVal ? "none" : "";
}

function removeCondition(btn) {
  btn.closest(".qb-cond-row").remove();
  updatePreview();
}

function buildSQL() {
  const tbl       = document.getElementById("qbTable").value;
  const cols      = getSelectedCols();
  let colStr      = cols.length ? cols.join(", ") : "*";
  const logic     = document.getElementById("qbLogic").value;
  const isGrouped = document.getElementById("qbGroupToggle")?.checked;
  const groupField = document.getElementById("qbGroupBy").value;
  const isOrdered = document.getElementById("qbOrderToggle")?.checked;
  const ord       = document.getElementById("qbOrderBy").value;
  const dir       = document.getElementById("qbOrderDir").value;
  const isLimited = document.getElementById("qbLimitToggle")?.checked;
  let lim         = parseInt(document.getElementById("qbLimit").value, 10) || 50;

  if (isGrouped || !isLimited || lim > 5000) lim = 5000;
  if (isGrouped && cols.length > 0 && !cols.includes(groupField)) {
    colStr = `${groupField}, ${colStr}`;
  }

  const conds = Array.from(document.querySelectorAll(".qb-cond-row")).map(row => {
    let field = row.querySelector(".qb-cond-field")?.value || "";
    const op    = row.querySelector(".qb-cond-op")?.value || "=";
    const val   = row.querySelector(".qb-cond-val")?.value || "";

    if (tbl === "bot_scheduler" && field === "sTicketID") {
      field = "JSON_VALUE(Bot_Input, '$.sTicketID')";
    }

    if (op === "IS NULL" || op === "IS NOT NULL") return `${field} ${op}`;
    if (op === "LIKE") {
      return `${field} LIKE CONCAT(CHAR(37), '${val.replace(/'/g, "''")}', CHAR(37))`;
    }
    return `${field} ${op} '${val}'`;
  });

  let sql = `SELECT ${colStr}\nFROM ${tbl}`;
  if (conds.length) sql += `\nWHERE ${conds.join(`\n  ${logic} `)}`;
  if (isGrouped && groupField) sql += `\nGROUP BY ${groupField}`;
  if (isOrdered && ord) sql += `\nORDER BY ${ord} ${dir}`;
  sql += `\nLIMIT ${lim};`;
  return sql;
}

function updatePreview() {
  const pre = document.getElementById("qbSqlPreview");
  if (!pre) return;
  const sql      = buildSQL();
  const keywords = ["SELECT","FROM","WHERE","GROUP BY","ORDER BY","LIMIT","AND","OR","ASC","DESC","IS NULL","IS NOT NULL","LIKE","CONCAT","CHAR","IN","INNER JOIN","ON","AS","MAX"];
  let html = escapeHtml(sql);
  keywords.forEach(k => {
    html = html.replace(new RegExp("\\b" + k + "\\b", "g"), `<span style="color:#60a5fa;font-weight:600">${k}</span>`);
  });
  html = html.replace(/'([^']*)'/g, `<span style="color:#34d399">'$1'</span>`);
  pre.innerHTML = html;
}
window.qbUpdatePreview = updatePreview;

async function runQuery() {
  const btn  = document.getElementById("btnQbRun");
  btn.disabled = true;
  btn.textContent = "Running…";

  const card                 = document.getElementById("qbResultsCard");
  const msg                  = document.getElementById("qbResultsMsg");
  const title                = document.getElementById("qbResultsTitle");
  const interactiveGridToolbar = document.getElementById("qbInteractiveGridToolbar");
  const gridContainer        = document.getElementById("qbResultsContainer");

  card.style.display = "";
  if (interactiveGridToolbar) interactiveGridToolbar.style.display = "none";
  msg.textContent = "Sending query…";
  if (gridContainer) gridContainer.innerHTML = "";

  fullQueryRowsCache = [];
  pendingChanges     = {};
  lastSelectedRowIndex = null;
  qbCurrentPage = 1;

  try {
    // If an active TL context exists, use its stored SQL directly so the
    // refresh after save re-fetches the same TL result set, not the QB form SQL.
    let sql;
    if (activeQueryContext.source === "ticketlookup" && activeQueryContext.sql) {
      sql = activeQueryContext.sql;
    } else {
      const previewEl = document.getElementById("qbSqlPreview");
      sql = (previewEl && previewEl.getAttribute("contenteditable") === "true")
        ? previewEl.innerText.trim()
        : buildSQL();
    }

    const result = await api("/api/admin/query", {
      method:  "POST",
      headers: authHeaders(),
      body:    JSON.stringify({ sql }),
    });

    const rows     = result.rows || result.items || result.data || result || [];
    const isSelect = result.is_select !== false;

    fullQueryRowsCache = JSON.parse(JSON.stringify(rows));

    // Stamp the context so saveChangesBatch always knows which table to UPDATE.
    if (activeQueryContext.source !== "ticketlookup") {
      activeQueryContext = {
        table:  document.getElementById("qbTable").value,
        sql,
        source: "builder",
      };
    }

    if (isSelect) {
      title.textContent = `Interactive Results Grid — ${rows.length} total row(s) matched`;
      if (rows.length > 0) {
        if (interactiveGridToolbar) interactiveGridToolbar.style.display = "flex";
        const sampleCols = result.columns || Object.keys(rows[0]);
        populateBulkFieldDropdown(sampleCols);
        renderGridPage();
      } else {
        msg.textContent = "Query completed. 0 results returned matching conditions.";
      }
    } else {
      title.textContent = "Transaction Completed Successfully";
      msg.innerHTML = `Mutation complete. ${result.affected_rows || 0} row(s) altered.`;
      if (gridContainer) {
        gridContainer.innerHTML = `<div style="padding:2rem;text-align:center;color:#34d399;font-weight:600">Database statement processed successfully.</div>`;
      }
    }

    card.scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    title.textContent = "Query error";
    msg.textContent   = String(e.message || e);
  } finally {
    btn.disabled    = false;
    btn.textContent = "▶ Run query";
  }
}

function renderGridPage() {
  const gridContainer = document.getElementById("qbResultsContainer");
  if (!gridContainer || fullQueryRowsCache.length === 0) return;

  const cols       = Object.keys(fullQueryRowsCache[0]);
  const totalRows  = fullQueryRowsCache.length;
  const totalPages = Math.ceil(totalRows / qbPageSize) || 1;
  if (qbCurrentPage > totalPages) qbCurrentPage = totalPages;
  if (qbCurrentPage < 1)         qbCurrentPage = 1;

  const startIdx = (qbCurrentPage - 1) * qbPageSize;
  const endIdx   = Math.min(startIdx + qbPageSize, totalRows);
  const pageRows = fullQueryRowsCache.slice(startIdx, endIdx);

  const pageDisplay = document.getElementById("qbPageDisplay");
  if (pageDisplay) {
    pageDisplay.textContent = `Rows ${startIdx + 1}-${endIdx} of ${totalRows} (Page ${qbCurrentPage}/${totalPages})`;
  }

  const savedWidths = loadQbColumnWidths();
  let tableHtml = `
    <table class="qb-results-table">
      <colgroup>
        <col style="width:46px">
        ${cols.map(c => {
          const width = Math.max(96, Math.min(1400, Number(savedWidths[c]) || defaultQbColumnWidth(c)));
          return `<col data-qb-column="${escapeHtml(c)}" style="width:${width}px">`;
        }).join("")}
      </colgroup>
      <thead>
        <tr>
          <th class="qb-select-column">
            <input type="checkbox" id="qbSelectAllRows" onchange="window.toggleSelectAllRows(this)" />
          </th>
          ${cols.map((c, i) => `<th class="qb-results-th"><span>${escapeHtml(c)}</span><span class="qb-column-resizer" role="separator" aria-orientation="vertical" aria-label="Resize ${escapeHtml(c)} column" data-column-index="${i + 1}" title="Drag to resize"></span></th>`).join("")}
        </tr>
      </thead>
      <tbody>
  `;

  pageRows.forEach((row, localIdx) => {
    const globalIdx  = startIdx + localIdx;
    const isRowDirty = pendingChanges[globalIdx] !== undefined;
    const rowStyles  = isRowDirty
      ? `background:rgba(245,158,11,0.03);border-bottom:1px solid var(--border);`
      : `border-bottom:1px solid var(--border);`;

    tableHtml += `
      <tr class="qb-grid-row" data-index="${globalIdx}" style="${rowStyles}" onclick="window.handleRowRowClick(${globalIdx}, event)">
        <td style="padding:0.45rem;text-align:center;" onclick="event.stopPropagation();">
          <input type="checkbox" class="qb-row-select-checkbox" data-index="${globalIdx}" onchange="window.handleRowCheckboxChange(${globalIdx}, event)" />
        </td>
    `;

    cols.forEach(c => {
      const isCellDirty  = pendingChanges[globalIdx] && pendingChanges[globalIdx][c] !== undefined;
      const displayValue = isCellDirty ? pendingChanges[globalIdx][c] : (row[c] ?? "");
      const isImmutableKey = (c.toLowerCase() === "id");

      if (isImmutableKey) {
        tableHtml += `<td style="padding:0.45rem 0.75rem;font-size:0.82rem;color:var(--muted);background:var(--surface1);white-space:nowrap">${escapeHtml(String(displayValue || "—"))}</td>`;
      } else {
        const inputStyles = isCellDirty
          ? `background:rgba(245,158,11,0.08);border:1px solid #f59e0b;color:inherit;`
          : `background:transparent;border:1px solid transparent;color:inherit;transition:all 0.15s;`;

        tableHtml += `
          <td class="qb-results-cell">
            <textarea rows="2"
              class="qb-cell-input ${isCellDirty ? "dirty" : ""}"
              data-row="${globalIdx}"
              data-col="${escapeHtml(c)}"
              oninput="window.handleCellInput(this)"
              style="${inputStyles}"
              onfocus="this.style.background='var(--surface2)';this.style.borderColor='var(--border)';"
              onblur="if(!this.classList.contains('dirty')){this.style.background='transparent';this.style.borderColor='transparent';}"
            >${escapeHtml(String(displayValue))}</textarea>
          </td>`;
      }
    });

    tableHtml += `</tr>`;
  });

  tableHtml += `</tbody></table>`;
  gridContainer.innerHTML = tableHtml;
  attachQbColumnResizers(gridContainer.querySelector(".qb-results-table"), cols);

  window.toggleSelectAllRows = (master) => {
    document.querySelectorAll(".qb-row-select-checkbox").forEach(cb => {
      cb.checked = master.checked;
      const rElement = cb.closest(".qb-grid-row");
      if (rElement) rElement.style.background = master.checked ? "rgba(96,165,250,0.15)" : "";
    });
  };

  window.handleRowCheckboxChange = (idx, e) => {
    const rowEl = document.querySelector(`.qb-grid-row[data-index="${idx}"]`);
    if (rowEl) rowEl.style.background = e.target.checked ? "rgba(96,165,250,0.15)" : "";
  };

  window.handleRowRowClick = (index, event) => {
    if (event.target.type === "checkbox" || ["INPUT", "TEXTAREA", "BUTTON"].includes(event.target.tagName)) return;
    const checkboxes = Array.from(document.querySelectorAll(".qb-row-select-checkbox"));
    const targetCb   = checkboxes.find(c => parseInt(c.dataset.index, 10) === index);
    if (!targetCb) return;

    if (event.shiftKey && lastSelectedRowIndex !== null) {
      const start = Math.min(index, lastSelectedRowIndex);
      const end   = Math.max(index, lastSelectedRowIndex);
      for (let i = start; i <= end; i++) {
        const cb = checkboxes.find(c => parseInt(c.dataset.index, 10) === i);
        if (cb) {
          cb.checked = true;
          const rowEl = cb.closest(".qb-grid-row");
          if (rowEl) rowEl.style.background = "rgba(96,165,250,0.15)";
        }
      }
    } else {
      targetCb.checked = !targetCb.checked;
      const rowEl = targetCb.checked ? "rgba(96,165,250,0.15)" : "";
      if (rowEl) rowEl.style.background = targetCb.checked ? "rgba(96,165,250,0.15)" : "";
      lastSelectedRowIndex = index;
    }
  };
}

function qbPrevPage() {
  if (qbCurrentPage > 1) { qbCurrentPage--; renderGridPage(); }
}

function qbNextPage() {
  const totalPages = Math.ceil(fullQueryRowsCache.length / qbPageSize);
  if (qbCurrentPage < totalPages) { qbCurrentPage++; renderGridPage(); }
}

function handleCellInput(inputEl) {
  const rowIdx = parseInt(inputEl.dataset.row, 10);
  const colName = inputEl.dataset.col;
  const val     = inputEl.value;

  inputEl.classList.add("dirty");
  inputEl.style.border     = "1px solid #f59e0b";
  inputEl.style.background = "rgba(245,158,11,0.05)";

  if (!pendingChanges[rowIdx]) pendingChanges[rowIdx] = {};
  pendingChanges[rowIdx][colName] = val;

  document.getElementById("btnQbSaveGrid").disabled = false;
}
window.handleCellInput = handleCellInput;

function populateBulkFieldDropdown(cols) {
  const dropdown = document.getElementById("qbBulkColumnSelect");
  if (!dropdown) return;
  const editableCols = cols.filter(c => c.toLowerCase() !== "id");
  dropdown.innerHTML = editableCols.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
}

function applyBulkEdit() {
  const colTarget           = document.getElementById("qbBulkColumnSelect").value;
  const bulkVal             = document.getElementById("qbBulkValueInput").value;
  const selectedCheckboxes  = Array.from(document.querySelectorAll(".qb-row-select-checkbox:checked"));

  if (selectedCheckboxes.length === 0) {
    alert("Please select or shift-highlight at least one row checkbox to apply bulk modifications.");
    return;
  }

  selectedCheckboxes.forEach(cb => {
    const rowIdx = parseInt(cb.dataset.index, 10);
    if (!pendingChanges[rowIdx]) pendingChanges[rowIdx] = {};
    pendingChanges[rowIdx][colTarget] = bulkVal;
  });

  document.getElementById("qbBulkValueInput").value = "";
  document.getElementById("btnQbSaveGrid").disabled  = false;
  renderGridPage();
}

async function saveChangesBatch() {
  const saveBtn  = document.getElementById("btnQbSaveGrid");
  const origText = "💾 Save Changes to Database";

  if (!fullQueryRowsCache || fullQueryRowsCache.length === 0 || Object.keys(pendingChanges).length === 0) {
    alert("No modifications or valid data rows found to submit.");
    saveBtn.disabled    = true;
    saveBtn.textContent = origText;
    pendingChanges = {};
    return;
  }

  saveBtn.disabled    = true;
  saveBtn.textContent = "Saving batch…";

  const tableName  = activeQueryContext.table || document.getElementById("qbTable").value;
  const statements = [];

  for (const [rIdx, changes] of Object.entries(pendingChanges)) {
    const originalRowData = fullQueryRowsCache[parseInt(rIdx, 10)];
    if (!originalRowData) continue;

    let pkField = "id";
    let pkValue = originalRowData["id"];

    if (pkValue === undefined && originalRowData["RunnerName"] !== undefined) {
      pkField = "RunnerName";
      pkValue = originalRowData["RunnerName"];
    }

    if (pkValue === undefined) {
      alert("Error: Cannot safely update. Please make sure 'id' or 'RunnerName' is checked in your Columns list.");
      saveBtn.disabled    = false;
      saveBtn.textContent = origText;
      return;
    }

    const setClauses = Object.entries(changes).map(([col, val]) => {
      return `${col} = '${val.replace(/'/g, "''")}'`;
    });

    if (setClauses.length > 0) {
      statements.push(`UPDATE ${tableName} SET ${setClauses.join(", ")} WHERE ${pkField} = '${String(pkValue).replace(/'/g, "''")}';`);
    }
  }

  if (statements.length === 0) {
    alert("No pending changes found to submit.");
    saveBtn.disabled    = true;
    saveBtn.textContent = origText;
    pendingChanges = {};
    return;
  }

  try {
    let successCount = 0;
    for (const sql of statements) {
      await api("/api/admin/query", {
        method:  "POST",
        headers: authHeaders(),
        body:    JSON.stringify({ sql }),
      });
      successCount++;
    }
    alert(`Batch complete! Successfully updated ${successCount} specific rows.`);
    pendingChanges      = {};
    saveBtn.disabled    = true;
    saveBtn.textContent = origText;
    await runQuery();
  } catch (err) {
    if (!isSessionExpiredError(err)) alert("Update error: " + String(err.message || err));
    saveBtn.disabled    = false;
    saveBtn.textContent = origText;
  }
}

function downloadCSV(rowsArray, fileLabel) {
  if (!rowsArray || rowsArray.length === 0) return;
  const headers = Object.keys(rowsArray[0]);
  const csvRows = [headers.join(",")];
  for (const row of rowsArray) {
    const values = headers.map(header => {
      const val = row[header] ?? "";
      return `"${String(val).replace(/"/g, '""')}"`;
    });
    csvRows.push(values.join(","));
  }
  const encodedUri = encodeURI("data:text/csv;charset=utf-8," + csvRows.join("\n"));
  const link       = document.createElement("a");
  link.setAttribute("href", encodedUri);
  link.setAttribute("download", `bot_farm_${fileLabel}_export_${Date.now()}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

function copySQL(btn) {
  navigator.clipboard.writeText(buildSQL()).catch(() => {});
  btn.textContent = "Copied!";
  setTimeout(() => { btn.textContent = "Copy SQL"; }, 1500);
}

function reset() {
  document.getElementById("qbTable").value      = "rpa_runner";
  document.getElementById("qbLimit").value      = 50;
  document.getElementById("qbLogic").value      = "AND";
  document.getElementById("qbGroupToggle").checked = false;
  document.getElementById("qbOrderToggle").checked = true;
  document.getElementById("qbLimitToggle").checked = true;
  document.getElementById("qbConditions").innerHTML = "";
  if (document.getElementById("qbResultsCard"))            document.getElementById("qbResultsCard").style.display = "none";
  if (document.getElementById("qbInteractiveGridToolbar")) document.getElementById("qbInteractiveGridToolbar").style.display = "none";
  activeQueryContext = { table: null, sql: null, source: "builder" };
  updateColOptions();
  addCondition();
}

async function qbExportAllRecords() {
  const btn = document.getElementById("btnQbExportAll");
  if (!btn) return;
  const origText = "🗂️ Export All Query Records";

  btn.disabled = true;
  let secondsElapsed = 0;
  btn.innerHTML = `⏳ Fetching all… (<span id="qbExportTimer">0s</span>)`;

  const timerInterval = setInterval(() => {
    secondsElapsed++;
    const timerEl = document.getElementById("qbExportTimer");
    if (timerEl) timerEl.textContent = `${secondsElapsed}s`;
  }, 1000);

  try {
    const tableName     = document.getElementById("qbTable").value;
    const checkedCols   = getSelectedCols();
    const selectColumns = checkedCols.length > 0 ? checkedCols.join(", ") : "*";

    const previewEl = document.getElementById("qbSqlPreview");
    let sql = (previewEl && previewEl.getAttribute("contenteditable") === "true")
      ? previewEl.innerText.trim()
      : buildSQL();

    sql = sql
      .replace(/\bORDER\s+BY\s+[\s\S]+/i, "")
      .replace(/\bLIMIT\s+\d+[\s\S]*/i, "")
      .trim();
    sql += ";";

    const result = await api("/api/admin/query", {
      method:  "POST",
      headers: authHeaders(),
      body:    JSON.stringify({ sql }),
    });

    const rows = result.rows || result.items || result.data || result || [];
    clearInterval(timerInterval);

    if (rows.length === 0) {
      alert("The database returned 0 total records for this filter set.");
      return;
    }

    downloadCSV(rows, "full_query_unlimited");
  } catch (err) {
    clearInterval(timerInterval);
    if (!isSessionExpiredError(err)) alert("Export streaming error: " + String(err.message || err));
  } finally {
    clearInterval(timerInterval);
    setTimeout(() => {
      btn.disabled  = false;
      btn.innerHTML = origText;
    }, 1000);
  }
}
