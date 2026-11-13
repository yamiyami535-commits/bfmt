// Utility helpers for Bot Farm Management Tools

export const token = sessionStorage.getItem("access_token");

let sessionExpiredModalShown = false;
const sessionExpiryTime = Number(sessionStorage.getItem("access_token_expires_at"));
if (sessionExpiryTime) {
  setTimeout(() => showSessionExpiredModal(), Math.max(0, sessionExpiryTime - Date.now()));
}

export function showSessionExpiredModal() {
  if (sessionExpiredModalShown) return;
  sessionExpiredModalShown = true;
  sessionStorage.removeItem("access_token");
  sessionStorage.removeItem("access_token_expires_at");
  const modal = document.getElementById("sessionExpiredModal");
  if (!modal) {
    window.location.replace("/admin");
    return;
  }
  modal.classList.remove("hidden");
  document.getElementById("sessionExpiredSignIn")?.focus();
}

export function isSessionExpiredError(error) {
  return Boolean(error && error.name === "SessionExpiredError");
}


export function checkAuth() {
  if (!token) {
    window.location.href = "/admin";
    return false;
  }
  return true;
}

export function authHeaders() {
  return { 
    "Authorization": "Bearer " + token, 
    "Content-Type": "application/json" 
  };
}

export async function api(path, opts = {}) {
  const r = await fetch(path, { 
    ...opts, 
    headers: { ...(opts.headers || {}), Authorization: "Bearer " + token } 
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) {
    showSessionExpiredModal();
    const error = new Error("Your session has expired. Sign in again to continue.");
    error.name = "SessionExpiredError";
    throw error;
  }
  if (!r.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data) || r.statusText);
  }
  return data;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function formatDateTime(dateStr) {
  if (!dateStr) return "—";
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return escapeHtml(String(dateStr).slice(0, 16));
  const pad = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

export function showSuccessToast(message) {
  const toast = document.createElement("div");
  toast.className = "action-success-toast";
  toast.textContent = "✓ " + message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2500);
}

export function qcPill(qc) {
  const v = (qc || "").toString();
  if (!v) return "<span class='muted'>—</span>";
  const cls = v.toLowerCase() === "down" ? "pill bad" : "pill ok";
  return "<span class='" + cls + "'>" + escapeHtml(v) + "</span>";
}