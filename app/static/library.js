/* Separate media catalog panel with 5 dedicated folders and streaming chunked
 * upload fallback (CzarUploader) to defeat reverse-proxy HTTP 413 limits.
 */
const Library = (() => {
  const get = (name) => document.getElementById(`library-${name}`);
  let next = null;
  let queryVersion = 0;
  let uploadAbort = null;
  let stopQueue = false;
  let importing = false;
  let applying = false;
  let selected = null;
  let activeFolder = "";
  let debounce = null;

  const FOLDERS = {
    content_icons: {
      id: "content_icons",
      label: "Ícones de conteúdo",
      accept: "image/*,.svg,image/svg+xml",
      kinds: new Set(["image"]),
    },
    function_icons: {
      id: "function_icons",
      label: "Ícones de função",
      accept: "image/*,.svg,image/svg+xml",
      kinds: new Set(["image"]),
    },
    audio: {
      id: "audio",
      label: "Áudio",
      accept: "audio/*,.at9,.wav,.mp3,.flac,.ogg,.m4a,.aac,.opus",
      kinds: new Set(["audio"]),
    },
    theme_image: {
      id: "theme_image",
      label: "Imagem de tema",
      accept: "image/*,.svg,image/svg+xml",
      kinds: new Set(["image"]),
    },
    animated_bg: {
      id: "animated_bg",
      label: "Fundo animado",
      accept: "video/*,.gif,.apng,.mp4,.mov,.mkv,.webm,.avi,.m4v",
      kinds: new Set(["video", "animation"]),
    },
  };

  const folderLabel = (fid) => FOLDERS[fid]?.label || "Imagem de tema";

  const summary = (item) => [
    item.format,
    item.width && item.height ? `${item.width} × ${item.height}` : null,
    item.duration == null ? null : `${item.duration.toLocaleString("pt-BR", { maximumFractionDigits: 2 })} s`,
    fmtBytes(item.bytes),
    item.normalized?.normalized ? "720p PS4" : null,
  ].filter(Boolean).join(" · ");

  function thumbnail(item) {
    if (item.thumbnail) {
      const image = document.createElement("img");
      image.src = item.thumbnail;
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.width = 320;
      image.height = 180;
      return image;
    }
    const placeholder = document.createElement("span");
    placeholder.className = "library-placeholder";
    placeholder.textContent = "Áudio";
    return placeholder;
  }

  function card(item) {
    const entry = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "library-card";
    button.dataset.folder = item.folder || "";
    const text = document.createElement("span");
    text.textContent = item.displayName;
    const meta = document.createElement("small");
    meta.textContent = summary(item);
    const badge = document.createElement("span");
    badge.className = "library-folder-badge";
    badge.textContent = item.folderLabel || folderLabel(item.folder);
    text.append(meta, badge);
    button.append(thumbnail(item), text);
    button.onclick = () => openAsset(item);
    entry.append(button);
    return entry;
  }

  function updateFolderTabs(counts) {
    const nav = get("folders");
    if (!nav) return;
    let total = 0;
    if (counts && typeof counts === "object") {
      if (typeof counts.all === "number") {
        total = counts.all;
      } else {
        for (const fid of Object.keys(FOLDERS)) total += Number(counts[fid] || 0);
      }
    }
    for (const tab of nav.querySelectorAll(".library-folder-tab")) {
      const fid = tab.dataset.folder || "";
      const active = fid === activeFolder;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      const badge = tab.querySelector(".folder-count");
      if (badge && counts) {
        badge.textContent = String(fid ? Number(counts[fid] || 0) : total);
      }
    }
    const labelEl = get("files-label");
    if (labelEl) {
      labelEl.textContent = activeFolder
        ? `Importar em "${folderLabel(activeFolder)}"`
        : "Importar arquivos";
    }
    const input = get("files");
    if (input) {
      if (activeFolder && FOLDERS[activeFolder]) {
        input.setAttribute("accept", FOLDERS[activeFolder].accept);
      } else {
        input.removeAttribute("accept");
      }
    }
  }

  function setFolder(fid) {
    activeFolder = FOLDERS[fid] ? fid : "";
    updateFolderTabs(null);
    refresh();
  }

  async function refresh(append = false) {
    const version = ++queryVersion;
    const params = new URLSearchParams({ q: get("search").value, sort: get("sort").value });
    if (get("kind").value) params.set("kind", get("kind").value);
    if (activeFolder) params.set("folder", activeFolder);
    if (append && next !== null) params.set("offset", next);
    get("more").hidden = true;
    get("status").textContent = "Carregando mídias…";
    try {
      const result = await api(`/api/library?${params}`);
      if (version !== queryVersion) return;
      if (result.counts) updateFolderTabs(result.counts);
      if (!append) get("items").replaceChildren();
      get("items").append(...result.items.map(card));
      next = result.next;
      get("more").hidden = next === null;
      get("status").textContent = get("items").children.length ? "" : "Nenhuma mídia encontrada.";
    } catch (err) {
      if (version === queryVersion) get("status").textContent = err.message;
    }
  }

  async function transfer(file, meter, status, targetFolder) {
    const controller = new AbortController();
    uploadAbort = controller;
    const folder = targetFolder || activeFolder || "";
    const res = await window.CzarUploader.upload(file, {
      purpose: "library",
      params: folder ? { folder } : {},
      directUrl: "/api/library",
      directFields: folder ? { folder } : {},
      signal: controller.signal,
      abortMessage: "Envio interrompido no navegador.",
      onProgress: (ev) => {
        if (ev.phase === "upload" && typeof ev.ratio === "number") {
          meter.value = ev.ratio;
          const pct = Math.floor(ev.ratio * 100);
          status.textContent = ev.mode === "chunked"
            ? `Enviando em blocos · ${pct}%`
            : `Enviando · ${pct}%`;
        } else if (ev.phase === "processing") {
          if (typeof ev.ratio === "number") meter.value = ev.ratio;
          else meter.removeAttribute("value");
          if (ev.stage) status.textContent = ev.stage;
          else status.textContent = "Verificando e salvando no servidor…";
        }
      },
      onStage: (stageTxt) => {
        meter.removeAttribute("value");
        status.textContent = stageTxt;
      },
    });
    return { code: res.status, asset: res.data };
  }

  async function importFiles(files, onAsset, folderOverride = null) {
    if (importing || !files.length) return;
    importing = true;
    stopQueue = false;
    get("files").disabled = true;
    get("cancel").hidden = false;
    get("queue").replaceChildren();
    const targetFolder = folderOverride !== null ? folderOverride : activeFolder;
    const queue = files.map((file) => {
      const line = document.createElement("li");
      const meter = document.createElement("progress");
      meter.max = 1;
      meter.value = 0;
      meter.setAttribute("aria-label", file.name);
      const status = document.createElement("span");
      status.setAttribute("role", "status");
      status.textContent = "Na fila";
      line.append(file.name, meter, status);
      get("queue").append(line);
      return { file, meter, status };
    });
    try {
      for (const { file, meter, status } of queue) {
        if (stopQueue) {
          meter.hidden = true;
          status.textContent = "Envio cancelado";
          continue;
        }
        try {
          const { code, asset } = await transfer(file, meter, status, targetFolder);
          meter.value = 1;
          status.textContent = code === 201 ? "Disponível na biblioteca" : "Este conteúdo já está na biblioteca";
          if (onAsset) {
            try { onAsset(asset); }
            catch (err) { status.textContent += ` · ${err.message}`; }
          }
        } catch (err) {
          meter.hidden = true;
          status.textContent = err.message;
        } finally { uploadAbort = null; }
      }
    } finally {
      importing = false;
      get("files").disabled = false;
      get("cancel").hidden = true;
      await refresh();
    }
  }

  function orderedImageSlots(folder) {
    const all = [...slots.catalog];
    const priority = (spec) => {
      if (folder === "function_icons") return spec.group === "function_icon" ? 0 : (spec.group === "content_icon" ? 1 : 2);
      if (folder === "content_icons") return spec.group === "content_icon" ? 0 : (spec.group === "function_icon" ? 1 : 2);
      if (folder === "theme_image") return spec.group === "background" ? 0 : 1;
      return 0;
    };
    return all.sort((a, b) => priority(a) - priority(b));
  }

  function openAsset(item) {
    selected = item;
    get("title").textContent = item.displayName;
    get("info").textContent = [
      item.folderLabel || folderLabel(item.folder),
      summary(item),
      item.codec,
      item.fps == null ? null : `${item.fps.toLocaleString("pt-BR", { maximumFractionDigits: 2 })} FPS`,
    ].filter(Boolean).join(" · ");
    const link = get("original");
    link.href = item.original;
    link.download = item.originalName;
    const preview = get("preview");
    preview.replaceChildren();
    if (item.kind === "video" && ["video/mp4", "video/webm"].includes(item.mime) ||
        item.kind === "audio" && item.extension !== ".at9" &&
        ["audio/wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/mp4"].includes(item.mime)) {
      const element = document.createElement(item.kind);
      element.controls = true;
      element.preload = "none";
      element.src = `${item.original}?play=true`;
      if (item.kind === "video" && item.thumbnail) element.poster = item.thumbnail;
      preview.append(element);
    } else if (item.thumbnail) {
      const image = thumbnail(item);
      image.alt = `Miniatura de ${item.displayName}`;
      if (["image/gif", "image/apng"].includes(item.mime)) {
        image.src = `${item.original}?play=true`;
        image.alt = item.displayName;
      }
      preview.append(image);
    }

    const folderSel = get("asset-folder");
    if (folderSel) {
      folderSel.replaceChildren();
      for (const f of Object.values(FOLDERS)) {
        if (f.kinds.has(item.kind)) {
          folderSel.add(new Option(f.label, f.id));
        }
      }
      if (item.folder) folderSel.value = item.folder;
      folderSel.disabled = folderSel.options.length <= 1;
    }

    const target = get("target");
    target.replaceChildren();
    if (Projects.id) {
      if (item.kind === "video" || item.kind === "animation") {
        target.add(new Option("Fundo animado", "animated"));
      }
      if (item.kind === "image" || item.extension === ".gif") {
        orderedImageSlots(item.folder).forEach((spec) => target.add(new Option(lbl(spec.label), spec.id)));
      }
      if (item.kind === "audio") {
        sfx.catalog.filter((spec) => spec.convert || sys.at9tool || item.extension === ".at9")
          .forEach((spec) => target.add(new Option(lbl(spec.label), spec.id)));
      }
    }
    get("apply").disabled = !target.options.length;
    get("layer").hidden = !Projects.id || !Compositor.supported(item);
    target.disabled = !target.options.length;
    get("apply-status").textContent = Projects.id
      ? (target.disabled ? "Nenhum destino compatível." : "") : "Abra um projeto para usar esta mídia.";
    get("asset").showModal();
  }

  async function changeAssetFolder() {
    const folderSel = get("asset-folder");
    if (!selected || !folderSel || !folderSel.value) return;
    const nextFolder = folderSel.value;
    folderSel.disabled = true;
    get("apply-status").textContent = "Movendo para pasta…";
    try {
      const res = await fetch(`/api/library/${encodeURIComponent(selected.id)}/folder`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: nextFolder }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      selected = data;
      get("apply-status").textContent = `Movido para "${data.folderLabel || folderLabel(nextFolder)}".`;
      await refresh();
    } catch (err) {
      get("apply-status").textContent = err.message;
    } finally {
      folderSel.disabled = folderSel.options.length <= 1;
    }
  }

  async function applyAsset() {
    if (!selected || applying || anim.busy) return;
    const target = get("target");
    const slot = target.value;
    applying = true;
    const closeButton = get("asset").querySelector('button[type="submit"]');
    closeButton.disabled = true;
    get("apply").disabled = true;
    get("layer").disabled = true;
    target.disabled = true;
    get("apply-status").textContent = "Processando para o projeto…";
    try {
      await useLibraryAsset(selected.id, slot);
      get("apply-status").textContent = "Mídia aplicada ao editor.";
    } catch (err) {
      get("apply-status").textContent = err.message;
    } finally {
      get("apply").disabled = false;
      get("layer").disabled = false;
      target.disabled = false;
      closeButton.disabled = false;
      applying = false;
    }
  }

  function boot() {
    const panel = document.getElementById("media-library");
    panel.ontoggle = () => { if (panel.open) refresh(); };
    if (panel.open) refresh();

    const nav = get("folders");
    if (nav) {
      nav.addEventListener("click", (event) => {
        const btn = event.target.closest(".library-folder-tab");
        if (!btn) return;
        setFolder(btn.dataset.folder || "");
      });
    }
    const folderSel = get("asset-folder");
    if (folderSel) folderSel.onchange = changeAssetFolder;

    get("search").oninput = () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => refresh(), 200);
    };
    get("kind").onchange = get("sort").onchange = () => refresh();
    get("view").onchange = () => get("items").classList.toggle("is-list", get("view").value === "list");
    get("more").onclick = () => refresh(true);
    get("files").onchange = () => {
      const files = [...get("files").files];
      get("files").value = "";
      importFiles(files);
    };
    get("cancel").onclick = () => { stopQueue = true; uploadAbort?.abort(); };
    get("apply").onclick = applyAsset;
    get("layer").onclick = () => {
      try {
        Compositor.add(selected);
        get("asset").close();
        document.getElementById("media-library").open = false;
        document.querySelector('.tab[data-tab="compositor"]').click();
      } catch (err) { get("apply-status").textContent = err.message; }
    };
    get("asset").addEventListener("close", () => get("preview").replaceChildren());
    get("asset").addEventListener("cancel", (event) => { if (applying) event.preventDefault(); });
    updateFolderTabs(null);
  }

  return { boot, importFiles, setFolder, refresh };
})();
