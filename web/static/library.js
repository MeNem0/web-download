(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    crumbs: $("lib-crumbs"),
    summary: $("lib-summary"),
    list: $("lib-list"),
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
    album: $("meta-album"),
    trackNo: $("meta-track-no"),
    filename: $("meta-filename"),
    bulkHint: $("meta-bulk-hint"),
    seqWrap: $("meta-seq-wrap"),
    numberSeq: $("meta-number-seq"),
    metaTitle: $("meta-title"),
    btnClose: $("btn-meta-close"),
    btnSave: $("btn-meta-save"),
    btnRename: $("btn-meta-rename"),
    btnDelete: $("btn-meta-delete"),
    btnCoverClear: $("btn-meta-cover-clear"),
    nameModal: $("name-modal"),
    nameTitle: $("name-modal-title"),
    nameDetail: $("name-modal-detail"),
    nameLabel: $("name-modal-label"),
    nameInput: $("name-modal-input"),
    btnNameCancel: $("btn-name-cancel"),
    btnNameConfirm: $("btn-name-confirm"),
  };

  if (!els.list) return;

  let rootPath = "";
  let libPath = "";
  let selectedPath = "";
  let bulkPaths = [];
  let editMode = "single"; // single | bulk
  let coverObjectUrl = null;
  const expanded = new Set();
  const selected = new Set();
  const cache = new Map();
  let nameResolver = null;

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
      const data = await fetchListing(path);
      rootPath = data.root || rootPath;
      libPath = data.path || "";
      await render();
    } catch (err) {
      els.list.innerHTML = `<p class="track-empty">${escapeHtml(err.message || "Error")}</p>`;
    }
  }

  async function render() {
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
            <span class="lib-badge folder">Folder</span>
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
            <span class="lib-badge file">MP3</span>
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
        if (expanded.has(path)) expanded.delete(path);
        else expanded.add(path);
        await render();
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

  function setEditorMode(mode, count) {
    editMode = mode;
    const bulk = mode === "bulk";
    els.bulkHint?.classList.toggle("hidden", !bulk);
    els.seqWrap?.classList.toggle("hidden", !bulk);
    els.titleField?.classList.toggle("hidden", bulk);
    els.btnRename?.classList.toggle("hidden", bulk);
    els.btnDelete?.classList.toggle("hidden", bulk);
    if (els.metaTitle) {
      els.metaTitle.textContent = bulk ? `Edit ${count} tracks` : "Edit track";
    }
    if (els.btnSave) {
      els.btnSave.textContent = bulk ? `Apply to ${count}` : "Save";
    }
    if (els.artists) {
      els.artists.placeholder = bulk ? "Leave blank to keep" : "";
    }
    if (els.album) {
      els.album.placeholder = bulk ? "Leave blank to keep" : "";
    }
    if (els.trackNo) {
      els.trackNo.placeholder = bulk ? "Same # for all, or use order" : "";
      els.trackNo.disabled = false;
    }
    if (els.numberSeq) els.numberSeq.checked = false;
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
    els.album.value = data.album || "";
    els.trackNo.value = data.track_number || "";
    els.coverUrl.value = "";
    els.filename.textContent = data.name || "";
    if (data.has_cover) {
      setCoverSrc(`/api/library/cover?path=${encodeURIComponent(data.path)}&t=${Date.now()}`);
    } else {
      setCoverSrc("");
    }
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
    els.album.value = "";
    els.trackNo.value = "";
    els.coverUrl.value = "";
    els.filename.textContent = `${paths.length} files selected`;
    setCoverSrc("");
    els.modal.classList.remove("hidden");
  }

  function closeEditor() {
    els.modal.classList.add("hidden");
    selectedPath = "";
    bulkPaths = [];
    editMode = "single";
    setCoverSrc("");
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
      album: els.album.value,
      track_number: els.trackNo.value,
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
      album: els.album.value.trim() || null,
      track_number: sequential ? null : els.trackNo.value.trim() || null,
      number_sequentially: sequential,
      cover_url: els.coverUrl.value.trim() || null,
      remove_cover: false,
    };
    if (
      !body.artists &&
      !body.album &&
      !body.track_number &&
      !body.number_sequentially &&
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
        album: els.album.value,
        track_number: els.trackNo.value,
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
  els.coverFile.addEventListener("change", () => {
    const file = els.coverFile.files && els.coverFile.files[0];
    uploadCover(file);
    els.coverFile.value = "";
  });
  els.numberSeq?.addEventListener("change", () => {
    if (els.trackNo) els.trackNo.disabled = !!els.numberSeq.checked;
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
