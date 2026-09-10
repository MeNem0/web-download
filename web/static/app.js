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
    artist: $("artist"),
    album: $("album"),
    albumArtist: $("album-artist"),
    albumLabel: $("album-label"),
    releaseHint: $("release-hint"),
    releaseGroup: document.querySelector(".release-type"),
    releaseIndicator: $("release-indicator"),
    pathPreview: $("path-preview"),
    jobCoverRow: $("job-cover-row"),
    jobCoverTile: $("job-cover-tile"),
    jobCoverPreview: $("job-cover-preview"),
    jobCoverFile: $("job-cover-file"),
    jobCoverUrl: $("job-cover-url"),
    btnJobCoverClear: $("btn-job-cover-clear"),
    artistList: $("artist-list"),
    albumSuggestPanel: $("album-suggest"),
    albumSuggestList: $("album-suggest-list"),
    albumSuggestStatus: $("album-suggest-status"),
    albumMatchSpinner: $("album-match-spinner"),
    albumMatchHint: $("album-match-hint"),
    btnNewArtist: $("btn-new-artist"),
    limit: $("limit"),
    stripTerms: $("strip-terms"),
    verifyArtists: $("verify-artists"),
    verifyArtistsWrap: $("verify-artists-wrap"),
    tracks: $("tracks"),
    tracksHint: $("tracks-hint"),
    browser: $("browser"),
    force: $("force"),
    metadata: $("metadata"),
    playlistNumbers: $("playlist-numbers"),
    debug: $("debug"),
    btnQueue: $("btn-queue"),
    btnDownload: $("btn-download"),
    reviewModal: $("review-modal"),
    reviewSub: $("review-sub"),
    reviewArt: $("review-art"),
    reviewAlbumName: $("review-album-name"),
    reviewCounts: $("review-counts"),
    reviewAlbumArtist: $("review-album-artist"),
    reviewAlbumQ: $("review-album-q"),
    reviewAlbumResults: $("review-album-results"),
    reviewRows: $("review-rows"),
    reviewStatus: $("review-status"),
    reviewHint: $("review-hint"),
    reviewUseAlbumMeta: $("review-use-album-meta"),
    btnReviewClose: $("btn-review-close"),
    btnReviewConfirm: $("btn-review-confirm"),
    btnReviewAlbumSearch: $("btn-review-album-search"),
    btnReviewAll: $("btn-review-all"),
    btnReviewMatched: $("btn-review-matched"),
    btnReviewPlaylist: $("btn-review-playlist"),
    btnReviewNone: $("btn-review-none"),
    btnCancel: $("btn-cancel"),
    btnClear: $("btn-clear"),
    btnFixContinue: $("btn-fix-continue"),
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
    fixPanel: document.querySelector(".fix-panel"),
    fixList: $("fix-list"),
    fixSummary: $("fix-summary"),
    fixHint: $("fix-hint"),
    queueList: $("queue-list"),
    queueHistory: $("queue-history"),
    queueCurrent: $("queue-current"),
    log: $("log"),
    meta: $("meta"),
    ffmpegWarning: $("ffmpeg-warning"),
    ytdlpVersion: $("ytdlp-version"),
    btnYtdlpUpdate: $("btn-ytdlp-update"),
  };

  let coverPath = null;
  let coverObjectUrl = null;
  let logPinnedToBottom = true;
  let lastVersion = -1;
  let queueSubmitting = false;
  let lastDownloadBusy = false;
  let kickTimer = null;

  // The exact catalog release the user picked from the album matcher, if any.
  // Kept only while the Album field still matches what was picked — editing
  // it afterwards means the pick no longer applies, so it's cleared.
  let selectedCollectionId = null;
  let selectedCollectionLabel = "";

  els.log?.addEventListener("scroll", () => {
    const nearBottom =
      els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 40;
    logPinnedToBottom = nearBottom;
  });

  const VARIOUS_ARTISTS = "Various Artists";

  // Auto-matched values keep refreshing as you pick a different album, but
  // stop the moment you type your own album artist.
  let albumArtistTouched = false;

  function autoFillAlbumArtist(value) {
    const next = (value || "").trim();
    if (!next || albumArtistTouched || !els.albumArtist) return;
    if (els.albumArtist.value.trim() === next) return;
    els.albumArtist.value = next;
    updatePathPreview();
  }

  function releaseType() {
    const value = document.querySelector('input[name="release-type"]:checked')?.value;
    return value === "single" || value === "compilation" ? value : "album";
  }

  function isCompilation() {
    return releaseType() === "compilation";
  }

  // The pill behind the checked release-type segment; sized/positioned in
  // JS (rather than one CSS rule per segment) so it can genuinely slide and
  // resize between segments of different widths instead of just swapping.
  function positionReleaseIndicator({ animate = true } = {}) {
    const group = els.releaseGroup;
    const indicator = els.releaseIndicator;
    if (!group || !indicator) return;
    const checked = group.querySelector('input[name="release-type"]:checked');
    const seg = checked?.closest(".seg");
    if (!seg) return;
    const groupRect = group.getBoundingClientRect();
    const segRect = seg.getBoundingClientRect();
    if (!animate) indicator.classList.add("no-anim");
    indicator.style.width = `${segRect.width}px`;
    indicator.style.transform = `translateX(${segRect.left - groupRect.left}px)`;
    if (!animate) {
      // Commit the jump before re-enabling the transition, so the next
      // (real) change is the first thing that actually animates.
      void indicator.offsetWidth;
      indicator.classList.remove("no-anim");
    }
  }

  function applyReleaseType() {
    const compilation = isCompilation();
    if (els.albumLabel) {
      els.albumLabel.textContent = compilation
        ? "Compilation name"
        : releaseType() === "single"
          ? "Single name"
          : "Album name";
    }
    if (els.releaseHint) els.releaseHint.hidden = !compilation;
    // Online artist matching only has an effect when artists vary per track.
    els.verifyArtistsWrap?.classList.toggle("hidden", !compilation);
    if (compilation) {
      if (!els.artist.value) els.artist.value = VARIOUS_ARTISTS;
      autoFillAlbumArtist(els.artist.value || VARIOUS_ARTISTS);
    } else if (els.albumArtist?.value.trim() === VARIOUS_ARTISTS && !albumArtistTouched) {
      els.albumArtist.value = "";
    }
    positionReleaseIndicator();
    updatePathPreview();
    updateActionButtons({ downloading: lastDownloadBusy });
  }

  function updatePathPreview() {
    const artist = els.artist.value.trim();
    const album = els.album.value.trim();
    if (!artist || !album) {
      els.pathPreview.textContent = "Choose artist and album";
      return;
    }
    els.pathPreview.textContent = `${artist} / ${album}`;
  }

  function setCoverPreview(url, { objectUrl = false } = {}) {
    if (coverObjectUrl && coverObjectUrl !== url) {
      URL.revokeObjectURL(coverObjectUrl);
      coverObjectUrl = null;
    }
    if (objectUrl && url) coverObjectUrl = url;
    else if (!url) coverObjectUrl = null;

    const img = els.jobCoverPreview;
    const tile = els.jobCoverTile;
    if (!img) return;

    img.onload = () => {
      img.hidden = false;
      tile?.classList.add("has-image");
    };
    img.onerror = () => {
      if (coverObjectUrl && coverObjectUrl === img.src) {
        URL.revokeObjectURL(coverObjectUrl);
        coverObjectUrl = null;
      }
      img.removeAttribute("src");
      img.hidden = true;
      tile?.classList.remove("has-image");
    };

    if (url) {
      img.hidden = false;
      tile?.classList.add("has-image");
      img.src = url;
    } else {
      img.removeAttribute("src");
      img.hidden = true;
      tile?.classList.remove("has-image");
    }
  }

  function clearJobCover() {
    coverPath = null;
    if (els.jobCoverFile) els.jobCoverFile.value = "";
    if (els.jobCoverUrl) els.jobCoverUrl.value = "";
    setCoverPreview("");
  }

  // ---- Album matcher, integrated directly into the Album/Artist fields ----
  // Typing an album (optionally with an artist) live-searches the same
  // catalog the review step uses, so picking a hit fills album, artist,
  // album artist, and cover art together — and hands the review step the
  // exact release id instead of making it re-guess from typed text.

  function setAlbumSuggestStatus(message, { error = false } = {}) {
    const el = els.albumSuggestStatus;
    if (!el) return;
    if (!message) {
      el.textContent = "";
      el.classList.add("hidden");
      el.classList.remove("is-error");
      return;
    }
    el.textContent = message;
    el.classList.toggle("is-error", !!error);
    el.classList.remove("hidden");
  }

  function setAlbumMatchHint(text) {
    if (!els.albumMatchHint) return;
    if (!text) {
      els.albumMatchHint.classList.add("hidden");
      els.albumMatchHint.textContent = "";
      return;
    }
    els.albumMatchHint.textContent = text;
    els.albumMatchHint.classList.remove("hidden");
  }

  function closeAlbumSuggest() {
    els.albumSuggestPanel?.classList.add("hidden");
    els.album?.setAttribute("aria-expanded", "false");
  }

  function openAlbumSuggest() {
    els.albumSuggestPanel?.classList.remove("hidden");
    els.album?.setAttribute("aria-expanded", "true");
  }

  function clearAlbumSuggest() {
    if (els.albumSuggestList) els.albumSuggestList.innerHTML = "";
    setAlbumSuggestStatus("");
    closeAlbumSuggest();
  }

  function forgetSelectedCollection() {
    selectedCollectionId = null;
    selectedCollectionLabel = "";
    setAlbumMatchHint("");
  }

  function albumMatchQuery() {
    const artist = isCompilation() ? "" : els.artist?.value.trim() || "";
    const album = els.album?.value.trim() || "";
    return [artist, album].filter(Boolean).join(" ").trim();
  }

  // Normalizes hits from either endpoint into one shape the dropdown renders.
  function renderAlbumSuggestHits(hits, { onPick } = {}) {
    const list = els.albumSuggestList;
    if (!list) return;
    list.innerHTML = "";
    for (const hit of hits) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "suggest-hit";
      btn.setAttribute("role", "option");
      const kindLabel = hit.kind === "song" ? "Single" : "Album";
      const bits = [hit.artist, hit.year, kindLabel];
      if (hit.track_count) bits.push(`${hit.track_count} tracks`);
      if (hit.various_artists) bits.push("Various artists");
      const art = String(hit.artwork_url || "").replaceAll('"', "&quot;");
      btn.innerHTML = `
        <span class="suggest-art">${
          art ? `<img alt="" loading="lazy" src="${art}" />` : ""
        }</span>
        <span class="suggest-body">
          <span class="suggest-title">${escapeHtml(hit.title || "")}</span>
          <span class="suggest-meta">${escapeHtml(bits.filter(Boolean).join(" · "))}</span>
        </span>
      `;
      btn.addEventListener("click", () => onPick?.(hit));
      li.appendChild(btn);
      list.appendChild(li);
    }
    openAlbumSuggest();
  }

  async function applyAlbumSuggestHit(hit) {
    if (!hit) return;
    if (hit.title && els.album) {
      els.album.value = hit.title;
      updatePathPreview();
    }
    // A compilation is an explicit choice, so a pick must not silently
    // downgrade it to a normal album — but the catalog crediting it to
    // "Various Artists" should switch a plain Album pick into one.
    if (hit.various_artists) {
      const radio = document.querySelector('input[name="release-type"][value="compilation"]');
      if (radio && !radio.checked) {
        radio.checked = true;
        radio.dispatchEvent(new Event("change", { bubbles: true }));
      }
      els.artist.value = VARIOUS_ARTISTS;
    } else if (!isCompilation() && hit.artist) {
      els.artist.value = hit.artist;
      if (hit.kind === "song") {
        const single = document.querySelector('input[name="release-type"][value="single"]');
        if (single && !single.checked) {
          single.checked = true;
          single.dispatchEvent(new Event("change", { bubbles: true }));
        }
      } else if (hit.kind === "album") {
        const albumRadio = document.querySelector('input[name="release-type"][value="album"]');
        if (albumRadio && !albumRadio.checked) {
          albumRadio.checked = true;
          albumRadio.dispatchEvent(new Event("change", { bubbles: true }));
        }
      }
    }
    if (hit.artist) autoFillAlbumArtist(hit.various_artists ? VARIOUS_ARTISTS : hit.artist);
    if (els.jobCoverUrl && hit.artwork_url) {
      els.jobCoverUrl.value = hit.artwork_url;
      await previewCoverUrl();
    }
    // Only a real catalog album (not a bare song hit) carries an id the
    // review step can match a tracklist against.
    if (hit.isAlbum && hit.id) {
      selectedCollectionId = hit.id;
      selectedCollectionLabel = hit.title;
      setAlbumMatchHint(`Catalog match locked · reviewing will compare against “${hit.title}”`);
    } else {
      forgetSelectedCollection();
    }
    clearAlbumSuggest();
    updatePathPreview();
    updateActionButtons({ downloading: lastDownloadBusy });
  }

  let albumMatchSeq = 0;
  let albumMatchTimer = null;

  async function runAlbumMatchSearch() {
    const q = albumMatchQuery();
    if (q.length < 2) {
      clearAlbumSuggest();
      return;
    }
    const seq = ++albumMatchSeq;
    const single = releaseType() === "single";
    els.albumMatchSpinner?.removeAttribute("hidden");
    try {
      let hits = [];
      let failed = false;
      if (single) {
        const res = await fetch(
          `/api/download/cover/search?q=${encodeURIComponent(q)}&entity=song&limit=8`
        );
        const data = await res.json().catch(() => ({}));
        failed = !res.ok;
        hits = res.ok
          ? (data.results || []).map((r) => ({
              id: r.id,
              kind: r.kind,
              title: r.album || r.title || "",
              artist: r.artist || "",
              year: r.year || "",
              artwork_url: r.artwork_url || "",
              isAlbum: false,
            }))
          : [];
      } else {
        const res = await fetch(`/api/album/search?q=${encodeURIComponent(q)}&limit=8`);
        const data = await res.json().catch(() => ({}));
        failed = !res.ok;
        hits = res.ok
          ? (data.results || []).map((r) => ({
              id: r.id,
              kind: "album",
              title: r.name || "",
              artist: r.artist || "",
              year: r.year || "",
              artwork_url: r.artwork_url || "",
              track_count: r.track_count || 0,
              various_artists: !!r.various_artists,
              isAlbum: true,
            }))
          : [];
      }
      if (seq !== albumMatchSeq) return; // a newer keystroke already moved on
      if (failed) {
        setAlbumSuggestStatus("Catalog search failed.", { error: true });
        if (els.albumSuggestList) els.albumSuggestList.innerHTML = "";
        openAlbumSuggest();
        return;
      }
      if (!hits.length) {
        setAlbumSuggestStatus("No catalog matches — keep your own typed title.");
        if (els.albumSuggestList) els.albumSuggestList.innerHTML = "";
        openAlbumSuggest();
        return;
      }
      setAlbumSuggestStatus("");
      renderAlbumSuggestHits(hits, { onPick: applyAlbumSuggestHit });
    } catch (err) {
      if (seq !== albumMatchSeq) return;
      setAlbumSuggestStatus(err.message || "Catalog search failed.", { error: true });
      openAlbumSuggest();
    } finally {
      if (seq === albumMatchSeq) els.albumMatchSpinner?.setAttribute("hidden", "");
    }
  }

  function scheduleAlbumMatchSearch(delay = 320) {
    clearTimeout(albumMatchTimer);
    albumMatchTimer = setTimeout(runAlbumMatchSearch, delay);
  }

  function assignCoverFileInput(file) {
    if (!els.jobCoverFile || !file) return;
    try {
      const dt = new DataTransfer();
      dt.items.add(file);
      els.jobCoverFile.files = dt.files;
    } catch (_) {
      /* some browsers block programmatic file assignment */
    }
  }

  async function ingestCoverFile(file) {
    if (!file || !String(file.type || "").startsWith("image/")) return false;
    coverPath = null;
    if (els.jobCoverUrl) els.jobCoverUrl.value = "";
    assignCoverFileInput(file);
    setCoverPreview(URL.createObjectURL(file), { objectUrl: true });
    try {
      const form = new FormData();
      form.append("file", file, file.name || "cover.png");
      const res = await fetch("/api/download/cover", { method: "POST", body: form });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.path) {
        coverPath = data.path;
        if (data.preview_url) setCoverPreview(data.preview_url);
      }
    } catch (_) {
      /* blob preview still works; buildBody can upload from the file input */
    }
    return true;
  }

  function imageFromClipboard(ev) {
    const items = ev.clipboardData?.items;
    if (!items) return null;
    for (const item of items) {
      if (item.kind === "file" && String(item.type || "").startsWith("image/")) {
        return item.getAsFile();
      }
    }
    return null;
  }

  async function loadArtists(selectName) {
    const res = await fetch("/api/library/artists");
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Could not load artists");
    if (els.artistList) {
      const options = [`<option value="${escapeHtml(VARIOUS_ARTISTS)}"></option>`];
      for (const a of data.artists || []) {
        options.push(`<option value="${escapeHtml(a.name)}"></option>`);
      }
      els.artistList.innerHTML = options.join("");
    }
    if (selectName) els.artist.value = selectName;
    updatePathPreview();
  }

  function formReady() {
    return !!(
      els.url?.value.trim() &&
      els.artist?.value &&
      els.album?.value.trim()
    );
  }

  function updateActionButtons({ downloading = false } = {}) {
    const ready = formReady();
    const busySubmit = queueSubmitting;
    // Download/Queue stay available whenever the form is valid — a running
    // job or fix must never permanently lock the primary actions.
    if (els.btnDownload) {
      els.btnDownload.disabled = !ready || busySubmit;
      els.btnDownload.textContent = busySubmit ? "Queuing…" : "Review & queue";
    }
    if (els.btnQueue) {
      els.btnQueue.disabled = !ready || busySubmit;
    }
    if (els.btnCancel) {
      els.btnCancel.disabled = !downloading;
    }
  }

  function setRunningFields(running) {
    const downloading = !!running;
    [
      els.url,
      els.artist,
      els.album,
      els.albumArtist,
      els.limit,
      els.tracks,
      els.browser,
      els.force,
      els.metadata,
      els.playlistNumbers,
      els.debug,
      els.stripTerms,
      els.verifyArtists,
      els.btnNewArtist,
      els.jobCoverFile,
      els.jobCoverUrl,
    ].forEach((el) => {
      if (el) el.disabled = false;
    });
    document.querySelectorAll('input[name="release-type"]').forEach((el) => {
      el.disabled = false;
    });
    if (els.playlistNumbers) {
      els.playlistNumbers.disabled = !els.metadata?.checked;
    }
    updateActionButtons({ downloading });
  }

  function renderTracks(tracks) {
    if (!tracks || !tracks.length) {
      els.trackList.innerHTML =
        '<li class="track-empty">Songs show up here once the playlist is read.</li>';
      return;
    }

    const html = tracks.map((track) => {
      const status = track.status || "pending";
      const idx = String(track.index).padStart(2, "0");
      const detail = track.detail
        ? `<div class="track-detail">${escapeHtml(track.detail)}</div>`
        : status === "pending"
          ? '<div class="track-detail">Queued</div>'
          : "";
      const actions = track.skippable
        ? `<div class="track-side">
            <button type="button" class="btn tiny" data-skip-track="${track.index}">Skip</button>
          </div>`
        : `<div class="track-side" aria-hidden="true"></div>`;
      return `
        <li class="track ${status}" data-index="${track.index}">
          <span class="mark">${ICON[status] || ""}</span>
          <span class="track-index">${idx}</span>
          <div class="track-body">
            <div class="track-title">${escapeHtml(track.title || "Untitled")}</div>
            ${detail}
          </div>
          ${actions}
        </li>`;
    }).join("");
    els.trackList.innerHTML = html;

    keepActiveTrackVisible();
  }

  // Follow the song being downloaded inside the list's own scroll box.
  // scrollIntoView would also scroll every ancestor, which yanked the whole
  // page down to the song list each time a track finished.
  function keepActiveTrackVisible() {
    const list = els.trackList;
    const active = list?.querySelector(".track.active");
    if (!active) return;
    const listBox = list.getBoundingClientRect();
    const trackBox = active.getBoundingClientRect();
    let delta = 0;
    if (trackBox.top < listBox.top) {
      delta = trackBox.top - listBox.top;
    } else if (trackBox.bottom > listBox.bottom) {
      delta = trackBox.bottom - listBox.bottom;
    }
    if (delta) list.scrollTo({ top: list.scrollTop + delta, behavior: "smooth" });
  }

  async function skipQueuedTrack(trackIndex) {
    const res = await fetch("/api/tracks/skip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_index: trackIndex }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Could not skip track");
      await refresh();
      return;
    }
    applyState(data.status);
  }

  function renderFixPanel(s) {
    if (!els.fixList) return;
    const fixes = s.fixes || [];
    const failedCount = fixes.filter((t) => t.status === "failed").length;
    const busyCount = fixes.filter((t) => t.status === "fixing").length;

    els.fixPanel?.classList.toggle("has-failures", fixes.length > 0);
    if (els.fixSummary) {
      if (busyCount && !failedCount) els.fixSummary.textContent = "Fixing…";
      else if (failedCount) els.fixSummary.textContent = `${failedCount} to fix`;
      else els.fixSummary.textContent = "None";
    }
    if (els.fixHint) {
      els.fixHint.textContent = fixes.length
        ? "Queue keeps moving. Fixes still use each track’s original album folder, tags, and cover."
        : "Failed songs stay here while the queue keeps going.";
    }

    if (!fixes.length) {
      els.fixList.innerHTML = '<li class="track-empty">No failed tracks</li>';
    } else {
      els.fixList.innerHTML = fixes.map((track) => {
        const busy = track.status === "fixing";
        const idx = String(track.track_index ?? "").padStart(2, "0");
        const albumPath = [track.artist, track.album].filter(Boolean).join(" / ");
        let actions;
        if (busy) {
          actions = `<div class="fix-item-meta">Fixing…</div>`;
        } else if (track.remediable) {
          actions = `<div class="fix-actions">
              <button type="button" class="btn tiny" data-fix="retry" data-fix-id="${escapeHtml(track.id)}">Retry</button>
              <button type="button" class="btn tiny" data-fix="loose" data-fix-id="${escapeHtml(track.id)}">Loose</button>
              <button type="button" class="btn tiny" data-fix="paste_url" data-fix-id="${escapeHtml(track.id)}">Paste URL</button>
              <button type="button" class="btn tiny" data-fix="skip" data-fix-id="${escapeHtml(track.id)}">Skip</button>
            </div>`;
        } else if (s.remediating) {
          actions = `<div class="fix-item-meta">Another fix is running</div>`;
        } else {
          actions = `<div class="fix-item-meta">Waiting for track info…</div>`;
        }
        return `
          <li class="fix-item${busy ? " is-busy" : ""}" data-fix-id="${escapeHtml(track.id || "")}">
            <div class="fix-item-title">#${idx} ${escapeHtml(track.title || "Untitled")}</div>
            <div class="fix-item-detail">${escapeHtml(track.detail || "Failed")}</div>
            ${albumPath ? `<div class="fix-item-meta">${escapeHtml(albumPath)}</div>` : ""}
            ${actions}
          </li>`;
      }).join("");
    }

    const canSkipAll =
      !s.remediating && fixes.some((t) => t.status === "failed" && t.remediable);
    if (els.btnFixContinue) els.btnFixContinue.disabled = !canSkipAll;
  }

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

  function renderQueue(s) {
    const current = s.current;
    if (!current || (!s.running && !current.label && !s.downloading)) {
      els.queueCurrent.innerHTML = '<p class="track-empty">No active job</p>';
    } else {
      const pct = current.percent ?? s.percent ?? 0;
      els.queueCurrent.innerHTML = `
        <div class="queue-item active">
          <div class="queue-item-title">${escapeHtml(current.label || "Current job")}</div>
          <div class="queue-item-sub">${escapeHtml(current.status_text || s.status || "")}</div>
          <div class="queue-meter"><div style="width:${pct}%"></div></div>
          <div class="queue-item-meta">${pct}% · ${current.completed || 0}/${current.total || "–"}</div>
        </div>`;
    }

    const pending = s.queue || [];
    if (!pending.length) {
      els.queueList.innerHTML = '<li class="track-empty">Queue is empty</li>';
    } else {
      els.queueList.innerHTML = pending.map((item) => `
        <li class="queue-item">
          <div class="queue-item-title">${escapeHtml(item.label || item.album || "Job")}</div>
          <div class="queue-item-sub">Queued</div>
          <button type="button" class="btn tiny" data-queue-remove="${escapeHtml(item.id)}">Remove</button>
        </li>`).join("");
    }

    const history = s.history || [];
    if (!history.length) {
      els.queueHistory.innerHTML = '<li class="track-empty">Nothing yet</li>';
    } else {
      els.queueHistory.innerHTML = history.slice(0, 8).map((item) => `
        <li class="queue-item ${escapeHtml(item.status || "")}">
          <div class="queue-item-title">${escapeHtml(item.label || "Job")}</div>
          <div class="queue-item-sub">${escapeHtml(item.status_text || item.status || "")}</div>
        </li>`).join("");
    }

  }

  function applyState(s) {
    const downloading = !!s.downloading;
    const remediating = !!s.remediating;
    const total = s.total || 0;
    const completed = s.completed || 0;
    const percent = s.percent ?? 0;
    const filePercent = s.file_percent ?? 0;

    els.progressLabel.textContent = downloading
      ? "Downloading"
      : remediating
        ? "Fixing"
        : percent >= 100
          ? "Finished"
          : "Ready";
    els.status.textContent = s.status || (
      downloading
        ? "Working…"
        : remediating
          ? "Fixing a track…"
          : "Paste a link and pick an artist"
    );
    els.count.textContent = `${completed} / ${total || "–"}`;
    els.percent.textContent = `${percent}%`;
    els.barFill.style.width = `${Math.max(0, Math.min(100, percent))}%`;

    if (downloading && filePercent > 0 && filePercent < 100) {
      els.fileWrap.classList.remove("hidden");
      els.fileFill.style.width = `${filePercent}%`;
      els.filePercent.textContent = `${filePercent}%`;
    } else {
      els.fileWrap.classList.add("hidden");
    }

    // Action buttons key off the album download only — remediations must not
    // lock Download/Queue or rename the button to Working…
    setRunningFields(downloading);
    lastDownloadBusy = downloading;

    // Self-heal: pending jobs with no active download → nudge the worker.
    const queued = Array.isArray(s.queue) ? s.queue.length : (s.queue_length || 0);
    if (queued > 0 && !downloading) {
      if (kickTimer) clearTimeout(kickTimer);
      kickTimer = setTimeout(() => {
        fetch("/api/queue/continue", { method: "POST" }).catch(() => {});
      }, 400);
    }

    const bits = [];
    if (s.artist && s.album) bits.push(`${s.artist} / ${s.album}`);
    else if (s.collection) bits.push(s.collection);
    if (s.host_download_dir) bits.push(`Host: ${s.host_download_dir}`);
    if (queued > 0) bits.push(`${queued} queued`);
    els.meta.textContent = bits.join(" · ");

    // Docker always has ffmpeg; a bare "python web_server.py" run might not,
    // and that failure otherwise only surfaces once a download is attempted.
    els.ffmpegWarning?.classList.toggle("hidden", s.ffmpeg_available !== false);

    if (s.ytdlp_version && els.ytdlpVersion && !els.ytdlpVersion.classList.contains("is-busy")) {
      const methods = Array.isArray(s.download_methods) ? s.download_methods.length : 0;
      els.ytdlpVersion.textContent = `yt-dlp ${s.ytdlp_version}`;
      els.ytdlpVersion.title = methods
        ? `Download methods tried per song: ${s.download_methods.join(", ")}`
        : "Downloader engine version";
    }

    renderTracks(s.tracks || []);
    els.trackSummary.textContent = summarize(s.tracks || []);
    renderFixPanel(s);
    renderQueue(s);

    if (Array.isArray(s.log)) {
      const text = s.log.join("\n");
      if (els.log.textContent !== text) {
        els.log.textContent = text;
        if (logPinnedToBottom) els.log.scrollTop = els.log.scrollHeight;
      }
    }
  }

  async function buildBody() {
    const url = els.url.value.trim();
    const compilation = isCompilation();
    const artist = els.artist.value.trim() || (compilation ? VARIOUS_ARTISTS : "");
    const album = els.album.value.trim();
    if (!url) throw new Error("Paste a link");
    if (!artist) throw new Error("Choose an artist");
    if (!album) throw new Error("Enter an album / single name");

    let cover_path = coverPath;
    if (els.jobCoverFile?.files?.[0]) {
      const form = new FormData();
      form.append("file", els.jobCoverFile.files[0]);
      const res = await fetch("/api/download/cover", { method: "POST", body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Cover upload failed");
      cover_path = data.path;
      coverPath = cover_path;
    }

    const limitRaw = els.limit.value.trim();
    const tracksRaw = els.tracks?.value.trim() || "";
    if (tracksRaw) {
      const parsed = describeTracksSpec(tracksRaw);
      if (parsed.error) throw new Error(parsed.error);
    }
    return {
      url,
      artist,
      album,
      album_artist: (els.albumArtist?.value || "").trim() || artist,
      release_type: releaseType(),
      cover_url: els.jobCoverUrl.value.trim() || null,
      cover_path: cover_path || null,
      force: els.force.checked,
      embed_metadata: els.metadata.checked,
      playlist_track_numbers: els.metadata.checked && els.playlistNumbers.checked,
      debug: els.debug.checked,
      strip_terms: (els.stripTerms?.value || "").trim(),
      verify_artists: compilation && !!els.verifyArtists?.checked,
      browser: els.browser.value,
      limit: limitRaw ? Number(limitRaw) : null,
      tracks: tracksRaw || null,
    };
  }

  function collapseIndices(nums) {
    const ordered = [...nums].sort((a, b) => a - b);
    if (!ordered.length) return "";
    const parts = [];
    let start = ordered[0];
    let prev = ordered[0];
    for (let i = 1; i < ordered.length; i += 1) {
      const n = ordered[i];
      if (n === prev + 1) {
        prev = n;
        continue;
      }
      parts.push(start === prev ? String(start) : `${start}-${prev}`);
      start = prev = n;
    }
    parts.push(start === prev ? String(start) : `${start}-${prev}`);
    return parts.join(",");
  }

  function parseTrackRangeToken(raw) {
    const m = String(raw).match(/^(\d+)(?:-(\d+))?$/);
    if (!m) return { error: `Invalid track selector “${raw}”. Try 1-10 or !3-5` };
    let start = Number(m[1]);
    let end = m[2] ? Number(m[2]) : start;
    if (start < 1 || end < 1) return { error: "Track numbers must be 1 or greater" };
    if (end < start) [start, end] = [end, start];
    if (end - start > 50000) return { error: "Track range is too large" };
    return { start, end };
  }

  function describeTracksSpec(spec) {
    const text = String(spec || "").trim();
    if (!text) {
      return { label: "all songs", error: null };
    }
    const include = new Set();
    const exclude = new Set();
    let hasInclude = false;
    for (const clause of text.split(/\s+/).filter(Boolean)) {
      const isExclude = clause.startsWith("!") || clause.startsWith("-");
      const body = isExclude ? clause.slice(1) : clause;
      if (!body) {
        return { label: "", error: `Invalid track selector “${clause}”` };
      }
      for (const raw of body.split(",").map((s) => s.trim()).filter(Boolean)) {
        const piece =
          raw.startsWith("!") || raw.startsWith("-") ? raw.slice(1) : raw;
        const parsed = parseTrackRangeToken(piece);
        if (parsed.error) return { label: "", error: parsed.error };
        const bucket =
          isExclude || raw.startsWith("!") || raw.startsWith("-")
            ? exclude
            : include;
        if (bucket === include) hasInclude = true;
        for (let n = parsed.start; n <= parsed.end; n += 1) bucket.add(n);
      }
    }
    const bits = [];
    if (hasInclude) bits.push(`only ${collapseIndices(include)}`);
    if (exclude.size) bits.push(`skip ${collapseIndices(exclude)}`);
    return { label: bits.join(" · ") || "all songs", error: null };
  }

  function updateTracksHint() {
    if (!els.tracksHint) return;
    const raw = els.tracks?.value.trim() || "";
    if (!raw) {
      els.tracksHint.textContent =
        "Blank = all. 1-10 only those. !3-5 skip 3–5. 1-20 !3-5 both.";
      els.tracksHint.classList.remove("is-error", "is-ok");
      return;
    }
    const parsed = describeTracksSpec(raw);
    if (parsed.error) {
      els.tracksHint.textContent = parsed.error;
      els.tracksHint.classList.add("is-error");
      els.tracksHint.classList.remove("is-ok");
      return;
    }
    els.tracksHint.textContent = parsed.label;
    els.tracksHint.classList.add("is-ok");
    els.tracksHint.classList.remove("is-error");
  }

  async function enqueue(prepared) {
    if (queueSubmitting) return null;
    queueSubmitting = true;
    updateActionButtons({ downloading: lastDownloadBusy });
    try {
      const body = prepared || (await buildBody());
      const res = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail;
        const message = typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
            : `Failed to queue (${res.status})`;
        throw new Error(message);
      }
      if (data.status) applyState(data.status);
      return data;
    } finally {
      queueSubmitting = false;
      updateActionButtons({ downloading: lastDownloadBusy });
    }
  }

  // ---- Album match review -------------------------------------------------
  // Keeps its own copy of the form so a review can sit open while the queue
  // runs, and so editing the form afterwards cannot change what was approved.
  // assign maps an album track position to a playlist song's index, so a bad
  // automatic pairing can be re-pointed at the right song by hand.
  let review = {
    album: null,
    sources: [],
    assign: {},
    autoAssign: {},
    include: {},
    body: null,
    busy: false,
  };

  function sourceByIndex(index) {
    if (index === null || index === undefined) return null;
    return review.sources.find((s) => s.index === index) || null;
  }

  function assignedSourceIndexes() {
    return new Set(
      Object.values(review.assign).filter((v) => v !== null && v !== undefined)
    );
  }

  // One row per album track, then whatever the playlist has left over.
  function reviewRowModel() {
    const rows = [];
    const tracks = review.album?.tracks || [];
    tracks.forEach((track, aIdx) => {
      const src = sourceByIndex(review.assign[aIdx]);
      rows.push({
        key: `a${aIdx}`,
        albumIndex: aIdx,
        album_track: track,
        source: src,
        kind: src ? "matched" : "missing",
        manual: (review.assign[aIdx] ?? null) !== (review.autoAssign[aIdx] ?? null),
      });
    });
    const used = assignedSourceIndexes();
    for (const src of review.sources) {
      if (used.has(src.index)) continue;
      rows.push({
        key: `s${src.index}`,
        albumIndex: null,
        album_track: null,
        source: src,
        kind: "extra",
        manual: false,
      });
    }
    return rows;
  }

  function fmtDuration(ms) {
    const total = Math.round((Number(ms) || 0) / 1000);
    if (!total) return "";
    const m = Math.floor(total / 60);
    const s = String(total % 60).padStart(2, "0");
    return `${m}:${s}`;
  }

  function closeReview() {
    els.reviewModal?.classList.add("hidden");
    els.reviewAlbumResults?.classList.add("hidden");
  }

  function includedRows() {
    return (review.viewRows || []).filter((r) => review.include[r.key]);
  }

  function reviewIncludedCount() {
    return includedRows().length;
  }

  function reviewSearchCount() {
    return includedRows().filter((r) => !r.source).length;
  }

  function updateReviewFooter() {
    const n = reviewIncludedCount();
    const searched = reviewSearchCount();
    if (els.btnReviewConfirm) {
      els.btnReviewConfirm.textContent = `Queue ${n} song${n === 1 ? "" : "s"}`;
      els.btnReviewConfirm.disabled = n === 0 || review.busy;
    }
    if (els.reviewHint) {
      els.reviewHint.textContent = searched
        ? `${searched} not in the playlist will be searched for by name.`
        : "";
    }
  }

  function sourceOptionLabel(src, usedElsewhere) {
    const dur = src.duration_ms ? ` · ${fmtDuration(src.duration_ms)}` : "";
    return `${src.index}. ${src.title}${dur}${usedElsewhere ? "  (used)" : ""}`;
  }

  function assignSelectHtml(row) {
    const used = assignedSourceIndexes();
    const current = review.assign[row.albumIndex] ?? null;
    const opts = [
      `<option value=""${current === null ? " selected" : ""}>— none —</option>`,
    ];
    for (const src of review.sources) {
      const takenElsewhere = used.has(src.index) && src.index !== current;
      opts.push(
        `<option value="${src.index}"${src.index === current ? " selected" : ""}>` +
          `${escapeHtml(sourceOptionLabel(src, takenElsewhere))}</option>`
      );
    }
    return `<select class="rv-assign" data-assign="${row.albumIndex}">${opts.join(
      ""
    )}</select>`;
  }

  function renderReviewRows() {
    const body = els.reviewRows;
    if (!body) return;
    const rows = reviewRowModel();
    review.viewRows = rows;
    body.innerHTML = "";
    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.className = `rv-row is-${row.kind}${row.manual ? " is-manual" : ""}`;
      const at = row.album_track;
      const src = row.source;
      const included = !!review.include[row.key];
      // A missing album track can still be included: the downloader will go
      // and find it by name instead of taking it from the playlist.
      const pickable = !!src || !!at;
      const albumCell = at
        ? `<span class="rv-title">${escapeHtml(at.title)}</span>` +
          `<span class="rv-sub">${escapeHtml(at.artist)}${
            at.duration_ms ? ` · ${fmtDuration(at.duration_ms)}` : ""
          }</span>`
        : `<span class="rv-sub">Not on the album</span>`;
      let srcCell;
      if (at && review.sources.length) {
        // Album rows get a picker so a wrong or missing pairing can be fixed.
        srcCell =
          assignSelectHtml(row) +
          (src
            ? `<span class="rv-sub">${escapeHtml(src.artists || "")}</span>`
            : `<span class="rv-sub">${
                included ? "Will search YouTube by name" : "Not in the playlist"
              }</span>`);
      } else if (src) {
        srcCell =
          `<span class="rv-title">${escapeHtml(src.title)}</span>` +
          `<span class="rv-sub">${escapeHtml(src.artists || "")}${
            src.duration_ms ? ` · ${fmtDuration(src.duration_ms)}` : ""
          }</span>`;
      } else {
        srcCell = `<span class="rv-sub">${
          included ? "Will search YouTube by name" : "Not in the playlist"
        }</span>`;
      }
      const state = row.manual
        ? `<span class="rv-chip manual">manual</span>`
        : row.kind === "matched"
          ? `<span class="rv-chip ok">match</span>`
          : row.kind === "missing"
            ? `<span class="rv-chip warn">missing</span>`
            : `<span class="rv-chip extra">extra</span>`;
      tr.innerHTML = `
        <td class="rv-pick">${
          pickable
            ? `<input type="checkbox" data-rv="${row.key}"${included ? " checked" : ""} />`
            : ""
        }</td>
        <td class="rv-num">${at ? at.number : ""}</td>
        <td>${albumCell}</td>
        <td class="rv-src">${srcCell}</td>
        <td class="rv-state">${state}</td>
      `;
      body.appendChild(tr);
    }
    body.querySelectorAll("input[data-rv]").forEach((input) => {
      input.addEventListener("change", () => {
        review.include[input.getAttribute("data-rv")] = input.checked;
        renderReviewRows();
      });
    });
    body.querySelectorAll("select[data-assign]").forEach((sel) => {
      sel.addEventListener("change", () => {
        reassign(Number(sel.getAttribute("data-assign")), sel.value);
      });
    });
    updateReviewFooter();
  }

  function reassign(albumIndex, rawValue) {
    const value = rawValue === "" ? null : Number(rawValue);
    if (value !== null) {
      // A song belongs to one album track, so steal it from any other row.
      for (const key of Object.keys(review.assign)) {
        if (Number(key) !== albumIndex && review.assign[key] === value) {
          review.assign[key] = null;
          review.include[`a${key}`] = false;
        }
      }
      review.include[`a${albumIndex}`] = true;
      review.include[`s${value}`] = false;
    }
    review.assign[albumIndex] = value;
    renderReviewRows();
  }

  function renderReviewHeader(data) {
    const album = data.album;
    if (els.reviewArt) {
      const art = album?.artwork_url || "";
      els.reviewArt.hidden = !art;
      if (art) els.reviewArt.src = art;
    }
    if (els.reviewAlbumName) {
      const label = album
        ? `${album.name}${album.artist ? ` — ${album.artist}` : ""}${
            album.year ? ` (${album.year})` : ""
          }`
        : data.album_error || "No album match";
      els.reviewAlbumName.textContent = label;
      // Long soundtrack names get clipped, so keep the full text on hover.
      els.reviewAlbumName.title = label;
    }
    applyCatalogueCredit(album);
    const s = data.summary || {};
    if (els.reviewCounts) {
      const bits = [];
      if (album) bits.push(`${s.matched || 0} of ${album.track_count || 0} matched`);
      if (s.missing) bits.push(`${s.missing} missing`);
      if (s.extra) bits.push(`${s.extra} extra`);
      bits.push(`${s.source_count || 0} in playlist`);
      els.reviewCounts.textContent = bits.join(" · ");
    }
    if (els.reviewSub) {
      const b = review.body || {};
      els.reviewSub.textContent = `${b.artist || ""} / ${b.album || ""}`;
    }
  }

  // The catalogue is the authority on who a release is credited to, so let it
  // drive the album artist. A catalogued compilation also switches the job to
  // Various Artists, which is mirrored back into the form so the visible
  // settings always match what will actually be queued.
  function applyCatalogueCredit(album) {
    if (album?.artist && review.body) {
      const credited = album.various_artists ? VARIOUS_ARTISTS : album.artist;
      if (album.various_artists) {
        const radio = document.querySelector(
          'input[name="release-type"][value="compilation"]'
        );
        if (radio && !radio.checked) {
          radio.checked = true;
          applyReleaseType();
        }
        // Filing a catalogued compilation under one performer's folder is the
        // exact problem this feature exists to solve, so move it.
        if (els.artist && els.artist.value !== VARIOUS_ARTISTS) {
          els.artist.value = VARIOUS_ARTISTS;
          updatePathPreview();
        }
      }
      autoFillAlbumArtist(credited);
      review.body.artist = els.artist?.value.trim() || review.body.artist;
      review.body.album_artist =
        (els.albumArtist?.value || "").trim() || credited;
      review.body.release_type = releaseType();
    }
    if (els.reviewAlbumArtist) {
      const credited = review.body?.album_artist || "";
      const folder = review.body?.artist || "";
      const bits = [];
      if (credited) bits.push(`Album artist: ${credited}`);
      if (folder && review.body?.album) {
        bits.push(`saving to ${folder}/${review.body.album}`);
      }
      els.reviewAlbumArtist.textContent = bits.join(" · ");
      els.reviewAlbumArtist.hidden = !bits.length;
    }
  }

  function setReviewStatus(text, { error = false } = {}) {
    if (!els.reviewStatus) return;
    els.reviewStatus.textContent = text || "";
    els.reviewStatus.classList.toggle("is-error", !!error);
  }

  async function loadReview({ collectionId = "" } = {}) {
    const body = review.body;
    if (!body) return;
    setReviewStatus("Reading playlist and looking up the album…");
    if (els.reviewRows) els.reviewRows.innerHTML = "";
    review.viewRows = [];
    updateReviewFooter();
    try {
      const res = await fetch("/api/album/match", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: body.url,
          album: body.album,
          artist: body.album_artist || body.artist,
          release_type: body.release_type,
          collection_id: collectionId,
          strip_terms: body.strip_terms || "",
          limit: body.limit,
          tracks: body.tracks,
          browser: body.browser,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Match failed (${res.status})`);
      review.album = data.album || null;
      review.sources = data.sources || [];
      review.assign = {};
      review.include = {};
      for (const row of data.rows || []) {
        if (row.album_index === null || row.album_index === undefined) {
          review.include[`s${row.source.index}`] = !!row.include;
          continue;
        }
        review.assign[row.album_index] = row.source ? row.source.index : null;
        review.include[`a${row.album_index}`] = !!row.include;
      }
      review.autoAssign = { ...review.assign };
      renderReviewHeader(data);
      renderReviewRows();
      setReviewStatus(
        data.album
          ? ""
          : "Couldn't find this album in the catalogue — the playlist is listed as-is.",
        { error: !data.album }
      );
    } catch (e) {
      setReviewStatus(e.message || String(e), { error: true });
    }
  }

  async function openReview() {
    let body;
    try {
      body = await buildBody();
    } catch (e) {
      alert(e.message || String(e));
      return;
    }
    review = {
      album: null,
      sources: [],
      assign: {},
      autoAssign: {},
      include: {},
      viewRows: [],
      body,
      busy: false,
    };
    if (els.reviewAlbumQ) els.reviewAlbumQ.value = body.album || "";
    if (els.reviewAlbumResults) els.reviewAlbumResults.classList.add("hidden");
    els.reviewModal?.classList.remove("hidden");
    // A field-level catalog pick is an exact release, not a name to re-guess —
    // hand its id straight to the matcher instead of fuzzy-searching again.
    await loadReview({ collectionId: selectedCollectionId || "" });
  }

  async function searchReviewAlbum() {
    const q = (els.reviewAlbumQ?.value || "").trim();
    if (q.length < 2) {
      setReviewStatus("Type at least 2 characters to search.", { error: true });
      return;
    }
    setReviewStatus("Searching albums…");
    try {
      const res = await fetch(`/api/album/search?q=${encodeURIComponent(q)}&limit=8`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Album search failed");
      const list = els.reviewAlbumResults;
      if (!list) return;
      list.innerHTML = "";
      for (const hit of data.results || []) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cover-hit";
        btn.innerHTML = `
          ${hit.artwork_url ? `<img src="${escapeHtml(hit.artwork_url)}" alt="" />` : ""}
          <span class="cover-hit-text">
            <strong>${escapeHtml(hit.name)}</strong>
            <span>${escapeHtml(hit.artist)}${hit.year ? ` · ${hit.year}` : ""}${
              hit.track_count ? ` · ${hit.track_count} tracks` : ""
            }</span>
          </span>`;
        btn.addEventListener("click", async () => {
          list.classList.add("hidden");
          await loadReview({ collectionId: hit.id });
        });
        li.appendChild(btn);
        list.appendChild(li);
      }
      list.classList.toggle("hidden", !(data.results || []).length);
      setReviewStatus((data.results || []).length ? "" : "No albums found.");
    } catch (e) {
      setReviewStatus(e.message || String(e), { error: true });
    }
  }

  function buildTrackPlan() {
    const useAlbum = !!els.reviewUseAlbumMeta?.checked;
    const plan = [];
    for (const row of includedRows()) {
      const src = row.source;
      const at = row.album_track;
      if (!src && !at) continue;
      if (!src) {
        // Missing from the playlist: hand the downloader the album's own
        // details and let it search for a matching upload.
        plan.push({
          index: plan.length + 1,
          title: at.title,
          artists: at.artist,
          duration_ms: at.duration_ms,
          album: review.body?.album || "",
          cover_url: null,
          source: "search",
          youtube_url: null,
          album_track: at.number,
        });
        continue;
      }
      plan.push({
        index: plan.length + 1,
        title: useAlbum && at ? at.title : src.title,
        artists: useAlbum && at && at.artist ? at.artist : src.artists,
        duration_ms: src.duration_ms,
        album: review.body?.album || src.album || "",
        cover_url: src.cover_url,
        source: src.source,
        youtube_url: src.youtube_url,
        album_track: useAlbum && at ? at.number : null,
        raw_title: src.raw_title || "",
      });
    }
    return plan;
  }

  async function confirmReview() {
    const plan = buildTrackPlan();
    if (!plan.length) return;
    review.busy = true;
    updateReviewFooter();
    setReviewStatus("Queuing…");
    try {
      const data = await enqueue({ ...review.body, track_plan: plan });
      closeReview();
      const label = data?.queued?.label || review.body?.album || "Job";
      els.status.textContent = lastDownloadBusy
        ? `Queued · ${label}`
        : `Starting · ${label}`;
    } catch (e) {
      setReviewStatus(e.message || String(e), { error: true });
    } finally {
      review.busy = false;
      updateReviewFooter();
    }
  }

  function setAllReviewRows(mode) {
    for (const row of reviewRowModel()) {
      if (!row.source && !row.album_track) continue;
      // "Whole album" means the release itself, not the playlist's bonus
      // material; "Playlist" means everything the link actually contains.
      review.include[row.key] =
        mode === "whole" ? !!row.album_track
        : mode === "playlist" ? !!row.source
        : mode === "matched" ? row.kind === "matched"
        : false;
    }
    renderReviewRows();
  }

  els.btnReviewClose?.addEventListener("click", closeReview);
  els.btnReviewConfirm?.addEventListener("click", confirmReview);
  els.btnReviewAlbumSearch?.addEventListener("click", searchReviewAlbum);
  els.reviewAlbumQ?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      searchReviewAlbum();
    }
  });
  els.btnReviewAll?.addEventListener("click", () => setAllReviewRows("whole"));
  els.btnReviewMatched?.addEventListener("click", () => setAllReviewRows("matched"));
  els.btnReviewPlaylist?.addEventListener("click", () => setAllReviewRows("playlist"));
  els.btnReviewNone?.addEventListener("click", () => setAllReviewRows("none"));
  els.reviewModal?.addEventListener("click", (e) => {
    if (e.target === els.reviewModal) closeReview();
  });

  let pasteFixId = null;
  const remediateModal = $("remediate-modal");
  const remediateTrackLabel = $("remediate-track");
  const remediateUrl = $("remediate-url");
  const btnRemediateClose = $("btn-remediate-close");
  const btnRemediateGo = $("btn-remediate-go");

  function closeRemediateModal() {
    pasteFixId = null;
    remediateModal?.classList.add("hidden");
    if (remediateUrl) remediateUrl.value = "";
  }

  function openRemediateModal(fixId, title) {
    pasteFixId = fixId;
    if (remediateTrackLabel) {
      remediateTrackLabel.textContent = title;
    }
    if (remediateUrl) remediateUrl.value = "";
    remediateModal?.classList.remove("hidden");
    remediateUrl?.focus();
  }

  async function remediate(fixId, action, youtubeUrl) {
    const body = {
      fix_id: fixId,
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

  els.trackList?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-skip-track]");
    if (!btn) return;
    const index = Number(btn.getAttribute("data-skip-track"));
    if (!index) return;
    skipQueuedTrack(index).catch((e) => alert(e.message));
  });

  els.fixList?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-fix]");
    if (!btn) return;
    const action = btn.getAttribute("data-fix");
    const fixId = btn.getAttribute("data-fix-id");
    if (!action || !fixId) return;
    if (action === "paste_url") {
      const row = btn.closest(".fix-item");
      const title =
        row?.querySelector(".fix-item-title")?.textContent?.trim() || "Track";
      openRemediateModal(fixId, title);
      return;
    }
    remediate(fixId, action).catch((e) => alert(e.message));
  });

  els.btnFixContinue?.addEventListener("click", async () => {
    const ids = [
      ...(els.fixList?.querySelectorAll("[data-fix='skip']") || []),
    ]
      .map((btn) => btn.getAttribute("data-fix-id"))
      .filter(Boolean);
    try {
      for (const fixId of ids) {
        await remediate(fixId, "skip");
      }
    } catch (e) {
      alert(e.message || String(e));
    }
  });

  els.queueList?.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-queue-remove]");
    if (!btn) return;
    const id = btn.getAttribute("data-queue-remove");
    const res = await fetch("/api/queue/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Could not remove");
      return;
    }
    applyState(data.status);
  });

  btnRemediateClose?.addEventListener("click", closeRemediateModal);
  bindBackdropClose(remediateModal, closeRemediateModal);
  btnRemediateGo?.addEventListener("click", async () => {
    const url = remediateUrl?.value.trim();
    const fixId = pasteFixId;
    if (!fixId || !url) return;
    closeRemediateModal();
    await remediate(fixId, "paste_url", url);
  });
  remediateUrl?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      btnRemediateGo?.click();
    }
  });

  async function refresh() {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`Status ${res.status}`);
    const s = await res.json();
    lastVersion = s.version ?? lastVersion;
    applyState(s);
  }

  function connectEvents() {
    const es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      try {
        const s = JSON.parse(ev.data);
        if (typeof s.version === "number" && s.version < lastVersion) return;
        lastVersion = s.version ?? lastVersion;
        applyState(s);
      } catch (_) { /* ignore */ }
    };
    es.onerror = () => {
      es.close();
      setTimeout(connectEvents, 2000);
    };
  }

  els.url.addEventListener("input", () => updateActionButtons({ downloading: lastDownloadBusy }));
  els.artist.addEventListener("input", () => {
    if (!els.albumArtist.value.trim()) {
      els.albumArtist.placeholder = els.artist.value || "Defaults to artist";
    }
    updatePathPreview();
    updateActionButtons({ downloading: lastDownloadBusy });
    scheduleAlbumMatchSearch();
  });
  els.album.addEventListener("input", () => {
    updatePathPreview();
    updateActionButtons({ downloading: lastDownloadBusy });
    // A locked catalog pick only applies to the album text it was made for;
    // any further edit means the user is no longer asking for that release.
    if (selectedCollectionId && els.album.value.trim() !== selectedCollectionLabel) {
      forgetSelectedCollection();
    }
    scheduleAlbumMatchSearch();
  });
  els.album.addEventListener("focus", () => {
    if (els.albumSuggestList?.children.length) openAlbumSuggest();
  });
  els.album.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      closeAlbumSuggest();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      clearTimeout(albumMatchTimer);
      runAlbumMatchSearch();
    } else if (ev.key === "ArrowDown") {
      const first = els.albumSuggestList?.querySelector(".suggest-hit");
      if (first) {
        ev.preventDefault();
        first.focus();
      }
    }
  });
  els.albumSuggestList?.addEventListener("keydown", (ev) => {
    const items = [...els.albumSuggestList.querySelectorAll(".suggest-hit")];
    const i = items.indexOf(document.activeElement);
    if (ev.key === "ArrowDown" && i < items.length - 1) {
      ev.preventDefault();
      items[i + 1].focus();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      if (i > 0) items[i - 1].focus();
      else els.album.focus();
    } else if (ev.key === "Escape") {
      closeAlbumSuggest();
      els.album.focus();
    }
  });
  document.addEventListener("click", (ev) => {
    if (!els.albumSuggestPanel || els.albumSuggestPanel.classList.contains("hidden")) return;
    const field = $("album-field");
    if (field && !field.contains(ev.target)) closeAlbumSuggest();
  });
  els.albumArtist?.addEventListener("input", () => {
    albumArtistTouched = !!els.albumArtist.value.trim();
    updatePathPreview();
  });
  document.querySelectorAll('input[name="release-type"]').forEach((el) => {
    el.addEventListener("change", () => {
      applyReleaseType();
      forgetSelectedCollection();
    });
  });

  els.metadata.addEventListener("change", () => {
    els.playlistNumbers.disabled = !els.metadata.checked;
  });

  els.tracks?.addEventListener("input", updateTracksHint);
  updateTracksHint();

  // Fonts/layout can still be settling on first paint, so snap the
  // indicator into place a beat later rather than trusting the very first
  // measurement — and keep it aligned if the window/font metrics change.
  positionReleaseIndicator({ animate: false });
  requestAnimationFrame(() => positionReleaseIndicator({ animate: false }));
  window.addEventListener("resize", () => positionReleaseIndicator({ animate: false }));

  els.jobCoverFile?.addEventListener("change", () => {
    const file = els.jobCoverFile.files && els.jobCoverFile.files[0];
    if (!file) return;
    ingestCoverFile(file);
  });

  let coverUrlTimer = null;
  async function previewCoverUrl() {
    const url = els.jobCoverUrl?.value.trim();
    if (!url) return;
    if (els.jobCoverFile) els.jobCoverFile.value = "";
    // Show remote URL immediately; replace with server-cached preview when ready.
    setCoverPreview(url);
    try {
      const res = await fetch("/api/download/cover/from-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return;
      coverPath = data.path || null;
      if (data.preview_url) setCoverPreview(data.preview_url);
    } catch (_) {
      /* keep best-effort remote preview */
    }
  }

  els.jobCoverUrl?.addEventListener("change", previewCoverUrl);
  els.jobCoverUrl?.addEventListener("input", () => {
    clearTimeout(coverUrlTimer);
    coverUrlTimer = setTimeout(() => {
      if (els.jobCoverUrl.value.trim()) previewCoverUrl();
    }, 450);
  });
  els.btnJobCoverClear?.addEventListener("click", clearJobCover);

  els.jobCoverTile?.addEventListener("click", () => {
    els.jobCoverTile.focus();
  });
  els.jobCoverTile?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      els.jobCoverFile?.click();
    }
  });

  async function handleCoverPaste(ev) {
    const file = imageFromClipboard(ev);
    if (!file) return false;
    ev.preventDefault();
    await ingestCoverFile(file);
    return true;
  }

  els.jobCoverUrl?.addEventListener("paste", (ev) => {
    if (imageFromClipboard(ev)) {
      handleCoverPaste(ev).catch(() => {});
      return;
    }
    setTimeout(previewCoverUrl, 0);
  });

  document.addEventListener("paste", (ev) => {
    if (!imageFromClipboard(ev)) return;
    const target = ev.target;
    const tag = target && target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") {
      if (target === els.jobCoverUrl) return; // handled above
      return;
    }
    const active = document.activeElement;
    const inCover =
      !!els.jobCoverRow &&
      (els.jobCoverRow.contains(active) ||
        els.jobCoverRow.contains(target) ||
        active === els.jobCoverTile);
    if (!inCover) return;
    handleCoverPaste(ev).catch(() => {});
  });

  ["dragenter", "dragover"].forEach((type) => {
    els.jobCoverTile?.addEventListener(type, (ev) => {
      ev.preventDefault();
      els.jobCoverTile.classList.add("is-drop");
    });
  });
  els.jobCoverTile?.addEventListener("dragleave", () => {
    els.jobCoverTile.classList.remove("is-drop");
  });
  els.jobCoverTile?.addEventListener("drop", (ev) => {
    ev.preventDefault();
    els.jobCoverTile.classList.remove("is-drop");
    const file = ev.dataTransfer?.files?.[0];
    if (file) ingestCoverFile(file).catch(() => {});
  });

  els.btnNewArtist?.addEventListener("click", async () => {
    const ask = window.mdAskName;
    const name = ask
      ? await ask({
          title: "New artist",
          label: "Artist name",
          value: "",
          confirmLabel: "Create",
        })
      : window.prompt("Artist name");
    if (!name || !name.trim()) return;
    const res = await fetch("/api/library/artists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Could not create artist");
      return;
    }
    await loadArtists(data.name);
    els.artist.value = data.name;
    updatePathPreview();
    updateActionButtons({ downloading: lastDownloadBusy });
  });

  els.btnQueue?.addEventListener("click", async () => {
    try {
      const data = await enqueue();
      if (data) els.status.textContent = "Added to queue";
    } catch (e) {
      alert(e.message || String(e));
    }
  });

  // Primary action reviews the match first; the queue button is the fast path.
  els.btnDownload.addEventListener("click", openReview);

  els.btnCancel.addEventListener("click", async () => {
    await fetch("/api/cancel", { method: "POST" });
  });

  function setEngineTag(text, { busy = false, error = false } = {}) {
    const el = els.ytdlpVersion;
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("is-busy", !!busy);
    el.classList.toggle("is-error", !!error);
  }

  els.btnYtdlpUpdate?.addEventListener("click", async () => {
    els.btnYtdlpUpdate.disabled = true;
    setEngineTag("updating…", { busy: true });
    try {
      const res = await fetch("/api/system/ytdlp/update", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setEngineTag("update failed", { error: true });
        alert(data.detail || "Update failed");
        return;
      }
      if (data.restart_required) {
        setEngineTag(`yt-dlp ${data.after} · restart`, { busy: true });
        alert(
          `yt-dlp updated ${data.before} → ${data.after}.\n\n` +
            "Restart the app (or container) so the new version is loaded."
        );
      } else {
        setEngineTag(`yt-dlp ${data.after}`);
        alert(`Already on the latest yt-dlp (${data.after}).`);
      }
    } catch (e) {
      setEngineTag("update failed", { error: true });
      alert(e.message || String(e));
    } finally {
      els.btnYtdlpUpdate.disabled = false;
    }
  });

  els.btnClear.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    els.log.textContent = "";
  });

  loadArtists()
    .then(() => refresh())
    .then(() => connectEvents())
    .catch((e) => {
      els.status.textContent = `Cannot reach server: ${e.message}`;
    });
})();
