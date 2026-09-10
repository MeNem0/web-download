(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    crumbs: $("lib-crumbs"),
    summary: $("lib-summary"),
    list: $("lib-list"),
    search: $("lib-search"),
    btnSearchClear: $("btn-lib-search-clear"),
    btnNew: $("btn-lib-new"),
    btnCollapse: $("btn-lib-collapse"),
    btnRefresh: $("btn-lib-refresh"),
    btnEditSelected: $("btn-lib-edit-selected"),
    btnClearSelected: $("btn-lib-clear-selected"),
    btnSelectAll: $("btn-lib-select-all"),
    modal: $("meta-modal"),
    cover: $("meta-cover"),
    coverFile: $("meta-cover-file"),
    coverUrl: $("meta-cover-url"),
    title: $("meta-track-title"),
    titleField: $("meta-title-field"),
    artists: $("meta-artists"),
    albumArtist: $("meta-album-artist"),
    album: $("meta-album"),
    trackNo: $("meta-track-no"),
    filename: $("meta-filename"),
    bulkHint: $("meta-bulk-hint"),
    seqWrap: $("meta-seq-wrap"),
    numberSeq: $("meta-number-seq"),
    compilation: $("meta-compilation"),
    compilationLabel: $("meta-compilation-label"),
    metaTitle: $("meta-title"),
    btnClose: $("btn-meta-close"),
    btnSave: $("btn-meta-save"),
    btnRename: $("btn-meta-rename"),
    btnDelete: $("btn-meta-delete"),
    btnCoverClear: $("btn-meta-cover-clear"),
    btnCoverSearch: $("btn-meta-cover-search"),
    coverSearchStatus: $("meta-cover-search-status"),
    coverSearchResults: $("meta-cover-search-results"),
    btnReplaceToggle: $("btn-meta-replace-toggle"),
    replacePanel: $("meta-replace-panel"),
    replaceUrl: $("meta-replace-url"),
    btnReplaceGo: $("btn-meta-replace-go"),
    replaceStatus: $("meta-replace-status"),
    nameModal: $("name-modal"),
    nameTitle: $("name-modal-title"),
    nameDetail: $("name-modal-detail"),
    nameLabel: $("name-modal-label"),
    nameInput: $("name-modal-input"),
    btnNameCancel: $("btn-name-cancel"),
    btnNameConfirm: $("btn-name-confirm"),
  };

  if (!els.list) return;

  const LIB_ICON = {
    folder:
      '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2 4.6c0-.7.58-1.3 1.3-1.3h2.75l1.1 1.3h5.55c.72 0 1.3.58 1.3 1.3v5.7c0 .72-.58 1.3-1.3 1.3H3.3c-.72 0-1.3-.58-1.3-1.3V4.6Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
    file:
      '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="5.1" cy="12" r="1.9" stroke="currentColor" stroke-width="1.3"/><path d="M7 12V3.6L12.4 2.4v7.3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  let rootPath = "";
  let libPath = "";
  let selectedPath = "";
  let bulkPaths = [];
  let editMode = "single"; // single | bulk
  // Bulk edits keep tags the user did not touch, so the checkbox needs to
  // distinguish "left alone" from "deliberately unchecked".
  let compilationTouched = false;
  let coverObjectUrl = null;
  const expanded = new Set();
  const selected = new Set();
  const cache = new Map();
  let nameResolver = null;
  let searchQuery = "";
  let searchTimer = null;
  let searchSeq = 0;

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function splitPath(path) {
    if (!path) return [];
    return path.replace(/\\/g, "/").split("/").filter(Boolean);
  }

  function joinUnderRoot(parts) {
    if (!rootPath) return parts.join("/");
    const rootNorm = rootPath.replace(/[/\\]+$/, "");
    const sep = rootPath.includes("\\") ? "\\" : "/";
    if (!parts.length) return rootNorm;
    return `${rootNorm}${sep}${parts.join(sep)}`;
  }

  function relativeParts(path) {
    if (!path || !rootPath) return splitPath(path);
    const a = path.replace(/\\/g, "/").replace(/\/+$/, "");
    const b = rootPath.replace(/\\/g, "/").replace(/\/+$/, "");
    if (a === b) return [];
    if (a.toLowerCase().startsWith(`${b.toLowerCase()}/`)) {
      return a.slice(b.length + 1).split("/").filter(Boolean);
    }
    return splitPath(path);
  }

  function setCoverSrc(url) {
    if (coverObjectUrl) {
      URL.revokeObjectURL(coverObjectUrl);
      coverObjectUrl = null;
    }
    els.cover.src = url || "";
    els.cover.style.visibility = url ? "visible" : "hidden";
  }

  function setMetaCoverSearchStatus(message, { error = false } = {}) {
    const el = els.coverSearchStatus;
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

  function clearMetaCoverSearch() {
    if (els.coverSearchResults) {
      els.coverSearchResults.innerHTML = "";
      els.coverSearchResults.classList.add("hidden");
    }
    setMetaCoverSearchStatus("");
  }

  function metaCoverSearchQuery() {
    const artist = (els.artists?.value || "").trim();
    const album = (els.album?.value || "").trim();
    const title = (els.title?.value || "").trim();
    return [artist, album || title].filter(Boolean).join(" ").trim();
  }

  async function applyMetaCoverHit(hit) {
    if (!hit?.artwork_url) return;
    if (hit.album && els.album && !els.album.value.trim()) {
      els.album.value = hit.album;
    }
    if (hit.artist && els.albumArtist && !els.albumArtist.value.trim()) {
      els.albumArtist.value = hit.artist;
    }
    if (els.artists && !els.artists.value.trim() && hit.artist) {
      els.artists.value = hit.artist;
    }
    els.coverUrl.value = hit.artwork_url;
    setCoverSrc(hit.artwork_url);
    setMetaCoverSearchStatus(`Cover set · ${hit.album || hit.title}`);
  }

  async function runMetaCoverSearch() {
    const q = metaCoverSearchQuery();
    if (q.length < 2) {
      setMetaCoverSearchStatus("Fill artist and album (or title) first.", { error: true });
      return;
    }
    if (els.btnCoverSearch) els.btnCoverSearch.disabled = true;
    setMetaCoverSearchStatus("Searching…");
    try {
      const res = await fetch(
        `/api/download/cover/search?q=${encodeURIComponent(q)}&entity=all&limit=10`
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        clearMetaCoverSearch();
        setMetaCoverSearchStatus(data.detail || "Search failed", { error: true });
        return;
      }
      const results = data.results || [];
      if (!results.length) {
        if (els.coverSearchResults) {
          els.coverSearchResults.innerHTML = "";
          els.coverSearchResults.classList.add("hidden");
        }
        setMetaCoverSearchStatus("No matches.", { error: true });
        return;
      }
      setMetaCoverSearchStatus(
        `${results.length} result${results.length === 1 ? "" : "s"} — pick one`
      );
      const list = els.coverSearchResults;
      if (!list) return;
      list.innerHTML = "";
      for (const hit of results) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cover-hit";
        const kindLabel = hit.kind === "song" ? "Single" : "Album";
        const bits = [hit.artist, hit.year, kindLabel].filter(Boolean);
        const art = String(hit.artwork_url || "").replaceAll('"', "&quot;");
        btn.innerHTML = `
          <img class="cover-hit-art" alt="" loading="lazy" src="${art}" />
          <span class="cover-hit-body">
            <span class="cover-hit-title">${escapeHtml(hit.album || hit.title || "")}</span>
            <span class="cover-hit-meta">${escapeHtml(bits.join(" · "))}</span>
          </span>
        `;
        btn.addEventListener("click", () => applyMetaCoverHit(hit));
        li.appendChild(btn);
        list.appendChild(li);
      }
      list.classList.remove("hidden");
    } catch (err) {
      clearMetaCoverSearch();
      setMetaCoverSearchStatus(err.message || "Search failed", { error: true });
    } finally {
      if (els.btnCoverSearch) els.btnCoverSearch.disabled = false;
    }
  }

  function askName({ title, label, detail = "", value = "", confirmLabel = "Save" }) {
    return new Promise((resolve) => {
      if (nameResolver) nameResolver(null);
      nameResolver = resolve;
      els.nameTitle.textContent = title;
      els.nameLabel.textContent = label;
      els.nameDetail.textContent = detail || "";
      els.nameDetail.style.display = detail ? "" : "none";
      els.nameInput.value = value || "";
      els.btnNameConfirm.textContent = confirmLabel;
      els.nameModal.classList.remove("hidden");
      requestAnimationFrame(() => {
        els.nameInput.focus();
        els.nameInput.select();
      });
    });
  }

  function closeNameModal(result) {
    els.nameModal.classList.add("hidden");
    const resolve = nameResolver;
    nameResolver = null;
    if (resolve) resolve(result);
  }

  window.mdAskName = askName;

  async function fetchListing(path) {
    const key = path || "";
    if (cache.has(key)) return cache.get(key);
    const qs = path ? `?path=${encodeURIComponent(path)}` : "";
    const res = await fetch(`/api/library${qs}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Could not open library");
    cache.set(key, data);
    return data;
  }

  function invalidate(path) {
    if (path == null) {
      cache.clear();
      return;
    }
    cache.delete(path);
    const prefix = path.replace(/\\/g, "/");
    for (const key of [...cache.keys()]) {
      const norm = key.replace(/\\/g, "/");
      if (norm === prefix || norm.startsWith(`${prefix}/`)) cache.delete(key);
    }
  }

  function updateSelectionUi() {
    const count = selected.size;
    const visible = els.list?.querySelectorAll("[data-select]")?.length || 0;
    if (els.btnEditSelected) {
      els.btnEditSelected.disabled = count < 1;
      els.btnEditSelected.textContent =
        count > 1 ? `Edit ${count} selected` : count === 1 ? "Edit selected" : "Edit selected";
    }
    if (els.btnClearSelected) els.btnClearSelected.disabled = count < 1;
    if (els.btnSelectAll) {
      els.btnSelectAll.disabled = visible < 1;
      els.btnSelectAll.textContent =
        visible > 0 && count >= visible ? "Deselect all" : "Select all";
    }
  }

  function selectAllVisible() {
    const inputs = [...(els.list?.querySelectorAll("[data-select]") || [])];
    if (!inputs.length) return;
    const allSelected = inputs.every((input) =>
      selected.has(input.getAttribute("data-select")),
    );
    if (allSelected) {
      inputs.forEach((input) => {
        const path = input.getAttribute("data-select");
        selected.delete(path);
        input.checked = false;
        input.closest(".lib-item")?.classList.remove("is-selected");
      });
    } else {
      inputs.forEach((input) => {
        const path = input.getAttribute("data-select");
        selected.add(path);
        input.checked = true;
        input.closest(".lib-item")?.classList.add("is-selected");
      });
    }
    updateSelectionUi();
  }

  function renderCrumbs() {
    const parts = relativeParts(libPath);
    const items = [`<button type="button" class="lib-crumb" data-crumb="">Library</button>`];
    parts.forEach((part, index) => {
      const target = joinUnderRoot(parts.slice(0, index + 1));
      items.push('<span class="lib-crumb-sep" aria-hidden="true">/</span>');
      items.push(
        `<button type="button" class="lib-crumb" data-crumb="${escapeHtml(target)}">${escapeHtml(part)}</button>`,
      );
    });
    els.crumbs.innerHTML = items.join("");
    els.crumbs.querySelectorAll("[data-crumb]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-crumb");
        openFolder(target || null);
      });
    });
  }

  async function openFolder(path) {
    try {
      if (searchQuery) clearSearch({ keepFocus: false });
      const data = await fetchListing(path);
      rootPath = data.root || rootPath;
      libPath = data.path || "";
      await render();
    } catch (err) {
      els.list.innerHTML = `<p class="track-empty">${escapeHtml(err.message || "Error")}</p>`;
    }
  }

  function clearSearch({ keepFocus = true } = {}) {
    searchQuery = "";
    if (els.search) els.search.value = "";
    if (els.btnSearchClear) els.btnSearchClear.disabled = true;
    if (keepFocus) els.search?.focus();
  }

  async function fetchSearch(query) {
    const qs = new URLSearchParams({ q: query, limit: "200" });
    const res = await fetch(`/api/library/search?${qs}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Search failed");
    return data;
  }

  async function renderSearch(query) {
    renderCrumbs();
    const seq = ++searchSeq;
    els.summary.textContent = "Searching…";
    els.list.innerHTML = '<p class="track-empty">Searching library…</p>';
    updateSelectionUi();
    try {
      const data = await fetchSearch(query);
      if (seq !== searchSeq || searchQuery.trim() !== query) return;
      rootPath = data.root || rootPath;
      const folders = data.folders || [];
      const files = data.files || [];
      const total = folders.length + files.length;
      els.summary.textContent = data.truncated
        ? `${total} results (capped)`
        : `${total} result${total === 1 ? "" : "s"}`;

      if (!total) {
        els.list.innerHTML =
          '<p class="track-empty">No matches. Try another title, artist, or album.</p>';
        updateSelectionUi();
        return;
      }

      const html = [];
      for (const folder of folders) {
        html.push(renderSearchFolder(folder));
      }
      for (const file of files) {
        html.push(renderSearchFile(file));
      }
      els.list.innerHTML = html.join("");
      bindTreeEvents(els.list);
      updateSelectionUi();
    } catch (err) {
      if (seq !== searchSeq) return;
      els.list.innerHTML = `<p class="track-empty">${escapeHtml(err.message || "Error")}</p>`;
      els.summary.textContent = "Search failed";
    }
  }

  function renderSearchFolder(folder) {
    const rel = folder.rel || folder.name;
    return `
      <div class="lib-node lib-search-hit" role="treeitem" style="--d:0">
        <div class="lib-item is-folder">
          <span class="lib-twist" aria-hidden="true"></span>
          <div class="lib-body" data-enter="${escapeHtml(folder.path)}" role="button" tabindex="0">
            <span class="lib-badge folder">${LIB_ICON.folder}<span class="sr-only">Folder</span></span>
            <div class="lib-copy">
              <div class="lib-name">${escapeHtml(folder.name)}</div>
              <div class="lib-sub">${escapeHtml(rel)}</div>
            </div>
          </div>
          <div class="lib-actions">
            <button class="lib-btn primary" type="button" data-enter="${escapeHtml(folder.path)}">Open</button>
          </div>
        </div>
      </div>`;
  }

  function renderSearchFile(file) {
    const meta = [file.artists, file.album].filter(Boolean).join(" · ");
    const rel = file.rel || file.name;
    const sub = [meta, rel].filter(Boolean).join(" · ");
    const checked = selected.has(file.path) ? " checked" : "";
    return `
      <div class="lib-node lib-search-hit" role="treeitem" style="--d:0">
        <div class="lib-item is-file${checked ? " is-selected" : ""}">
          <label class="lib-check">
            <input type="checkbox" data-select="${escapeHtml(file.path)}"${checked} />
          </label>
          <div class="lib-body" data-edit="${escapeHtml(file.path)}" role="button" tabindex="0">
            <span class="lib-badge file">${LIB_ICON.file}<span class="sr-only">MP3</span></span>
            <div class="lib-copy">
              <div class="lib-name">${escapeHtml(file.title || file.name)}</div>
              <div class="lib-sub">${escapeHtml(sub)}</div>
            </div>
          </div>
          <div class="lib-actions">
            <button class="lib-btn primary" type="button" data-edit="${escapeHtml(file.path)}">Edit</button>
            <button class="lib-btn" type="button" data-rename="${escapeHtml(file.path)}">Rename</button>
            <button class="lib-btn danger" type="button" data-delete="${escapeHtml(file.path)}">Delete</button>
          </div>
        </div>
      </div>`;
  }

  async function render() {
    const q = searchQuery.trim();
    if (q) {
      if (els.btnSearchClear) els.btnSearchClear.disabled = false;
      await renderSearch(q);
      return;
    }
    if (els.btnSearchClear) els.btnSearchClear.disabled = true;
    renderCrumbs();
    try {
      const data = await fetchListing(libPath || null);
      rootPath = data.root || rootPath;
      libPath = data.path || libPath;
      const folderCount = (data.folders || []).length;
      const fileCount = (data.files || []).length;
      els.summary.textContent = `${folderCount} folders · ${fileCount} tracks`;

      if (!folderCount && !fileCount) {
        els.list.innerHTML = '<p class="track-empty">This folder is empty.</p>';
        updateSelectionUi();
        return;
      }

      const html = [];
      for (const folder of data.folders || []) {
        html.push(await renderFolder(folder, 0));
      }
      for (const file of data.files || []) {
        html.push(renderFile(file, 0));
      }
      els.list.innerHTML = html.join("");
      bindTreeEvents(els.list);
      updateSelectionUi();
    } catch (err) {
      els.list.innerHTML = `<p class="track-empty">${escapeHtml(err.message || "Error")}</p>`;
    }
  }

  async function renderFolder(folder, depth) {
    const open = expanded.has(folder.path);
    let childrenHtml = "";
    if (open) {
      try {
        const child = await fetchListing(folder.path);
        const bits = [];
        for (const sub of child.folders || []) {
          bits.push(await renderFolder(sub, depth + 1));
        }
        for (const file of child.files || []) {
          bits.push(renderFile(file, depth + 1));
        }
        if (!bits.length) {
          bits.push(`<div class="lib-empty" style="--d:${depth + 1}">Empty folder</div>`);
        }
        childrenHtml = `<div class="lib-children">${bits.join("")}</div>`;
      } catch (err) {
        childrenHtml = `<div class="lib-empty" style="--d:${depth + 1}">${escapeHtml(err.message)}</div>`;
      }
    }

    return `
      <div class="lib-node" role="treeitem" aria-expanded="${open}" style="--d:${depth}">
        <div class="lib-item is-folder">
          <button class="lib-twist" type="button" data-toggle="${escapeHtml(folder.path)}" aria-label="${open ? "Collapse" : "Expand"}">
            <span class="lib-chevron ${open ? "is-open" : ""}"></span>
          </button>
          <div class="lib-body" data-enter="${escapeHtml(folder.path)}" role="button" tabindex="0">
            <span class="lib-badge folder">${LIB_ICON.folder}<span class="sr-only">Folder</span></span>
            <div class="lib-copy">
              <div class="lib-name">${escapeHtml(folder.name)}</div>
              <div class="lib-sub">Tap to open · chevron to expand</div>
            </div>
          </div>
          <div class="lib-actions">
            <button class="lib-btn" type="button" data-rename="${escapeHtml(folder.path)}">Rename</button>
            <button class="lib-btn danger" type="button" data-delete="${escapeHtml(folder.path)}">Delete</button>
          </div>
        </div>
        ${childrenHtml}
      </div>`;
  }

  function renderFile(file, depth) {
    const sub = [file.artists, file.album].filter(Boolean).join(" · ") || file.name;
    const checked = selected.has(file.path) ? " checked" : "";
    return `
      <div class="lib-node" role="treeitem" style="--d:${depth}">
        <div class="lib-item is-file${checked ? " is-selected" : ""}">
          <label class="lib-check">
            <input type="checkbox" data-select="${escapeHtml(file.path)}"${checked} />
          </label>
          <div class="lib-body" data-edit="${escapeHtml(file.path)}" role="button" tabindex="0">
            <span class="lib-badge file">${LIB_ICON.file}<span class="sr-only">MP3</span></span>
            <div class="lib-copy">
              <div class="lib-name">${escapeHtml(file.title || file.name)}</div>
              <div class="lib-sub">${escapeHtml(sub)}</div>
            </div>
          </div>
          <div class="lib-actions">
            <button class="lib-btn primary" type="button" data-edit="${escapeHtml(file.path)}">Edit</button>
            <button class="lib-btn" type="button" data-rename="${escapeHtml(file.path)}">Rename</button>
            <button class="lib-btn danger" type="button" data-delete="${escapeHtml(file.path)}">Delete</button>
          </div>
        </div>
      </div>`;
  }

  // Expanding/collapsing a folder used to re-render the whole tree from the
  // root down — every already-open sibling got rebuilt too, just to touch
  // one node. This patches only the toggled node's own children in place,
  // so opening a folder costs one (often cached) fetch and one small DOM
  // update instead of a full-tree rebuild.
  async function toggleFolder(path, node) {
    const item = node.querySelector(":scope > .lib-item");
    const twist = item?.querySelector("[data-toggle]");
    const chevron = twist?.querySelector(".lib-chevron");
    const depth = Number(node.style.getPropertyValue("--d")) || 0;
    let childrenEl = node.querySelector(":scope > .lib-children");

    if (expanded.has(path)) {
      expanded.delete(path);
      twist?.setAttribute("aria-label", "Expand");
      chevron?.classList.remove("is-open");
      node.setAttribute("aria-expanded", "false");
      childrenEl?.remove();
      return;
    }

    expanded.add(path);
    twist?.setAttribute("aria-label", "Collapse");
    chevron?.classList.add("is-open");
    node.setAttribute("aria-expanded", "true");

    if (!childrenEl) {
      childrenEl = document.createElement("div");
      childrenEl.className = "lib-children";
      node.appendChild(childrenEl);
    }
    childrenEl.innerHTML = `<div class="lib-empty is-loading" style="--d:${depth + 1}">Loading…</div>`;

    try {
      const data = await fetchListing(path);
      const bits = [];
      for (const sub of data.folders || []) bits.push(await renderFolder(sub, depth + 1));
      for (const file of data.files || []) bits.push(renderFile(file, depth + 1));
      childrenEl.innerHTML = bits.length
        ? bits.join("")
        : `<div class="lib-empty" style="--d:${depth + 1}">Empty folder</div>`;
      bindTreeEvents(childrenEl);
    } catch (err) {
      childrenEl.innerHTML = `<div class="lib-empty" style="--d:${depth + 1}">${escapeHtml(err.message || "Error")}</div>`;
    }
  }

  function bindActivate(el, handler) {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      handler();
    });
    el.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        ev.stopPropagation();
        handler();
      }
    });
  }

  function bindTreeEvents(root) {
    root.querySelectorAll("[data-toggle]").forEach((btn) => {
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const path = btn.getAttribute("data-toggle");
        const node = btn.closest(".lib-node");
        if (node) await toggleFolder(path, node);
      });
    });
    root.querySelectorAll("[data-enter]").forEach((el) => {
      bindActivate(el, () => openFolder(el.getAttribute("data-enter")));
    });
    root.querySelectorAll("[data-edit]").forEach((el) => {
      bindActivate(el, () => openEditor(el.getAttribute("data-edit")));
    });
    root.querySelectorAll("[data-rename]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        renameEntry(btn.getAttribute("data-rename"));
      });
    });
    root.querySelectorAll("[data-delete]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        deleteEntry(btn.getAttribute("data-delete"));
      });
    });
    root.querySelectorAll("[data-select]").forEach((input) => {
      input.addEventListener("click", (ev) => ev.stopPropagation());
      input.addEventListener("change", () => {
        const path = input.getAttribute("data-select");
        if (input.checked) selected.add(path);
        else selected.delete(path);
        const item = input.closest(".lib-item");
        if (item) item.classList.toggle("is-selected", input.checked);
        updateSelectionUi();
      });
    });
  }

  async function refresh() {
    invalidate(null);
    if (searchQuery.trim()) {
      await render();
      return;
    }
    await openFolder(libPath || null);
  }

  async function renameEntry(path) {
    const current = path.split(/[/\\]/).pop();
    const next = await askName({
      title: "Rename",
      label: "New name",
      detail: current,
      value: current,
      confirmLabel: "Rename",
    });
    if (!next || !next.trim() || next.trim() === current) return;
    const res = await fetch("/api/library/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, new_name: next.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Rename failed");
      return;
    }
    if (selectedPath === path) selectedPath = data.path;
    if (selected.has(path)) {
      selected.delete(path);
      selected.add(data.path);
    }
    if (expanded.has(path)) {
      expanded.delete(path);
      expanded.add(data.path);
    }
    bulkPaths = bulkPaths.map((p) => (p === path ? data.path : p));
    await refresh();
  }

  async function deleteEntry(path) {
    const name = path.split(/[/\\]/).pop();
    if (!window.confirm(`Delete "${name}"? This cannot be undone.`)) return;
    const res = await fetch("/api/library/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Delete failed");
      return;
    }
    if (selectedPath === path) closeEditor();
    selected.delete(path);
    expanded.delete(path);
    await refresh();
  }

  async function createFolder() {
    const name = await askName({
      title: "New folder",
      label: "Folder name",
      value: "",
      confirmLabel: "Create",
    });
    if (!name || !name.trim()) return;
    const res = await fetch("/api/library/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent: libPath, name: name.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Could not create folder");
      return;
    }
    await refresh();
  }

  function setReplaceStatus(message, { error = false } = {}) {
    const el = els.replaceStatus;
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

  function closeReplacePanel() {
    els.replacePanel?.classList.add("hidden");
    if (els.replaceUrl) els.replaceUrl.value = "";
    setReplaceStatus("");
  }

  function setEditorMode(mode, count) {
    editMode = mode;
    const bulk = mode === "bulk";
    els.bulkHint?.classList.toggle("hidden", !bulk);
    els.seqWrap?.classList.toggle("hidden", !bulk);
    els.titleField?.classList.toggle("hidden", bulk);
    els.btnRename?.classList.toggle("hidden", bulk);
    els.btnDelete?.classList.toggle("hidden", bulk);
    // Replacing audio only makes sense for one file at a time.
    els.btnReplaceToggle?.classList.toggle("hidden", bulk);
    closeReplacePanel();
    if (els.metaTitle) {
      els.metaTitle.textContent = bulk ? `Edit ${count} tracks` : "Edit track";
    }
    if (els.btnSave) {
      els.btnSave.textContent = bulk ? `Apply to ${count}` : "Save";
    }
    if (els.artists) {
      els.artists.placeholder = bulk ? "Leave blank to keep" : "";
    }
    if (els.albumArtist) {
      els.albumArtist.placeholder = bulk ? "Leave blank to keep" : "";
    }
    if (els.album) {
      els.album.placeholder = bulk ? "Leave blank to keep" : "";
    }
    if (els.trackNo) {
      els.trackNo.placeholder = bulk ? "Same # for all, or use order" : "";
      els.trackNo.disabled = false;
    }
    if (els.numberSeq) els.numberSeq.checked = false;
    if (els.compilationLabel) {
      els.compilationLabel.textContent = bulk
        ? "Various artists compilation (leave alone to keep)"
        : "Various artists compilation";
    }
    compilationTouched = false;
  }

  async function openEditor(path) {
    const res = await fetch(`/api/library/meta?path=${encodeURIComponent(path)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Could not read tags");
      return;
    }
    bulkPaths = [];
    setEditorMode("single", 1);
    selectedPath = data.path;
    els.title.value = data.title || "";
    els.artists.value = data.artists || "";
    if (els.albumArtist) els.albumArtist.value = data.album_artist || "";
    els.album.value = data.album || "";
    els.trackNo.value = data.track_number || "";
    if (els.compilation) els.compilation.checked = !!data.compilation;
    els.coverUrl.value = "";
    els.filename.textContent = data.name || "";
    if (data.has_cover) {
      setCoverSrc(`/api/library/cover?path=${encodeURIComponent(data.path)}&t=${Date.now()}`);
    } else {
      setCoverSrc("");
    }
    clearMetaCoverSearch();
    els.modal.classList.remove("hidden");
  }

  function openBulkEditor() {
    const paths = [...selected];
    if (!paths.length) return;
    bulkPaths = paths;
    selectedPath = "";
    setEditorMode("bulk", paths.length);
    els.title.value = "";
    els.artists.value = "";
    if (els.albumArtist) els.albumArtist.value = "";
    els.album.value = "";
    els.trackNo.value = "";
    if (els.compilation) els.compilation.checked = false;
    els.coverUrl.value = "";
    els.filename.textContent = `${paths.length} files selected`;
    setCoverSrc("");
    clearMetaCoverSearch();
    els.modal.classList.remove("hidden");
  }

  function closeEditor() {
    els.modal.classList.add("hidden");
    selectedPath = "";
    bulkPaths = [];
    editMode = "single";
    setCoverSrc("");
    clearMetaCoverSearch();
    closeReplacePanel();
  }

  async function saveTags() {
    if (editMode === "bulk") {
      await saveBulkTags();
      return;
    }
    if (!selectedPath) return;
    const body = {
      path: selectedPath,
      title: els.title.value,
      artists: els.artists.value,
      album_artist: els.albumArtist?.value || "",
      album: els.album.value,
      track_number: els.trackNo.value,
      compilation: !!els.compilation?.checked,
      cover_url: els.coverUrl.value.trim() || null,
      remove_cover: false,
    };
    const res = await fetch("/api/library/meta", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Save failed");
      return;
    }
    closeEditor();
    await refresh();
  }

  async function saveBulkTags() {
    if (!bulkPaths.length) return;
    const sequential = !!els.numberSeq?.checked;
    const body = {
      paths: bulkPaths,
      title: null,
      artists: els.artists.value.trim() || null,
      album_artist: els.albumArtist?.value.trim() || null,
      album: els.album.value.trim() || null,
      track_number: sequential ? null : els.trackNo.value.trim() || null,
      number_sequentially: sequential,
      compilation: compilationTouched ? !!els.compilation?.checked : null,
      cover_url: els.coverUrl.value.trim() || null,
      remove_cover: false,
    };
    if (
      !body.artists &&
      !body.album_artist &&
      !body.album &&
      !body.track_number &&
      !body.number_sequentially &&
      body.compilation === null &&
      !body.cover_url
    ) {
      alert("Enter at least one field to apply, or set a cover URL.");
      return;
    }
    els.btnSave.disabled = true;
    try {
      const res = await fetch("/api/library/meta/bulk", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(data.detail || "Bulk save failed");
        return;
      }
      if (data.errors?.length) {
        alert(`Updated ${data.updated} file(s). Some failed:\n${data.errors.slice(0, 5).join("\n")}`);
      }
      els.coverUrl.value = "";
      closeEditor();
      await refresh();
    } finally {
      els.btnSave.disabled = false;
    }
  }

  async function clearCover() {
    if (editMode === "bulk") {
      if (!bulkPaths.length) return;
      if (!window.confirm(`Remove cover art from ${bulkPaths.length} files?`)) return;
      const res = await fetch("/api/library/meta/bulk", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paths: bulkPaths,
          remove_cover: true,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(data.detail || "Could not remove cover");
        return;
      }
      setCoverSrc("");
      await refresh();
      return;
    }
    if (!selectedPath) return;
    if (!window.confirm("Remove embedded cover art?")) return;
    const res = await fetch("/api/library/meta", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: selectedPath,
        title: els.title.value,
        artists: els.artists.value,
        album_artist: els.albumArtist?.value || "",
        album: els.album.value,
        track_number: els.trackNo.value,
        compilation: !!els.compilation?.checked,
        remove_cover: true,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Could not remove cover");
      return;
    }
    setCoverSrc("");
    await refresh();
  }

  async function uploadCover(file) {
    if (!file) return;
    if (editMode === "bulk") {
      if (!bulkPaths.length) return;
      els.btnSave.disabled = true;
      let ok = 0;
      const errors = [];
      try {
        for (const path of bulkPaths) {
          const form = new FormData();
          form.append("file", file);
          const res = await fetch(
            `/api/library/cover?path=${encodeURIComponent(path)}`,
            { method: "POST", body: form },
          );
          if (res.ok) ok += 1;
          else {
            const data = await res.json().catch(() => ({}));
            errors.push(`${path.split(/[/\\]/).pop()}: ${data.detail || res.status}`);
          }
        }
        if (file.type.startsWith("image/")) {
          coverObjectUrl = URL.createObjectURL(file);
          els.cover.src = coverObjectUrl;
          els.cover.style.visibility = "visible";
        }
        if (errors.length) {
          alert(`Cover set on ${ok} file(s). Some failed:\n${errors.slice(0, 5).join("\n")}`);
        }
        await refresh();
      } finally {
        els.btnSave.disabled = false;
      }
      return;
    }
    if (!selectedPath) return;
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(
      `/api/library/cover?path=${encodeURIComponent(selectedPath)}`,
      { method: "POST", body: form },
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(data.detail || "Cover upload failed");
      return;
    }
    setCoverSrc(`/api/library/cover?path=${encodeURIComponent(selectedPath)}&t=${Date.now()}`);
    await refresh();
  }

  async function replaceAudio() {
    if (editMode === "bulk" || !selectedPath) return;
    const url = (els.replaceUrl?.value || "").trim();
    if (!url) {
      setReplaceStatus("Paste a YouTube video URL first.", { error: true });
      return;
    }
    const busy = [els.btnReplaceGo, els.btnSave, els.btnRename, els.btnDelete];
    busy.forEach((btn) => { if (btn) btn.disabled = true; });
    setReplaceStatus("Downloading and swapping the audio… this can take a minute.");
    try {
      const res = await fetch("/api/library/replace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: selectedPath, youtube_url: url }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setReplaceStatus(data.detail || "Could not replace audio", { error: true });
        return;
      }
      setReplaceStatus("Done — audio replaced, tags kept.");
      if (els.replaceUrl) els.replaceUrl.value = "";
      if (data.has_cover) {
        setCoverSrc(`/api/library/cover?path=${encodeURIComponent(selectedPath)}&t=${Date.now()}`);
      }
      await refresh();
    } catch (err) {
      setReplaceStatus(err.message || "Could not replace audio", { error: true });
    } finally {
      busy.forEach((btn) => { if (btn) btn.disabled = false; });
    }
  }

  function scheduleSearch(value) {
    searchQuery = value;
    if (els.btnSearchClear) els.btnSearchClear.disabled = !value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      render().catch(() => {});
    }, value.trim() ? 280 : 0);
  }

  els.search?.addEventListener("input", () => {
    scheduleSearch(els.search.value || "");
  });
  els.search?.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      ev.preventDefault();
      clearSearch();
      render().catch(() => {});
    }
  });
  els.btnSearchClear?.addEventListener("click", () => {
    clearSearch();
    render().catch(() => {});
  });

  els.btnNew.addEventListener("click", createFolder);
  els.btnCollapse.addEventListener("click", async () => {
    expanded.clear();
    await render();
  });
  els.btnRefresh.addEventListener("click", refresh);
  els.btnEditSelected?.addEventListener("click", openBulkEditor);
  els.btnSelectAll?.addEventListener("click", selectAllVisible);
  els.btnClearSelected?.addEventListener("click", () => {
    selected.clear();
    render();
  });
  function bindBackdropClose(modal, onClose) {
    if (!modal) return;
    // Only close when press + release both hit the scrim — not when the user
    // starts a text selection inside the dialog and drags onto the backdrop.
    let pressedOnBackdrop = false;
    modal.addEventListener("pointerdown", (ev) => {
      pressedOnBackdrop = ev.target === modal;
    });
    modal.addEventListener("click", (ev) => {
      if (pressedOnBackdrop && ev.target === modal) onClose();
      pressedOnBackdrop = false;
    });
  }

  els.btnClose.addEventListener("click", closeEditor);
  bindBackdropClose(els.modal, closeEditor);
  els.btnSave.addEventListener("click", saveTags);
  els.btnRename.addEventListener("click", () => {
    if (selectedPath) {
      renameEntry(selectedPath).then(() => {
        if (selectedPath) openEditor(selectedPath);
      });
    }
  });
  els.btnDelete.addEventListener("click", () => {
    if (selectedPath) deleteEntry(selectedPath);
  });
  els.btnCoverClear.addEventListener("click", clearCover);
  els.btnCoverSearch?.addEventListener("click", () => {
    runMetaCoverSearch();
  });
  els.coverFile.addEventListener("change", () => {
    const file = els.coverFile.files && els.coverFile.files[0];
    uploadCover(file);
    els.coverFile.value = "";
  });
  els.numberSeq?.addEventListener("change", () => {
    if (els.trackNo) els.trackNo.disabled = !!els.numberSeq.checked;
  });
  els.compilation?.addEventListener("change", () => {
    compilationTouched = true;
  });
  els.btnReplaceToggle?.addEventListener("click", () => {
    const isHidden = els.replacePanel?.classList.contains("hidden");
    if (isHidden) {
      els.replacePanel?.classList.remove("hidden");
      els.replaceUrl?.focus();
    } else {
      closeReplacePanel();
    }
  });
  els.btnReplaceGo?.addEventListener("click", () => {
    replaceAudio();
  });
  els.replaceUrl?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      replaceAudio();
    }
  });

  els.btnNameCancel?.addEventListener("click", () => closeNameModal(null));
  els.btnNameConfirm?.addEventListener("click", () => {
    closeNameModal(els.nameInput.value);
  });
  bindBackdropClose(els.nameModal, () => closeNameModal(null));
  els.nameInput?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      closeNameModal(els.nameInput.value);
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      closeNameModal(null);
    }
  });

  openFolder(null);
})();
