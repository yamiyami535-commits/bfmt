import { checkAuth, api, authHeaders, showSessionExpiredModal, isSessionExpiredError } from '../utils.js';
import { loadAudit } from './audit.js';
import { loadAlertForms, initAlertsEvents } from './alerts.js';
import { initQueryBuilder } from './queryBuilder.js';
import { startDashRefresh, stopDashRefresh, refreshLive, initRunnersEvents, liveCache, dashTab, setDashTab, setDashPage } from './runners.js';
import { startSchedRefresh, stopSchedRefresh, initSchedulerEvents } from './scheduler.js';

const THEME_KEY = "hub_theme";
const ACCENT_KEY = "hub_accent";
const NAV_COLLAPSE_KEY = "hub_nav_collapsed";

let userRole = "admin";

function applyTheme() {
  const t = localStorage.getItem(THEME_KEY) || "dark";
  document.documentElement.setAttribute("data-theme", t === "light" ? "light" : "dark");
  const btn = document.getElementById("themeToggle");
  if (btn) btn.textContent = t === "light" ? "Dark mode" : "Light mode";
}

function toggleTheme() {
  const cur = localStorage.getItem(THEME_KEY) || "dark";
  localStorage.setItem(THEME_KEY, cur === "dark" ? "light" : "dark");
  applyTheme();
}

function applyAccent() {
  const a = localStorage.getItem(ACCENT_KEY) || "blue";
  document.documentElement.setAttribute("data-accent", a);
  document.querySelectorAll(".swatch").forEach(s => s.classList.toggle("active", s.getAttribute("data-set") === a));
}

function applyNavCollapsed() {
  const sideNav = document.getElementById("sideNav");
  const btnNavCollapse = document.getElementById("btnNavCollapse");
  if (!sideNav || !btnNavCollapse) return;
  const c = localStorage.getItem(NAV_COLLAPSE_KEY) === "1";
  sideNav.classList.toggle("collapsed", c);
  btnNavCollapse.textContent = c ? "»" : "«";
  btnNavCollapse.title = c ? "Expand sidebar" : "Collapse sidebar";
}

function isAdminUser() {
  return userRole === "admin";
}

function parseViewFromHash() {
  const h = (location.hash || "#dash").replace(/^#/, "");
  if (h === "alerts" && !isAdminUser()) return "dash";
  if (h === "ticketlookup") return "qb"; // Ticket Lookup is inside QB
  if (h === "scheduler") return "audit";  // Scheduler is now a sub-tab of Audit
  if (["audit", "alerts", "dash", "ikb", "qb"].includes(h)) return h;
  return "dash";
}

function hideRestrictedUI() {
  if (!isAdminUser()) {
    document.querySelectorAll(".nav-item[data-view='alerts']").forEach(el => el.remove());
    const alertsView = document.getElementById("view-alerts");
    if (alertsView) alertsView.remove();
    const manageAllButton = document.getElementById("btnManageAll");
    if (manageAllButton) manageAllButton.style.display = "none";
  } else {
    // Show Ticket Lookup tab inside Query Builder for admins
    const tlTab = document.getElementById("qbTabTicket");
    if (tlTab) tlTab.style.display = "";
  }
}

let qbInitialized = false;

let auditTabBarInitialized = false;

function initAuditTabBar() {
  if (auditTabBarInitialized) return;
  auditTabBarInitialized = true;

  const tabBar = document.getElementById("auditTabBar");
  if (!tabBar) return;

  tabBar.addEventListener("click", (e) => {
    const tab = e.target.closest(".qb-tab");
    if (!tab) return;
    const target = tab.dataset.audittab;
    document.querySelectorAll("#auditTabBar .qb-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll("#view-audit .qb-pane").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    const pane = document.getElementById("auditPane-" + target);
    if (pane) pane.classList.add("active");

    if (target === "scheduler") {
      startSchedRefresh();
    } else {
      stopSchedRefresh();
      if (target === "log") loadAudit();
    }
  });
}

function getActiveAuditSubTab() {
  const active = document.querySelector("#auditTabBar .qb-tab.active");
  return active ? active.dataset.audittab : "log";
}

function applyRoute(name) {
  if (name === "alerts" && !isAdminUser()) name = "dash";
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));

  const el = document.getElementById("view-" + name);
  if (el) el.classList.add("active");

  document.querySelectorAll(".nav-item").forEach(b => {
    b.classList.toggle("active", b.getAttribute("data-view") === name);
  });

  // Stop both refresh loops on every nav; restart only what's needed below
  stopDashRefresh();
  stopSchedRefresh();

  if (name === "dash") {
    startDashRefresh();
  } else if (name === "audit") {
    initAuditTabBar();
    // Resume whichever sub-tab is currently active
    const subTab = getActiveAuditSubTab();
    if (subTab === "scheduler") {
      startSchedRefresh();
    } else {
      loadAudit();
    }
  } else if (name === "alerts") {
    loadAlertForms();
  } else if (name === "qb" && !qbInitialized) {
    initQueryBuilder();
    qbInitialized = true;
  }
}

async function initializeApp() {
  if (!checkAuth()) return;

  applyTheme();
  applyAccent();
  applyNavCollapsed();

  document.getElementById("themeToggle").onclick = toggleTheme;
  document.querySelectorAll(".swatch").forEach(s => {
    s.onclick = () => {
      localStorage.setItem(ACCENT_KEY, s.getAttribute("data-set") || "blue");
      applyAccent();
    };
  });

  const btnNavCollapse = document.getElementById("btnNavCollapse");
  if (btnNavCollapse) {
    btnNavCollapse.onclick = () => {
      const sideNav = document.getElementById("sideNav");
      localStorage.setItem(NAV_COLLAPSE_KEY, sideNav.classList.contains("collapsed") ? "0" : "1");
      applyNavCollapsed();
    };
  }

  // Profile Menu Dropdown Setup
  const profileBtn = document.getElementById("profileBtn");
  const profileDropdown = document.getElementById("profileDropdown");
  if (profileBtn && profileDropdown) {
    profileBtn.onclick = () => profileDropdown.classList.toggle("open");
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".profile-menu")) profileDropdown.classList.remove("open");
    });
  }

  const signOutBtn = document.getElementById("out");
  if (signOutBtn) {
    signOutBtn.onclick = () => { 
      sessionStorage.removeItem("access_token");
      sessionStorage.removeItem("access_token_expires_at"); 
      window.location.href = "/admin"; 
    };
  }

  // Bind View Routing Navigation Controls
  document.querySelectorAll(".nav-item").forEach(btn => {
    btn.onclick = () => {
      const v = btn.getAttribute("data-view");
      if (v === "alerts" && !isAdminUser()) return;
      location.hash = "#" + v;
    };
  });

  window.onhashchange = () => applyRoute(parseViewFromHash());
  
  document.addEventListener("toggle", (e) => {
    if (e.target instanceof HTMLElement && e.target.matches("details.cr-more")) {
      import('./runners.js').then(m => m.updatePollStatus());
    }
  });

  // Tab Filtering Listeners
  const tabAll = document.getElementById("tabAll");
  const tabSick = document.getElementById("tabSick");
  const sizeSel = document.getElementById("pageSizeSel");

  if (tabAll) {
    tabAll.onclick = () => {
      setDashTab("all"); setDashPage(1);
      tabAll.classList.add("active"); if(tabSick) tabSick.classList.remove("active");
      if (liveCache) import('./runners.js').then(m => m.renderRows(liveCache));
    };
  }
  if (tabSick) {
    tabSick.onclick = () => {
      setDashTab("sick"); setDashPage(1);
      tabSick.classList.add("active"); if(tabAll) tabAll.classList.remove("active");
      if (liveCache) import('./runners.js').then(m => m.renderRows(liveCache));
    };
  }
  if (sizeSel) {
    sizeSel.onchange = () => {
      setDashPage(1);
      if (liveCache) import('./runners.js').then(m => m.renderRows(liveCache));
    };
  }

  // Initialization Subsystem Execution Hook Setup
  initRunnersEvents();
  initAlertsEvents();
  initSchedulerEvents();
  initModalControls();

  // Load User Meta Profiles Context
  try {
    const me = await fetch("/api/admin/me", { headers: authHeaders() });
    if (me.status === 401) {
      showSessionExpiredModal();
      return;
    }
    const meJ = await me.json();
    document.getElementById("profileInfo").textContent = "User: " + meJ.username;
    document.getElementById("userDisplay").textContent = meJ.username;
    document.getElementById("profileRole").textContent = "Role: " + meJ.role;
    document.getElementById("profileEmail").textContent = "Email: " + (meJ.email || "—");

    userRole = meJ.role || "user";
    hideRestrictedUI();

    const capFetch = await fetch("/api/admin/capabilities", { headers: authHeaders() });
    if (capFetch.status === 401) {
      showSessionExpiredModal();
      return;
    }
    const capBox = document.getElementById("cap");
    if (capBox) capBox.textContent = JSON.stringify(await capFetch.json(), null, 2);

    if (!location.hash || location.hash === "#") location.hash = "#dash";
    applyRoute(parseViewFromHash());
  } catch (e) {
    console.error("Application initialization process crashed details:", e);
  }
}

function initModalControls() {
  const btnManageAll = document.getElementById("btnManageAll");
  const modal = document.getElementById("manageAllModal");
  const btnCloseModal = document.getElementById("btnCloseModal");
  const btnRestartAll = document.getElementById("btnRestartAll");
  const btnDisableAll = document.getElementById("btnDisableAll");

  if (btnManageAll && modal) {
    btnManageAll.onclick = () => modal.classList.remove("hidden");
  }
  if (btnCloseModal && modal) {
    btnCloseModal.onclick = () => modal.classList.add("hidden");
  }

  if (btnRestartAll) {
    btnRestartAll.onclick = async () => {
      if (!liveCache || !liveCache.items) return;
      const runners = liveCache.items.map(item => item.mysql.RunnerName);
      for (const runner of runners) {
        try {
          await api(`/api/admin/bots/${encodeURIComponent(runner)}/restart-service`, { method: "POST", headers: authHeaders() });
        } catch (err) {
          if (isSessionExpiredError(err)) return;
          console.error(`Failed to restart ${runner}:`, err);
        }
      }
      alert("All machines have been restarted.");
      if (modal) modal.classList.add("hidden");
      await refreshLive();
    };
  }

  if (btnDisableAll) {
    btnDisableAll.onclick = async () => {
      if (!liveCache || !liveCache.items) return;
      const runners = liveCache.items.map(item => item.mysql.RunnerName);
      for (const runner of runners) {
        try {
          await api(`/api/admin/bots/${encodeURIComponent(runner)}/status`, { method: "PUT", headers: authHeaders(), body: JSON.stringify({ status: "DISABLE" }) });
        } catch (err) {
          if (isSessionExpiredError(err)) return;
          console.error(`Failed to disable ${runner}:`, err);
        }
      }
      alert("All machines have been disabled.");
      if (modal) modal.classList.add("hidden");
      await refreshLive();
    };
  }
}

document.addEventListener("DOMContentLoaded", initializeApp);