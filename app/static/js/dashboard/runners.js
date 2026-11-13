import { api, authHeaders, escapeHtml, qcPill, formatDateTime, showSuccessToast, isSessionExpiredError } from '../utils.js';

export let liveCache = null;
export let dashTab = "all";
export let dashPage = 1;
export let liveTimer = null;
export let liveInFlight = null;

const openDetailsByRunner = new Map();
const loadBar = document.getElementById("loadBar");
const FAILURE_READ_KEY = "hub_failure_notifications_read_v1";
const FAILURE_BASE_TITLE = document.title;
let readFailureIds = loadReadFailureIds();
let currentFailureRows = [];

function loadReadFailureIds() {
  try {
    const parsed = JSON.parse(localStorage.getItem(FAILURE_READ_KEY) || "[]");
    return new Set(Array.isArray(parsed) ? parsed.map(String) : []);
  } catch (_) {
    return new Set();
  }
}

function saveReadFailureIds() {
  try {
    localStorage.setItem(FAILURE_READ_KEY, JSON.stringify(Array.from(readFailureIds).slice(-300)));
  } catch (_) {
    // Notifications still work for this page session if storage is unavailable.
  }
}

function failureId(run) {
  return String(run.id ?? [run.RunnerName, run.ScheduleName, run.CompletionDateTime, run.CompletionRemarks].join("|"));
}

function updateFailureWindowTitle(unreadCount) {
  document.title = unreadCount > 0 ? `(${unreadCount}) ${FAILURE_BASE_TITLE}` : FAILURE_BASE_TITLE;
}

function setLoading(on) { 
  if (loadBar) loadBar.classList.toggle("hidden", !on); 
}

function isSickBay(row) {
  const cr = row.control_room;
  const m = row.mysql || {};
  if (!(m.ControlRoomIP || "").toString().trim()) return true;
  if (!cr) return true;
  if (cr.poll_error) return true;
  if (!cr.device_matched) return true;
  const st = String(cr.device_status || "").toUpperCase();
  if (st.indexOf("CONNECT") < 0) return true;
  return false;
}

export function setDashTab(val) { dashTab = val; }
export function setDashPage(val) { dashPage = val; }

export function renderPager(totalItems, pageSize, page) {
  const info = document.getElementById("pagerInfo");
  const wrap = document.getElementById("pagerBtns");
  if (!wrap || !info) return;
  wrap.innerHTML = "";
  if (totalItems === 0) {
    info.textContent = "No rows in this view.";
    return;
  }
  if (pageSize <= 0) {
    info.textContent = "Showing all " + totalItems + " runner(s).";
    return;
  }
  const pages = Math.max(1, Math.ceil(totalItems / pageSize));
  const p = Math.min(Math.max(1, page), pages);
  dashPage = p;
  const from = (p - 1) * pageSize + 1;
  const to = Math.min(totalItems, p * pageSize);
  info.textContent = "Showing " + from + "–" + to + " of " + totalItems;
  for (let i = 1; i <= pages; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = String(i);
    if (i === p) b.classList.add("active");
    b.onclick = () => { dashPage = i; if (liveCache) renderRows(liveCache); };
    wrap.appendChild(b);
  }
}

export function renderRows(live) {
  liveCache = live;
  const tbody = document.getElementById("bots");
  const meta = document.getElementById("liveMeta");
  if (!tbody) return;
  
  renderFailureNotifications(live.failed_runs || []);
  const pollOn = live.cr_poll_enabled ? "on" : "off";
  if (meta) meta.textContent = "Background poller: " + pollOn + " · every " + (live.cr_poll_interval_seconds || "—") + "s · device API " + (live.cr_devices_list_path || "") + " · activity API " + (live.cr_activity_list_path || "");

  let items = live.items || [];
  const sickTotal = items.filter(isSickBay).length;
  const sickCountEl = document.getElementById("sickCount");
  if (sickCountEl) {
    if (sickTotal > 0) {
      sickCountEl.textContent = sickTotal;
      sickCountEl.style.display = "inline-flex";
    } else {
      sickCountEl.style.display = "none";
    }
  }

  if (dashTab === "sick") items = items.filter(isSickBay);

  const sel = document.getElementById("pageSizeSel");
  let pageSize = sel ? parseInt(sel.value, 10) : 12;
  if (isNaN(pageSize)) pageSize = 12;
  const all = pageSize <= 0;
  const total = items.length;

  if (all) {
    renderPager(total, 0, 1);
  } else {
    renderPager(total, pageSize, dashPage);
    const pages = Math.max(1, Math.ceil(total / pageSize));
    dashPage = Math.min(Math.max(1, dashPage), pages);
    const start = (dashPage - 1) * pageSize;
    items = items.slice(start, start + pageSize);
  }

  const neededRunners = new Set(items.map(r => (r.mysql || {}).RunnerName || ""));
  Array.from(tbody.querySelectorAll("tr")).forEach(tr => {
    const nameEl = tr.querySelector(".runner-name");
    const name = nameEl ? nameEl.textContent : "";
    if (!neededRunners.has(name) && !tr.classList.contains("no-results")) tr.remove();
  });

  for (const row of items) {
    const m = row.mysql || {};
    const name = m.RunnerName || "";
    const existingTr = Array.from(tbody.querySelectorAll("tr")).find(tr => {
      const nameEl = tr.querySelector(".runner-name");
      return nameEl && nameEl.textContent === name;
    });

    if (existingTr) {
      updateRowContent(existingTr, row);
    } else {
      const tr = createRowElement(row);
      tbody.appendChild(tr);
    }
  }
}

function createRowElement(row) {
  const m = row.mysql || {};
  const cr = row.control_room;
  const tr = document.createElement("tr");
  const readableName = m.RunnerReadableName || m.RunnerName || "";
  const runnerName = m.RunnerName || "";

  let runnerHtml = `
    <div class="runner-info-cell">
      <div class="runner-icon-combined">🤖</div>
      <div class="runner-details">
        <div class="runner-name">${escapeHtml(readableName)}</div>
        <div class="runner-meta-line">Task bot: <strong>${escapeHtml(m.TaskBotStatus || "—")}</strong></div>
        <div class="runner-meta-line">Machine Status: <span class="mono">${escapeHtml(m.RunnerStatus || "—")}</span></div>
        <div class="runner-meta-line mono muted" style="margin-top:0.35rem;font-size:0.75rem">${escapeHtml(runnerName ? `(${runnerName})` : "")} ${escapeHtml(m.ControlRoomIP || "—")}</div>
      </div>
    </div>
  `;
  tr.setAttribute("data-searchable", `${readableName} ${runnerName}`.toLowerCase());

  const qcTime = formatDateTime(m.QC_Time);
  let statusCol = "<div class='stack'>";
  statusCol += "<div>" + qcPill(m.QC_Status) + "</div>";
  statusCol += "<div class='muted' style='font-size:0.72rem'>Uptime: <strong>" + escapeHtml(qcTime) + "</strong></div>";
  const tb = (m.TaskBotStatus || "").toString().toUpperCase();
  if (tb.indexOf("RUNNING") >= 0) statusCol += "<div class='indeterminate' title='Task bot marked RUNNING in your database'></div>";
  statusCol += "</div>";

  let crCol = buildControlRoomColumn(cr, row.run_history);

  tr.innerHTML = `
    <td>${runnerHtml}</td>
    <td>${statusCol}</td>
    <td>${crCol}</td>
    <td style="text-align:center">
      <button type="button" class="actions-trigger-btn" data-runner="${escapeHtml(runnerName)}" style="background:var(--accent);color:#fff;border:1px solid var(--accent);border-radius:6px;padding:0.4rem 0.8rem;font-size:0.78rem;cursor:pointer;">⋮ Actions</button>
    </td>
  `;

  tr.querySelector(".actions-trigger-btn").onclick = (e) => {
    const runner = e.target.getAttribute("data-runner");
    showActionsPopover(runner, e.target);
  };

  syncDetailsState(tr, runnerName);
  return tr;
}

function updateRowContent(tr, row) {
  const cells = tr.querySelectorAll("td");
  if (cells.length >= 3) {
    const m = row.mysql || {};
    const cr = row.control_room;
    const readableName = m.RunnerReadableName || m.RunnerName || "";
    const runnerName = m.RunnerName || "";
    tr.setAttribute("data-searchable", `${readableName} ${runnerName}`.toLowerCase());

    cells[0].innerHTML = `
      <div class="runner-info-cell">
        <div class="runner-icon-combined">🤖</div>
        <div class="runner-details">
          <div class="runner-name">${escapeHtml(readableName)}</div>
          <div class="runner-meta-line">Task bot: <strong>${escapeHtml(m.TaskBotStatus || "—")}</strong></div>
          <div class="runner-meta-line">Machine Status: <span class="mono">${escapeHtml(m.RunnerStatus || "—")}</span></div>
          <div class="runner-meta-line mono muted" style="margin-top:0.35rem;font-size:0.75rem">${escapeHtml(runnerName ? `(${runnerName})` : "")} ${escapeHtml(m.ControlRoomIP || "—")}</div>
        </div>
      </div>
    `;

    const qcTime = formatDateTime(m.QC_Time);
    let statusCol = "<div class='stack'>";
    statusCol += "<div>" + qcPill(m.QC_Status) + "</div>";
    statusCol += "<div class='muted' style='font-size:0.72rem'>Uptime: <strong>" + escapeHtml(qcTime) + "</strong></div>";
    const tb = (m.TaskBotStatus || "").toString().toUpperCase();
    if (tb.indexOf("RUNNING") >= 0) statusCol += "<div class='indeterminate' title='Task bot marked RUNNING in your database'></div>";
    statusCol += "</div>";
    cells[1].innerHTML = statusCol;

    const previousDetails = Array.from(cells[2].querySelectorAll("details.cr-more")).map(el => ({
      summary: el.querySelector("summary")?.textContent?.trim() || "",
      open: el.open,
    }));
    if (runnerName) openDetailsByRunner.set(runnerName, previousDetails);

    cells[2].innerHTML = buildControlRoomColumn(cr, row.run_history);
    syncDetailsState(tr, runnerName);
  }
}

function buildControlRoomColumn(cr, history) {
  if (!cr) {
    return `<div class="status-side-grid"><div class="status-section"><div class="status-section-title">Machine</div><span class="muted">No Control Room data.</span></div><div class="status-section"><div class="status-section-title">Bot work</div>${buildRunHistory(history)}<span class="muted">No Control Room IP on this row.</span></div></div>`;
  }
  
  let machineSection = "<div class='status-section-title'>Machine</div>";
  if (cr.poll_error) {
    machineSection += "<div class='cr-machine'><span class='cr-dot bad'></span><div><div class='cr-machine-txt'>Link issue</div><div class='cr-machine-host'>" + escapeHtml(String(cr.poll_error).slice(0, 120)) + "</div></div></div>";
  } else if (!cr.device_matched) {
    machineSection += "<div class='cr-machine'><span class='cr-dot bad'></span><div><div class='cr-machine-txt'>Not in CR list</div><div class='cr-machine-host'>Name in CR must match runner name</div></div></div>";
  } else {
    const ok = String(cr.device_status || "").toUpperCase().indexOf("CONNECT") >= 0;
    machineSection += "<div class='cr-machine'><span class='cr-dot " + (ok ? "ok" : "bad") + "'></span><div><div class='cr-machine-txt'>" + escapeHtml(cr.device_status || "Unknown") + "</div><span class='cr-machine-host'>" + escapeHtml(cr.device_host || "") + "</span></div></div>";
  }

  let botWorkSection = "<div class='status-section-title'>Bot work</div>" + buildRunHistory(history);
  if (cr.activity_poll_error) {
    botWorkSection += "<div class='cr-headline idle'>Could not load</div>";
    botWorkSection += "<details class='cr-more'><summary>Why</summary><div class='cr-more-body'>" + escapeHtml(String(cr.activity_poll_error).slice(0, 400)) + "</div></details>";
  } else {
    const ac = cr.activity_counts || { active: 0, queued: 0, pending: 0, other: 0 };
    const run = ac.active || 0, que = ac.queued || 0, wait = ac.pending || 0, oth = ac.other || 0;
    const sum = run + que + wait + oth;

    if (sum === 0) {
      botWorkSection += "<div class='cr-headline idle'>Idle</div><div class='muted' style='font-size:0.76rem;margin-bottom:0.35rem'>No open runs.</div>";
    } else {
      botWorkSection += "<div class='cr-headline busy'>Busy</div>";
    }

    botWorkSection += `<div class='cr-tiles'>
      <div class='cr-tile run${run > 0 ? " on" : ""}'><div class='cr-tile-n'>${run}</div><div class='cr-tile-l'>Run</div></div>
      <div class='cr-tile queue${que > 0 ? " on" : ""}'><div class='cr-tile-n'>${que}</div><div class='cr-tile-l'>Queue</div></div>
      <div class='cr-tile wait${wait > 0 ? " on" : ""}'><div class='cr-tile-n'>${wait}</div><div class='cr-tile-l'>Wait</div></div>
      <div class='cr-tile other${oth > 0 ? " on" : ""}'><div class='cr-tile-n'>${oth}</div><div class='cr-tile-l'>Other</div></div>
    </div>`;

    if (sum > 0) {
      botWorkSection += `<div class='cr-bar-wrap' aria-hidden='true'>
        <span class='cr-bar-run' style='flex:${Math.max(run, 0.15)}'></span>
        <span class='cr-bar-queue' style='flex:${Math.max(que, 0.15)}'></span>
        <span class='cr-bar-wait' style='flex:${Math.max(wait, 0.15)}'></span>
        <span class='cr-bar-other' style='flex:${Math.max(oth, 0.15)}'></span>
      </div>`;
    }

    if (run > 0) botWorkSection += "<div class='indeterminate' style='margin:0.35rem 0 0.45rem' title='Running'></div>";

    if (cr.activity_last) {
      const L = cr.activity_last;
      const bot = String(L.fileName || L.automationName || "—").slice(0, 40);
      const prog = L.progress != null ? L.progress + "%" : "—";
      const lbl = sum === 0 ? "Last finished run" : "Latest on this runner";
      botWorkSection += "<div class='cr-last-label'>" + lbl + "</div><div class='cr-latest-line'><strong>" + escapeHtml(String(L.status || "—")) + "</strong> · " + escapeHtml(bot) + " · " + escapeHtml(prog) + "</div>";
    }

    const hint = cr.activity_hint || "";
    const total = cr.activity_cr_list_total;
    const matched = cr.activity_matched_rows;
    const rec = cr.activities_recent || [];
    let moreHtml = "";
    if (hint) moreHtml += escapeHtml(hint);
    if (rec.length > 1) {
      if (moreHtml) moreHtml += "<br/><br/>";
      moreHtml += "Open runs:";
      for (let i = 0; i < Math.min(5, rec.length); i++) {
        moreHtml += "<br/>· " + escapeHtml(String(rec[i].status || "")) + " — " + escapeHtml(String(rec[i].fileName || rec[i].automationName || "").slice(0, 40));
      }
    }
    if ((matched === 0 || matched == null) && total > 0) {
      if (moreHtml) moreHtml += "<br/><br/>";
      moreHtml += "CR returned activity, but none matched this runner. Check machine name & runner ID.";
    }
    if (moreHtml) botWorkSection += "<details class='cr-more'><summary>Details</summary><div class='cr-more-body'>" + moreHtml + "</div></details>";
  }

  return `<div class="status-side-grid">
    <div class="status-section">${machineSection}</div>
    <div class="status-section">${botWorkSection}</div>
  </div>`;
}

function buildRunHistory(history) {
  if (!history) return "<div class='cr-last-label'>Previous run</div><div class='muted' style='font-size:0.76rem;margin-bottom:0.4rem'>No scheduler run recorded.</div>";
  const status = String(history.CompletionStatus || "Unknown");
  const failed = status.toUpperCase() === "RUN_FAILED";
  const task = String(history.ScheduleName || "Unnamed bot");
  const error = String(history.CompletionRemarks || "").trim();
  const when = formatDateTime(history.CompletionDateTime);
  let html = `<div class='cr-last-label'>Previous scheduler result</div><div class='cr-latest-line'><strong class='${failed ? "run-history-failed" : ""}'>${escapeHtml(status)}</strong> | ${escapeHtml(task)} | ${escapeHtml(when)}</div>`;
  if (failed && error) html += `<div class='run-history-error'>${escapeHtml(error.slice(0, 300))}</div>`;
  return html;
}

function renderFailureNotifications(failures) {
  const button = document.getElementById("failureNotifyBtn");
  const badge = document.getElementById("failureNotifyCount");
  const list = document.getElementById("failureNotifyList");
  if (!button || !badge || !list) return;

  const rows = Array.isArray(failures) ? failures.slice() : [];
  rows.sort((a, b) => {
    const byId = Number(b.id || 0) - Number(a.id || 0);
    if (Number.isFinite(byId) && byId !== 0) return byId;
    return Date.parse(b.CompletionDateTime || "") - Date.parse(a.CompletionDateTime || "");
  });
  currentFailureRows = rows;
  const unreadCount = rows.filter(run => !readFailureIds.has(failureId(run))).length;
  badge.textContent = String(unreadCount);
  badge.classList.toggle("hidden", unreadCount === 0);
  button.setAttribute("aria-label", unreadCount ? `${unreadCount} unread failed bot run${unreadCount === 1 ? "" : "s"} in the last 2 hours` : "No unread failed bot runs in the last 2 hours");
  updateFailureWindowTitle(unreadCount);

  const readAllButton = document.getElementById("failureNotifyReadAll");
  if (readAllButton) {
    readAllButton.disabled = unreadCount === 0;
    readAllButton.textContent = unreadCount ? "Mark all read" : "All read";
  }
  if (rows.length === 0) {
    list.innerHTML = "<div class='failure-notify-empty'>No failed runs in the last 2 hours.</div>";
    return;
  }
  list.innerHTML = rows.map(run => {
    const isRead = readFailureIds.has(failureId(run));
    const runner = String(run.RunnerName || "Unknown runner");
    const bot = String(run.ScheduleName || "Unnamed bot");
    const error = String(run.CompletionRemarks || "No error details recorded.");
    return `<article class='failure-notify-item${isRead ? " is-read" : " is-unread"}'><div><strong>${escapeHtml(runner)}</strong><span>${escapeHtml(formatDateTime(run.CompletionDateTime))}</span></div><div class='failure-notify-bot'>${escapeHtml(bot)}</div><div class='failure-notify-error'>${escapeHtml(error.slice(0, 240))}</div></article>`;
  }).join("");
}

function syncDetailsState(tr, runnerName) {
  const saved = openDetailsByRunner.get(runnerName) || [];
  if (saved.length > 0) {
    Array.from(tr.querySelectorAll("details.cr-more")).forEach(el => {
      const summaryText = el.querySelector("summary")?.textContent?.trim() || "";
      if (saved.some(item => item.summary === summaryText && item.open)) {
        el.open = true;
      }
    });
  }
}

function showActionsPopover(runner, triggerBtn) {
  const popover = document.getElementById("actionsPopover");
  if (!popover) return;

  popover.innerHTML = `
    <button type="button" class="primary" data-act="trigger">Trigger bot</button>
    <button type="button" data-act="disable">Set Disable</button>
    <button type="button" data-act="restart">Set Restart</button>
  `;

  const rect = triggerBtn.getBoundingClientRect();
  popover.style.left = `${rect.left - 80}px`;
  popover.style.top = `${rect.bottom + window.scrollY + 8}px`;
  popover.classList.add("open");

  const cleanup = () => {
    popover.classList.remove("open");
    document.removeEventListener("scroll", cleanup, true);
    window.removeEventListener("resize", cleanup);
    document.removeEventListener("click", handleClickOutside);
  };

  document.addEventListener("scroll", cleanup, true);
  window.addEventListener("resize", cleanup);

  const handleClickOutside = (e) => {
    if (!e.target.closest(".actions-popover") && !e.target.closest(".actions-trigger-btn")) {
      cleanup();
    }
  };

  popover.querySelectorAll("button").forEach(btn => {
    btn.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      btn.disabled = true;
      try {
        const act = btn.getAttribute("data-act");
        if (act === "trigger") {
          await api(`/api/admin/bots/${encodeURIComponent(runner)}/trigger`, { method: "POST", headers: authHeaders() });
          showSuccessToast("Trigger sent for " + runner);
        } else {
          const sm = { disable: "DISABLE", restart: "RESTART" };
          await api(`/api/admin/bots/${encodeURIComponent(runner)}/status`, { method: "PUT", headers: authHeaders(), body: JSON.stringify({ status: sm[act] }) });
          showSuccessToast("Status changed to " + sm[act] + " for " + runner);
        }
        cleanup();
        await refreshLive();
      } catch (err) {
        if (!isSessionExpiredError(err)) alert(String(err.message || err));
      } finally {
        btn.disabled = false;
      }
    };
  });

  setTimeout(() => { document.addEventListener("click", handleClickOutside); }, 0);
}

export function updatePollStatus() {
  const status = document.getElementById("pollStatus");
  if (!status) return;
  const hasOpenDetails = document.querySelector("details.cr-more[open]") !== null;
  status.textContent = hasOpenDetails ? "Paused while viewing details — refresh resumes when closed." : "";
}

export function startDashRefresh() {
  if (liveTimer) clearInterval(liveTimer);
  refreshLive();
  updatePollStatus();
  liveTimer = setInterval(() => {
    const dash = document.getElementById("view-dash");
    const hasOpenDetails = document.querySelector("details.cr-more[open]") !== null;
    updatePollStatus();
    if (dash && dash.classList.contains("active") && !hasOpenDetails) {
      if (!liveInFlight) refreshLive();
    }
  }, 8000);
}

export function stopDashRefresh() {
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = null;
  }
}

export async function refreshLive() {
  if (liveInFlight) return;
  setLoading(true);
  const run = (async () => {
    try {
      const live = await api("/api/admin/bots/live");
      renderRows(live);
    } finally {
      setLoading(false);
    }
  })();
  liveInFlight = run.finally(() => { liveInFlight = null; });
  return liveInFlight;
}

let failureNotificationsInitialized = false;

function initFailureNotifications() {
  if (failureNotificationsInitialized) return;
  failureNotificationsInitialized = true;
  const button = document.getElementById("failureNotifyBtn");
  const panel = document.getElementById("failureNotifyPanel");
  const readAllButton = document.getElementById("failureNotifyReadAll");
  if (!button || !panel) return;
  if (readAllButton) {
    readAllButton.addEventListener("click", () => {
      currentFailureRows.forEach(run => readFailureIds.add(failureId(run)));
      saveReadFailureIds();
      renderFailureNotifications(currentFailureRows);
    });
  }
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = !panel.classList.contains("open");
    panel.classList.toggle("open", open);
    button.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (event) => {
    if (!panel.contains(event.target) && !button.contains(event.target)) {
      panel.classList.remove("open");
      button.setAttribute("aria-expanded", "false");
    }
  });
}

export function initRunnersEvents() {
  initFailureNotifications();
  const btnPoll = document.getElementById("btnPoll");
  if (btnPoll) {
    btnPoll.onclick = async () => {
      try {
        setLoading(true);
        await api("/api/admin/bots/poll/cr-devices", { method: "POST", headers: authHeaders() });
        await refreshLive();
        showSuccessToast("Control Room data refreshed");
      } catch (e) {
        if (!isSessionExpiredError(e)) alert(String(e.message || e));
      } finally {
        setLoading(false);
      }
    };
  }

  const searchInput = document.getElementById("tableSearch");
  if (searchInput) {
    searchInput.oninput = () => {
      const query = searchInput.value.toLowerCase().trim();

      // Pause auto-refresh while searching so actions aren't disrupted
      if (query !== "") {
        stopDashRefresh();
      } else {
        // Resume refresh when search is cleared
        startDashRefresh();
      }

      const rows = document.querySelectorAll("#bots tr");
      let visibleCount = 0;
      rows.forEach(row => {
        const searchable = row.getAttribute("data-searchable") || "";
        if (query === "" || searchable.includes(query)) {
          row.style.display = "";
          visibleCount++;
        } else {
          row.style.display = "none";
        }
      });
      const tbody = document.getElementById("bots");
      if (visibleCount === 0 && query !== "" && tbody) {
        if (!tbody.querySelector(".no-results")) {
          const tr = document.createElement("tr");
          tr.className = "no-results";
          tr.innerHTML = `<td colspan="4" style="text-align:center;padding:2rem;color:var(--muted)">No runners found matching "${escapeHtml(query)}"</td>`;
          tbody.appendChild(tr);
        }
      } else {
        const noResults = document.querySelector("#bots .no-results");
        if (noResults) noResults.remove();
      }
    };
  }
}