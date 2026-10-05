(() => {
  // View switching (Download / Activity / Library), the Activity badge, a
  // "download started" toast, and scroll-locking behind open dialogs. Purely
  // presentational: it only reads state the other scripts already render.
  const root = document.documentElement;
  const VIEWS = ["download", "activity", "library"];
  const wide = window.matchMedia("(min-width: 1100px)");
  const $ = (id) => document.getElementById(id);

  let view = root.getAttribute("data-view") || "download";
  if (!VIEWS.includes(view)) view = "download";

  // Wide screens show Activity beside the form, so it has no tab of its own.
  const shownView = () => (wide.matches && view === "activity" ? "download" : view);

  function apply({ scroll = true } = {}) {
    const shown = shownView();
    root.setAttribute("data-view", shown);
    document.querySelectorAll("[data-view-btn]").forEach((btn) => {
      btn.setAttribute("aria-current", btn.dataset.viewBtn === shown ? "page" : "false");
    });
    try { localStorage.setItem("md-view", view); } catch (_) { /* storage blocked */ }
    if (scroll) window.scrollTo({ top: 0 });
    // The release-type pill measures its segments, which have no size while
    // hidden. Re-measure right away (layout is computed on demand), and once
    // more shortly after in case web fonts shifted the segment widths.
    window.dispatchEvent(new Event("resize"));
    setTimeout(() => window.dispatchEvent(new Event("resize")), 120);
  }

  function setView(next, opts) {
    view = VIEWS.includes(next) ? next : "download";
    apply(opts);
  }

  document.querySelectorAll("[data-view-btn]").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.viewBtn));
  });
  wide.addEventListener("change", () => apply({ scroll: false }));
  apply({ scroll: false });

  // ---- Activity badge: failed-song count, or a pulse while downloading ------
  const activityTabs = document.querySelectorAll(".tab-activity");
  let wasDownloading = false;

  function refreshBadge() {
    const summary = ($("fix-summary")?.textContent || "").trim();
    const failed = Number((/^(\d+)/.exec(summary) || [])[1] || 0);
    const downloading = !!$("btn-cancel") && !$("btn-cancel").disabled;

    activityTabs.forEach((tab) => {
      tab.classList.toggle("is-running", downloading);
      tab.classList.toggle("has-failed", failed > 0);
      const badge = tab.querySelector("[data-badge]");
      if (!badge) return;
      badge.hidden = !(failed > 0);
      badge.textContent = failed > 0 ? String(failed) : "";
    });

    if (downloading && !wasDownloading && !wide.matches && shownView() !== "activity") {
      toast("Download started", "View activity", () => setView("activity"));
    }
    wasDownloading = downloading;
  }

  const watch = new MutationObserver(refreshBadge);
  const fixSummary = $("fix-summary");
  const cancelBtn = $("btn-cancel");
  if (fixSummary) watch.observe(fixSummary, { childList: true, characterData: true, subtree: true });
  if (cancelBtn) watch.observe(cancelBtn, { attributes: true, attributeFilter: ["disabled"] });
  refreshBadge();

  // ---- Toast ---------------------------------------------------------------
  let toastTimer = null;
  function toast(message, actionLabel, onAction) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.innerHTML = "";
    const text = document.createElement("span");
    text.textContent = message;
    el.appendChild(text);
    if (actionLabel && onAction) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = actionLabel;
      btn.addEventListener("click", () => {
        el.classList.remove("show");
        onAction();
      });
      el.appendChild(btn);
    }
    requestAnimationFrame(() => el.classList.add("show"));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 5000);
  }

  // ---- Lock page scroll while a dialog is open -------------------------------
  const modals = document.querySelectorAll(".modal");
  const syncModalLock = () => {
    const open = [...modals].some((m) => !m.classList.contains("hidden"));
    document.body.classList.toggle("modal-open", open);
  };
  const modalWatch = new MutationObserver(syncModalLock);
  modals.forEach((m) => modalWatch.observe(m, { attributes: true, attributeFilter: ["class"] }));
  syncModalLock();
})();
