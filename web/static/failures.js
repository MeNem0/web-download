(() => {
  // Failure log panel: every failed song, each retry, and how it ended. The
  // server writes the log (failures.jsonl); this only reads and displays it.
  const $ = (id) => document.getElementById(id);
  const panel = $("failure-panel");
  const list = $("failure-list");
  if (!panel || !list) return;

  const FILTERS = {
    key: "failed,recovered,retry_failed,gave_up,skipped",
    fail: "failed,retry_failed,gave_up",
    ok: "recovered",
    all: "",
  };
  const LABEL = {
    failed: "Failed",
    retry_start: "Retrying",
    recovered: "Recovered",
    retry_failed: "Retry failed",
    gave_up: "Gave up",
    skipped: "Skipped",
  };

  let filter = "key";
  let timer = null;
  let loading = false;

  const esc = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  function ago(seconds) {
    const diff = Math.max(0, Date.now() / 1000 - seconds);
    if (diff < 45) return "just now";
    if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
    return `${Math.round(diff / 86400)} d ago`;
  }

  function row(entry) {
    const where = [entry.artist, entry.album].filter(Boolean).join(" / ");
    const number = entry.index ? `#${String(entry.index).padStart(2, "0")} ` : "";
    const tags = [];
    if (entry.round && entry.rounds) tags.push(`round ${entry.round}/${entry.rounds}`);
    else if (entry.action && entry.action !== "auto") tags.push(entry.action.replace("_", " "));
    if (entry.attempts) tags.push(`${entry.attempts} attempts`);
    const message = entry.error || entry.detail || "";
    const methods = entry.methods?.length ? `Methods tried: ${entry.methods.join(", ")}` : "";
    const when = new Date(entry.ts * 1000).toLocaleString();
    return `
      <li class="failure-item is-${esc(entry.event)}">
        <span class="failure-dot" aria-hidden="true"></span>
        <div class="failure-main">
          <div class="failure-head">
            <span class="failure-badge">${esc(LABEL[entry.event] || entry.event)}</span>
            <span class="failure-title">${esc(number)}${esc(entry.title || "Untitled")}</span>
          </div>
          ${where ? `<div class="failure-where">${esc(where)}</div>` : ""}
          ${message ? `<div class="failure-msg">${esc(message)}</div>` : ""}
          ${methods ? `<div class="failure-where" title="${esc(methods)}">${esc(methods)}</div>` : ""}
        </div>
        <div class="failure-meta" title="${esc(when)}">
          <span>${esc(ago(entry.ts))}</span>
          ${tags.length ? `<span>${esc(tags.join(" · "))}</span>` : ""}
        </div>
      </li>`;
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const res = await fetch(`/api/failures?limit=150&event=${encodeURIComponent(FILTERS[filter])}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Could not load the log");

      const counts = data.counts || {};
      const shown = (counts.total || 0) - (counts.retry_start || 0);
      const pill = $("failure-count");
      if (pill) {
        pill.hidden = shown <= 0;
        pill.textContent = String(shown);
      }
      const where = $("failure-path");
      if (where) where.textContent = data.path ? `Saved to ${data.path}` : "";

      const entries = data.entries || [];
      list.innerHTML = entries.length
        ? entries.map(row).join("")
        : '<li class="track-empty">Nothing logged yet.</li>';
    } catch (err) {
      list.innerHTML = `<li class="track-empty">${esc(err.message || "Could not load the log")}</li>`;
    } finally {
      loading = false;
    }
  }

  function startPolling() {
    stopPolling();
    timer = setInterval(load, 6000);
  }

  function stopPolling() {
    clearInterval(timer);
    timer = null;
  }

  panel.addEventListener("toggle", () => {
    if (panel.open) {
      load();
      startPolling();
    } else {
      stopPolling();
    }
  });

  document.querySelectorAll("[data-failure-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      filter = btn.dataset.failureFilter;
      document
        .querySelectorAll("[data-failure-filter]")
        .forEach((b) => b.classList.toggle("is-active", b === btn));
      load();
    });
  });

  $("btn-failure-refresh")?.addEventListener("click", load);

  $("btn-failure-clear")?.addEventListener("click", async () => {
    if (!window.confirm("Clear the whole failure log? This can't be undone.")) return;
    await fetch("/api/failures/clear", { method: "POST" });
    load();
  });

  // A change in the Needs attention summary means something just failed,
  // retried, or recovered, so refresh the count (and the list if it's open).
  const summary = $("fix-summary");
  if (summary) {
    new MutationObserver(() => load()).observe(summary, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  load();
})();
