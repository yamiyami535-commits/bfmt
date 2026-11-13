import { api, authHeaders, escapeHtml } from '../utils.js';

// ─── State ────────────────────────────────────────────────────────────────────
let scenarioTree   = {};   // { company: [ {scenario_key, scenario_label, channels:{ch:{form_fields}}}, ... ] }
let runnersCache   = [];   // [{RunnerName, RunnerReadableName, RunnerID, ControlRoomIP, RunnerStatus}]
let currentChannelFields = []; // form_fields for the currently selected channel

export async function initTicketInsert() {
  document.getElementById("tiCompany")?.addEventListener("change", onCompanyChange);
  document.getElementById("tiScenario")?.addEventListener("change", onScenarioChange);
  document.getElementById("tiChannel")?.addEventListener("change", onChannelChange);
  document.getElementById("tiForm")?.addEventListener("submit", onSubmit);
  document.getElementById("tiResetBtn")?.addEventListener("click", resetForm);

  await Promise.all([loadScenarios(), loadRunners()]);
}

// ─── Loaders ──────────────────────────────────────────────────────────────────

async function loadScenarios() {
  const wrap = document.getElementById("tiCompany");
  if (!wrap) return;
  try {
    const res = await api("/api/admin/ticket-insert/scenarios", { method: "GET", headers: authHeaders() });
    scenarioTree = res.companies || {};
    const companies = Object.keys(scenarioTree);
    wrap.innerHTML = `<option value="">Select company…</option>` +
      companies.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  } catch (err) {
    wrap.innerHTML = `<option value="">Failed to load companies</option>`;
    console.error("Failed to load ticket scenarios:", err);
  }
}

async function loadRunners() {
  try {
    const res = await api("/api/admin/ticket-insert/runners", { method: "GET", headers: authHeaders() });
    runnersCache = res.runners || [];
    renderRunnerOptions();
  } catch (err) {
    console.error("Failed to load runners:", err);
  }
}

function renderRunnerOptions() {
  const sel = document.getElementById("tiRunner");
  if (!sel) return;
  if (runnersCache.length === 0) {
    sel.innerHTML = `<option value="">No runners found</option>`;
    return;
  }
  sel.innerHTML = `<option value="">Select runner…</option>` + runnersCache.map(r => {
    const label = r.RunnerReadableName ? `${r.RunnerName} — ${r.RunnerReadableName}` : r.RunnerName;
    const statusTag = r.RunnerStatus ? ` (${r.RunnerStatus})` : "";
    return `<option value="${escapeHtml(r.RunnerName)}">${escapeHtml(label)}${escapeHtml(statusTag)}</option>`;
  }).join("");
}

// ─── Cascade handlers ─────────────────────────────────────────────────────────

function onCompanyChange() {
  const company = document.getElementById("tiCompany").value;
  const scenarioSel = document.getElementById("tiScenario");
  const channelSel  = document.getElementById("tiChannel");

  channelSel.innerHTML = `<option value="">Select scenario first…</option>`;
  channelSel.disabled  = true;
  clearDynamicForm();

  if (!company || !scenarioTree[company]) {
    scenarioSel.innerHTML = `<option value="">Select company first…</option>`;
    scenarioSel.disabled  = true;
    return;
  }

  const scenarios = scenarioTree[company];
  scenarioSel.disabled = false;
  scenarioSel.innerHTML = `<option value="">Select scenario…</option>` +
    scenarios.map(s => `<option value="${escapeHtml(s.scenario_key)}">${escapeHtml(s.scenario_label)}</option>`).join("");
}

function onScenarioChange() {
  const company     = document.getElementById("tiCompany").value;
  const scenarioKey = document.getElementById("tiScenario").value;
  const channelSel  = document.getElementById("tiChannel");

  clearDynamicForm();

  const scenario = (scenarioTree[company] || []).find(s => s.scenario_key === scenarioKey);
  if (!scenario) {
    channelSel.innerHTML = `<option value="">Select scenario first…</option>`;
    channelSel.disabled  = true;
    return;
  }

  const channels = Object.keys(scenario.channels || {});
  channelSel.disabled = false;
  channelSel.innerHTML = `<option value="">Select channel…</option>` +
    channels.map(ch => `<option value="${escapeHtml(ch)}">${escapeHtml(ch)}</option>`).join("");
}

function onChannelChange() {
  const company     = document.getElementById("tiCompany").value;
  const scenarioKey = document.getElementById("tiScenario").value;
  const channel     = document.getElementById("tiChannel").value;

  clearDynamicForm();
  if (!company || !scenarioKey || !channel) return;

  const scenario = (scenarioTree[company] || []).find(s => s.scenario_key === scenarioKey);
  const chCfg    = scenario?.channels?.[channel];
  if (!chCfg) return;

  currentChannelFields = chCfg.form_fields || [];
  renderDynamicForm(currentChannelFields);
}

// ─── Dynamic form rendering ───────────────────────────────────────────────────

function renderDynamicForm(fields) {
  const container = document.getElementById("tiDynamicFields");
  const submitRow  = document.getElementById("tiSubmitRow");
  if (!container) return;

  if (fields.length === 0) {
    container.innerHTML = `<p class="muted" style="font-style:italic">No additional fields required for this channel.</p>`;
  } else {
    container.innerHTML = fields.map(f => renderField(f)).join("");
  }
  if (submitRow) submitRow.style.display = "";
}

function renderField(f) {
  const req = f.required ? "required" : "";
  const reqMark = f.required ? ` <span style="color:var(--dt-bad)">*</span>` : "";
  if (f.type === "select") {
    const opts = (f.options || []).map(o => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join("");
    return `
      <div class="ti-field">
        <label for="ti_${escapeHtml(f.key)}">${escapeHtml(f.label)}${reqMark}</label>
        <select id="ti_${escapeHtml(f.key)}" name="${escapeHtml(f.key)}" ${req}
          style="padding:0.45rem 0.65rem;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:inherit;font-size:0.85rem;width:100%">
          <option value="">Select…</option>${opts}
        </select>
      </div>`;
  }
  return `
    <div class="ti-field">
      <label for="ti_${escapeHtml(f.key)}">${escapeHtml(f.label)}${reqMark}</label>
      <input type="text" id="ti_${escapeHtml(f.key)}" name="${escapeHtml(f.key)}" ${req}
        style="padding:0.45rem 0.65rem;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:inherit;font-size:0.85rem;width:100%" />
    </div>`;
}

function clearDynamicForm() {
  const container = document.getElementById("tiDynamicFields");
  const submitRow  = document.getElementById("tiSubmitRow");
  const resultBox  = document.getElementById("tiResultBox");
  if (container) container.innerHTML = "";
  if (submitRow) submitRow.style.display = "none";
  if (resultBox) { resultBox.style.display = "none"; resultBox.innerHTML = ""; }
  currentChannelFields = [];
}

// ─── Submit ───────────────────────────────────────────────────────────────────

async function onSubmit(e) {
  e.preventDefault();

  const company     = document.getElementById("tiCompany").value;
  const scenarioKey = document.getElementById("tiScenario").value;
  const channel     = document.getElementById("tiChannel").value;
  const runnerName  = document.getElementById("tiRunner").value;

  const resultBox = document.getElementById("tiResultBox");
  const btn       = document.getElementById("tiSubmitBtn");

  if (!company || !scenarioKey || !channel) {
    showResult("error", "Please select company, scenario, and channel.");
    return;
  }
  if (!runnerName) {
    showResult("error", "Please select a runner.");
    return;
  }

  const fields = {};
  for (const f of currentChannelFields) {
    const el = document.getElementById(`ti_${f.key}`);
    fields[f.key] = el ? el.value.trim() : "";
  }

  const missing = currentChannelFields.filter(f => f.required && !fields[f.key]).map(f => f.label);
  if (missing.length > 0) {
    showResult("error", `Missing required field(s): ${missing.join(", ")}`);
    return;
  }

  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = "Submitting…";

  try {
    const res = await api("/api/admin/ticket-insert/submit", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        company,
        scenario_key: scenarioKey,
        channel,
        runner_name: runnerName,
        fields,
      }),
    });

    showResult("ok", `Inserted successfully. ScheduleID: <strong>${escapeHtml(res.schedule_id)}</strong> · Runner: <strong>${escapeHtml(res.runner_name)}</strong>`);
    resetFormFieldsOnly();
  } catch (err) {
    showResult("error", String(err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

function showResult(kind, html) {
  const resultBox = document.getElementById("tiResultBox");
  if (!resultBox) return;
  resultBox.style.display = "";
  resultBox.className = kind === "ok" ? "tl-badge tl-badge--ok" : "tl-badge tl-badge--warn";
  resultBox.style.display = "block";
  resultBox.style.padding = "0.6rem 0.85rem";
  resultBox.innerHTML = html;
}

function resetFormFieldsOnly() {
  currentChannelFields.forEach(f => {
    const el = document.getElementById(`ti_${f.key}`);
    if (el) el.value = "";
  });
}

function resetForm() {
  document.getElementById("tiCompany").value  = "";
  onCompanyChange();
  const resultBox = document.getElementById("tiResultBox");
  if (resultBox) { resultBox.style.display = "none"; resultBox.innerHTML = ""; }
}
