(() => {
  const $ = (id) => document.getElementById(id);

  const themeBtn = $("btn-theme");
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("md-theme", theme); } catch (_) { /* ignore */ }
    if (themeBtn) themeBtn.textContent = theme === "dark" ? "Light" : "Dark";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0d1013" : "#dfe5ea");
  }
  applyTheme(currentTheme());
  themeBtn?.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });

  const ICON = {
    done: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3.5 8.2 6.4 11l6.1-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    skipped: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 8h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    failed: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M5 5l6 6M11 5l-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    active: "",
    pending: "",
  };

  const els = {
    url: $("url"),
    output: $("output"),
    limit: $("limit"),
    browser: $("browser"),
    force: $("force"),
    metadata: $("metadata"),
    playlistNumbers: $("playlist-numbers"),
    debug: $("debug"),
    btnDownload: $("btn-download"),
    btnCancel: $("btn-cancel"),
    btnClear: $("btn-clear"),
    btnBrowse: $("btn-browse"),
    btnDefaultFolder: $("btn-default-folder"),
    barFill: $("bar-fill"),
    fileFill: $("file-fill"),
    fileWrap: $("file-meter-wrap"),
    filePercent: $("file-percent"),
    percent: $("percent"),
    count: $("count"),
    status: $("status"),
    progressLabel: $("progress-label"),
    trackList: $("track-list"),
    trackSummary: $("track-summary"),
    log: $("log"),
    meta: $("meta"),
    modal: $("browse-modal"),
    browsePath: $("browse-path"),
    browseList: $("browse-list"),
    btnBrowseClose: $("btn-browse-close"),
    btnBrowseUp: $("btn-browse-up"),
    btnBrowseNew: $("btn-browse-new"),
    btnBrowseUse: $("btn-browse-use"),
  };

  let defaultOutputDir = "";
  let outputSeeded = false;
  let browseCurrent = "";
  let browseParent = null;
  let browseCanCreate = false;
  let logPinnedToBottom = true;

  els.log.addEventListener("scroll", () => {
    const nearBottom =
      els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 40;
    logPinnedToBottom = nearBottom;
  });

  function setRunningFields(running) {
    els.btnDownload.disabled = running || !els.url.value.trim();
    els.btnDownload.textContent = running ? "Working…" : "Download";
    els.btnCancel.disabled = !running;
    [
      els.url,
      els.output,
      els.limit,
      els.browser,
      els.force,
      els.metadata,
      els.playlistNumbers,
      els.debug,
      els.btnBrowse,
      els.btnDefaultFolder,
    ].forEach((el) => {
      el.disabled = running;
    });
    if (!running) {
      els.playlistNumbers.disabled = !els.metadata.checked;
    }
  }

  function renderTracks(tracks) {
    if (!tracks || !tracks.length) {
      els.trackList.innerHTML =
        '<li class="track-empty">Songs show up here as each one starts.</li>';
      return;
    }

    const html = tracks.map((track) => {
      const status = track.status || "pending";
      const idx = String(track.index).padStart(2, "0");
      const detail = track.detail
        ? `<div class="track-detail">${escapeHtml(track.detail)}</div>`
        : "";
      const actions =
        track.remediable
          ? `<div class="track-actions">
              <button type="button" class="btn tiny" data-fix="retry" data-index="${track.index}">Retry</button>
              <button type="button" class="btn tiny" data-fix="loose" data-index="${track.index}">Loose match</button>
              <button type="button" class="btn tiny" data-fix="paste_url" data-index="${track.index}">Paste URL</button>
              <button type="button" class="btn tiny" data-fix="skip" data-index="${track.index}">Skip</button>
            </div>`
          : "";
      return `
        <li class="track ${status}" data-index="${track.index}">
          <span class="mark">${ICON[status] || ""}</span>
          <span class="track-index">${idx}</span>
          <div class="track-body">
            <div class="track-title">${escapeHtml(track.title || "Untitled")}</div>
            ${detail}
            ${actions}
          </div>
        </li>`;
    }).join("");
    els.trackList.innerHTML = html;

    const active = els.trackList.querySelector(".track.active");
    if (active) {
      active.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  let pasteIndex = null;
  const remediateModal = $("remediate-modal");
  const remediateTrackLabel = $("remediate-track");
  const remediateUrl = $("remediate-url");
  const btnRemediateClose = $("btn-remediate-close");
  const btnRemediateGo = $("btn-remediate-go");

  function closeRemediateModal() {
    pasteIndex = null;
    remediateModal?.classList.add("hidden");
    if (remediateUrl) remediateUrl.value = "";
  }

  function openRemediateModal(index, title) {
    pasteIndex = index;
    if (remediateTrackLabel) {
      remediateTrackLabel.textContent = `#${String(index).padStart(2, "0")} · ${title}`;
    }
    if (remediateUrl) remediateUrl.value = "";
    remediateModal?.classList.remove("hidden");
    remediateUrl?.focus();
  }

  async function remediate(trackIndex, action, youtubeUrl) {
    const body = {
      track_index: trackIndex,
      action,
      youtube_url: youtubeUrl || null,
      browser: els.browser.value,
    };
    const res = await fetch("/api/remediate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || `Remediation failed (${res.status})`);
      await refresh();
      return;
    }
    const data = await res.json();
    applyState(data.status);
  }

  els.trackList.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-fix]");
    if (!btn) return;
    const action = btn.getAttribute("data-fix");
    const index = Number(btn.getAttribute("data-index"));
    if (!action || !index) return;
    if (action === "paste_url") {
      const row = btn.closest(".track");
      const title =
        row?.querySelector(".track-title")?.textContent?.trim() || `Track ${index}`;
      openRemediateModal(index, title);
      return;
    }
    remediate(index, action).catch((e) => alert(e.message));
  });

  function bindBackdropClose(modal, onClose) {
    if (!modal) return;
    let pressedOnBackdrop = false;
    modal.addEventListener("pointerdown", (ev) => {
      pressedOnBackdrop = ev.target === modal;
    });
    modal.addEventListener("click", (ev) => {
      if (pressedOnBackdrop && ev.target === modal) onClose();
      pressedOnBackdrop = false;
    });
  }

  btnRemediateClose?.addEventListener("click", closeRemediateModal);
  bindBackdropClose(remediateModal, closeRemediateModal);
  btnRemediateGo?.addEventListener("click", async () => {
    const url = remediateUrl?.value.trim();
    const index = pasteIndex;
    if (!index || !url) return;
    closeRemediateModal();
    await remediate(index, "paste_url", url);
  });
  remediateUrl?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      btnRemediateGo?.click();
    }
  });

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function summarize(tracks) {
    if (!tracks || !tracks.length) return "Waiting";
    const counts = { done: 0, skipped: 0, failed: 0, active: 0 };
    tracks.forEach((t) => {
      if (counts[t.status] != null) counts[t.status] += 1;
    });
    const parts = [];
    if (counts.done) parts.push(`${counts.done} saved`);
    if (counts.skipped) parts.push(`${counts.skipped} skipped`);
    if (counts.failed) parts.push(`${counts.failed} failed`);
    if (counts.active) parts.push("1 running");
    return parts.join(" · ") || `${tracks.length} queued`;
  }

  function applyState(s) {
    const running = !!s.running;
    const total = s.total || 0;
    const completed = s.completed || 0;
    const percent = s.percent ?? 0;
    const filePercent = s.file_percent ?? 0;

    els.progressLabel.textContent = running
      ? "Downloading"
      : percent >= 100
        ? "Finished"
        : "Ready";
    els.status.textContent = s.status || (running ? "Working…" : "Paste a link to start");
    els.count.textContent = `${completed} / ${total || "–"}`;
    els.percent.textContent = `${percent}%`;
    els.barFill.style.width = `${Math.max(0, Math.min(100, percent))}%`;

    if (running && filePercent > 0 && filePercent < 100) {
      els.fileWrap.classList.remove("hidden");
      els.fileFill.style.width = `${filePercent}%`;
      els.filePercent.textContent = `${filePercent}%`;
    } else {
      els.fileWrap.classList.add("hidden");
    }

    setRunningFields(running);

    if (s.default_output_dir) defaultOutputDir = s.default_output_dir;
    if (!outputSeeded && s.output_dir) {
      els.output.value = s.output_dir;
      outputSeeded = true;
    }
    if (s.url && !els.url.value) els.url.value = s.url;

    const bits = [];
    if (s.collection) bits.push(s.collection);
    if (s.docker && s.browse_root) {
      bits.push(`Host folder mounted at ${s.browse_root}`);
    } else if (s.output_dir) {
      bits.push(s.output_dir);
    }
    els.meta.textContent = bits.join(" · ");

    const saveHint = document.getElementById("save-hint");
    if (saveHint) {
      saveHint.textContent = s.docker
        ? "Inside the downloads folder on this PC"
        : "Folder on this PC";
    }

    renderTracks(s.tracks || []);
    els.trackSummary.textContent = summarize(s.tracks || []);

    if (Array.isArray(s.log)) {
      els.log.textContent = s.log.length ? s.log.join("\n") : "";
      if (logPinnedToBottom) els.log.scrollTop = els.log.scrollHeight;
    }
  }

  async function refresh() {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`status ${res.status}`);
    applyState(await res.json());
  }

  function connectEvents() {
    const es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      try {
        applyState(JSON.parse(ev.data));
      } catch (_) {
        /* ignore */
      }
    };
    es.onerror = () => {
      es.close();
      setTimeout(connectEvents, 1500);
      refresh().catch(() => {});
    };
  }

  async function loadBrowse(path) {
    const qs = path == null || path === ""
      ? ""
      : `?path=${encodeURIComponent(path)}`;
    const res = await fetch(`/api/browse${qs}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || `Browse failed (${res.status})`);
      return;
    }
    browseCurrent = data.path || "";
    browseParent = data.parent;
    browseCanCreate = !!data.can_create && !!data.path;
    const rootLabel = data.browse_root
      ? (data.is_root ? `Downloads (${data.browse_root})` : data.path)
      : (data.is_root ? "Drives" : (data.path || "(unknown)"));
    els.browsePath.textContent = rootLabel;
    // parent === null means top of browse tree; "" means Windows drive list.
    els.btnBrowseUp.disabled = data.parent == null;
    els.btnBrowseUse.disabled = !data.path;
    els.btnBrowseNew.disabled = !browseCanCreate;

    els.browseList.innerHTML = "";
    (data.entries || []).forEach((entry) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = entry.name;
      btn.addEventListener("click", () => loadBrowse(entry.path));
      li.appendChild(btn);
      els.browseList.appendChild(li);
    });
    if (!(data.entries || []).length) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.disabled = true;
      btn.textContent = "(empty — use New folder)";
      li.appendChild(btn);
      els.browseList.appendChild(li);
    }
  }

  async function createFolder() {
    if (!browseCanCreate || !browseCurrent) {
      alert("Open a folder first, then create a subfolder.");
      return;
    }
    const ask = window.mdAskName;
    const name = ask
      ? await ask({
          title: "New folder",
          label: "Folder name",
          detail: browseCurrent,
          value: "",
          confirmLabel: "Create",
        })
      : window.prompt("New folder name");
    if (!name || !name.trim()) return;
    const res = await fetch("/api/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent: browseCurrent, name: name.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || `Could not create folder (${res.status})`);
      return;
    }
    await loadBrowse(browseCurrent);
  }

  function openBrowse() {
    els.modal.classList.remove("hidden");
    loadBrowse(els.output.value.trim() || defaultOutputDir || null);
  }

  function closeBrowse() {
    els.modal.classList.add("hidden");
  }

  els.url.addEventListener("input", () => {
    els.btnDownload.disabled =
      els.btnDownload.textContent.startsWith("Working") || !els.url.value.trim();
  });

  els.metadata.addEventListener("change", () => {
    els.playlistNumbers.disabled =
      els.btnDownload.textContent.startsWith("Working") || !els.metadata.checked;
  });
  els.playlistNumbers.disabled = !els.metadata.checked;

  els.btnDownload.addEventListener("click", async () => {
    const url = els.url.value.trim();
    if (!url) return;
    const limitRaw = els.limit.value.trim();
    const body = {
      url,
      output_dir: els.output.value.trim() || null,
      force: els.force.checked,
      embed_metadata: els.metadata.checked,
      playlist_track_numbers: els.metadata.checked && els.playlistNumbers.checked,
      debug: els.debug.checked,
      browser: els.browser.value,
      limit: limitRaw ? Number(limitRaw) : null,
    };
    els.btnDownload.disabled = true;
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || `Failed to start (${res.status})`);
      await refresh();
      return;
    }
    const data = await res.json();
    if (data.status?.output_dir) {
      els.output.value = data.status.output_dir;
      outputSeeded = true;
    }
    applyState(data.status);
  });

  els.btnCancel.addEventListener("click", async () => {
    await fetch("/api/cancel", { method: "POST" });
  });

  els.btnClear.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    els.log.textContent = "";
  });

  els.btnBrowse.addEventListener("click", openBrowse);
  els.btnBrowseClose.addEventListener("click", closeBrowse);
  els.btnBrowseNew.addEventListener("click", createFolder);
  bindBackdropClose(els.modal, closeBrowse);
  els.btnBrowseUp.addEventListener("click", () => {
    if (browseParent == null || browseParent === "") loadBrowse(null);
    else loadBrowse(browseParent);
  });
  els.btnBrowseUse.addEventListener("click", () => {
    if (!browseCurrent) return;
    els.output.value = browseCurrent;
    outputSeeded = true;
    closeBrowse();
  });
  els.btnDefaultFolder.addEventListener("click", () => {
    if (defaultOutputDir) {
      els.output.value = defaultOutputDir;
      outputSeeded = true;
    }
  });

  refresh().catch((e) => {
    els.status.textContent = `Cannot reach server: ${e.message}`;
  });
  connectEvents();
})();
