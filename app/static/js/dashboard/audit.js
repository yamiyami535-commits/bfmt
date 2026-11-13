import { api, escapeHtml } from '../utils.js';

let auditInFlight = null;

function auditSummary(action, detail) {
  if (detail == null || typeof detail !== "object") return "—";
  if (action === "bot.status") return "Task bot set to " + String(detail.status || detail.wanted || "—");
  if (action === "bot.trigger") return detail.error ? ("Trigger failed: " + String(detail.error).slice(0, 120)) : "Control Room deploy triggered";
  if (action === "bot.restart_service") {
    const r = detail.result || {};
    return "Restart " + (detail.service || "service") + " → HTTP " + (r.status_code ?? "—") + (r.ok ? " ok" : " fail");
  }
  if (action === "cr.poll_manual") return "Manual Control Room poll";
  if (action === "jobs.recover_down") return "Recover batch: checked " + String(detail.checked ?? "—");
  if (action === "settings.telegram.update") return "Telegram settings saved (" + (detail.keys || []).join(", ") + ")";
  if (action === "settings.email.update") return "Email settings saved (" + (detail.keys || []).join(", ") + ")";
  if (action === "integrations.telegram.test") return (detail.error || detail.ok === false) ? ("Test failed: " + String((detail.error || "")).slice(0, 100)) : "Telegram test sent";
  if (action === "integrations.email.test") return (detail.error || detail.ok === false) ? ("Test failed: " + String((detail.error || "")).slice(0, 100)) : "Email test sent";
  try {
    const j = JSON.stringify(detail);
    return j.length > 140 ? j.slice(0, 137) + "…" : j;
  } catch (_) {
    return "—";
  }
}

function formatAuditDateTime(raw) {
  if (raw == null || raw === "") return "—";
  const s = String(raw).trim();
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?/);
  if (m) {
    const pad = (n) => String(n).padStart(2, "0");
    return m[1] + "-" + m[2] + "-" + m[3] + " " + pad(m[4]) + ":" + m[5] + ":" + m[6];
  }
  const d = new Date(s);
  if (!Number.isNaN(d.getTime())) {
    const pad = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  return escapeHtml(s);
}

export async function loadAudit() {
  if (auditInFlight) return auditInFlight;
  const tbody = document.getElementById("auditRows");
  const meta = document.getElementById("auditMeta");
  
  const run = (async () => {
    if (!tbody) return;
    tbody.innerHTML = "";
    if (meta) meta.textContent = "Loading…";
    try {
      const j = await api("/api/admin/audit/logs?limit=150");
      if (meta) meta.textContent = j.total + " in database · showing " + j.items.length + " (newest " + j.limit + ")";
      for (const r of j.items) {
        const tr = document.createElement("tr");
        const res = [r.resource_type, r.resource_id].filter(Boolean).join(" ");
        const ok = r.success ? "yes" : "no";
        const det = r.detail != null ? JSON.stringify(r.detail) : "";
        tr.title = det.length > 800 ? det.slice(0, 800) + "…" : det;
        const sum = escapeHtml(auditSummary(r.action, r.detail));
        const tcell = formatAuditDateTime(r.created_at);
        tr.innerHTML = "<td class='time-cell'>" + tcell +
          "</td><td class='mono'>" + escapeHtml(r.actor_username) + "</td><td class='mono'>" + escapeHtml(r.action) +
          "</td><td class='summary-cell'>" + sum + "</td><td class='mono muted' style='font-size:0.76rem'>" + escapeHtml(res || "—") +
          "</td><td>" + ok + "</td><td class='mono muted' style='font-size:0.74rem'>" + escapeHtml(r.client_ip || "—") + "</td>";
        tbody.appendChild(tr);
      }
    } catch (e) {
      if (meta) meta.textContent = String(e.message || e);
    }
  })();
  auditInFlight = run.finally(() => { auditInFlight = null; });
  return auditInFlight;
}