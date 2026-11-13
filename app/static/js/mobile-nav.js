/**
 * mobile-nav.js
 * - Hamburger drawer for mobile
 * - Clones accent swatches into the sidebar for mobile access
 * - Syncs swatch state between topbar and sidebar copies
 *
 * Add just before </body>:
 *   <script src="/static/js/mobile-nav.js"></script>
 */
(function () {
  const MOBILE_BP = 768;
  function isMobile() { return window.innerWidth <= MOBILE_BP; }

  /* ── 1. Inject backdrop overlay ─────────────────────────────── */
  const overlay = document.createElement('div');
  overlay.className = 'nav-overlay';
  overlay.id = 'navOverlay';
  document.body.appendChild(overlay);

  /* ── 2. Inject hamburger into topbar (before h1) ─────────────── */
  const topbar = document.querySelector('.app-topbar');
  const h1 = topbar && topbar.querySelector('h1');
  if (topbar && h1) {
    const btn = document.createElement('button');
    btn.id = 'btnMobileNav';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Open navigation');
    btn.textContent = '☰';
    topbar.insertBefore(btn, h1);
  }

  /* ── 3. Clone accent swatches into sidebar footer ───────────── */
  const sideNav = document.getElementById('sideNav');
  const topbarSwatches = document.querySelector('.accent-swatches');

  if (sideNav && topbarSwatches) {
    // Create a container pinned to the sidebar bottom
    const sideSwatchWrap = document.createElement('div');
    sideSwatchWrap.id = 'sideAccentSwatches';

    const label = document.createElement('span');
    label.className = 'swatch-label';
    label.textContent = 'Accent colour';
    sideSwatchWrap.appendChild(label);

    // Clone each swatch button
    topbarSwatches.querySelectorAll('.swatch').forEach(sw => {
      const clone = sw.cloneNode(true);

      // Mirror clicks: apply to the real topbar swatch & trigger its handler
      clone.addEventListener('click', () => {
        // Find the real swatch and click it (your existing core.js handles the rest)
        const real = topbarSwatches.querySelector(`.swatch[data-set="${clone.dataset.set}"]`);
        if (real) real.click();

        // Sync active class on both sets
        syncActive(clone.dataset.set);
      });

      sideSwatchWrap.appendChild(clone);
    });

    sideNav.appendChild(sideSwatchWrap);

    // Keep sidebar swatches in sync when topbar swatches change
    function syncActive(activeSet) {
      [topbarSwatches, sideSwatchWrap].forEach(wrap => {
        wrap.querySelectorAll('.swatch').forEach(s => {
          s.classList.toggle('active', s.dataset.set === activeSet);
        });
      });
    }

    // Observe topbar for active-class changes (your core.js sets them)
    const observer = new MutationObserver(() => {
      const activeSw = topbarSwatches.querySelector('.swatch.active');
      if (activeSw) syncActive(activeSw.dataset.set);
    });
    observer.observe(topbarSwatches, { subtree: true, attributeFilter: ['class'] });

    // Run once on load to set initial state
    const initialActive = topbarSwatches.querySelector('.swatch.active');
    if (initialActive) syncActive(initialActive.dataset.set);
  }

  /* ── 4. Drawer open / close ─────────────────────────────────── */
  const hamburger = document.getElementById('btnMobileNav');

  function openNav() {
    sideNav.classList.add('mobile-open');
    overlay.classList.add('open');
    if (hamburger) {
      hamburger.textContent = '✕';
      hamburger.setAttribute('aria-label', 'Close navigation');
    }
  }

  function closeNav() {
    sideNav.classList.remove('mobile-open');
    overlay.classList.remove('open');
    if (hamburger) {
      hamburger.textContent = '☰';
      hamburger.setAttribute('aria-label', 'Open navigation');
    }
  }

  if (hamburger) {
    hamburger.addEventListener('click', () => {
      sideNav.classList.contains('mobile-open') ? closeNav() : openNav();
    });
  }

  overlay.addEventListener('click', closeNav);

  // Auto-close when a nav item is tapped
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => { if (isMobile()) closeNav(); });
  });

  // Close if resized back to desktop
  window.addEventListener('resize', () => { if (!isMobile()) closeNav(); });

})();
