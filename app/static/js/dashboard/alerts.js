import { api, authHeaders } from '../utils.js';
import { loadAudit } from './audit.js';

export async function loadAlertForms() {
  const tgMsg = document.getElementById("tgMsg");
  const emMsg = document.getElementById("emMsg");
  if (tgMsg) tgMsg.textContent = "";
  if (emMsg) emMsg.textContent = "";
  
  try {
    const tg = await api("/api/admin/integrations/telegram");
    document.getElementById("tgChat").value = tg.chat_id || "";
    document.getElementById("tgTpl").value = tg.message_template || "";
    document.getElementById("tgToken").value = "";
    document.getElementById("tgTokenMasked").textContent = tg.bot_token_masked ? ("Current token: " + tg.bot_token_masked) : "No token saved yet.";
    
    const em = await api("/api/admin/integrations/email");
    document.getElementById("emHost").value = em.smtp_host || "";
    document.getElementById("emPort").value = em.smtp_port || 587;
    document.getElementById("emTls").checked = !!em.use_tls;
    document.getElementById("emUser").value = em.username || "";
    document.getElementById("emPass").value = "";
    document.getElementById("emFrom").value = em.from_addr || "";
    document.getElementById("emTo").value = em.to_addr || "";
  } catch (e) {
    if (tgMsg) tgMsg.textContent = String(e.message || e);
  }
}

export function initAlertsEvents() {
  document.getElementById("btnTgSave").onclick = async () => {
    const el = document.getElementById("tgMsg");
    el.textContent = "";
    try {
      const body = { chat_id: document.getElementById("tgChat").value.trim(), message_template: document.getElementById("tgTpl").value };
      const tok = document.getElementById("tgToken").value.trim();
      if (tok) body.bot_token = tok;
      await api("/api/admin/integrations/telegram", { method: "PUT", headers: authHeaders(), body: JSON.stringify(body) });
      el.textContent = "Saved.";
      await loadAlertForms();
    } catch (e) {
      el.textContent = String(e.message || e);
    }
  };

  document.getElementById("btnTgTest").onclick = async () => {
    const el = document.getElementById("tgMsg");
    el.textContent = "";
    try {
      await api("/api/admin/integrations/telegram/test", { method: "POST", headers: authHeaders(), body: JSON.stringify({ message: "Test from Bot Farm Management Tools" }) });
      el.textContent = "Test sent.";
      loadAudit();
    } catch (e) {
      el.textContent = String(e.message || e);
    }
  };

  document.getElementById("btnEmSave").onclick = async () => {
    const el = document.getElementById("emMsg");
    el.textContent = "";
    try {
      const body = {
        smtp_host: document.getElementById("emHost").value.trim(),
        smtp_port: parseInt(document.getElementById("emPort").value, 10) || 587,
        use_tls: document.getElementById("emTls").checked,
        username: document.getElementById("emUser").value.trim() || null,
        from_addr: document.getElementById("emFrom").value.trim(),
        to_addr: document.getElementById("emTo").value.trim(),
      };
      const pw = document.getElementById("emPass").value;
      if (pw) body.password = pw;
      await api("/api/admin/integrations/email", { method: "PUT", headers: authHeaders(), body: JSON.stringify(body) });
      el.textContent = "Saved.";
      await loadAlertForms();
    } catch (e) {
      el.textContent = String(e.message || e);
    }
  };

  document.getElementById("btnEmTest").onclick = async () => {
    const el = document.getElementById("emMsg");
    el.textContent = "";
    try {
      await api("/api/admin/integrations/email/test", { method: "POST", headers: authHeaders(), body: JSON.stringify({ subject: "Bot Farm test", body: "Test email from Bot Farm Management Tools." }) });
      el.textContent = "Test sent.";
      loadAudit();
    } catch (e) {
      el.textContent = String(e.message || e);
    }
  };
}