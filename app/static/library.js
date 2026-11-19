/* A separate catalog panel; imports use one canonical multipart transfer each. */
const Library = (() => {
  const get = (name) => document.getElementById(`library-${name}`);
  let next = null;
  let queryVersion = 0;
  let upload = null;
  let stopQueue = false;
  let importing = false;
  let applying = false;
  let selected = null;
  let debounce = null;

  const summary = (item) => [
    item.format, item.width && item.height ? `${item.width} × ${item.height}` : null,
    item.duration == null ? null : `${item.duration.toLocaleString("pt-BR", { maximumFractionDigits: 2 })} s`,
    fmtBytes(item.bytes),
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
    const text = document.createElement("span");
    text.textContent = item.displayName;
    const meta = document.createElement("small");
    meta.textContent = summary(item);
    text.append(meta);
    button.append(thumbnail(item), text);
    button.onclick = () => openAsset(item);
    entry.append(button);
    return entry;
  }

  async function refresh(append = false) {
    const version = ++queryVersion;
    const params = new URLSearchParams({ q: get("search").value, sort: get("sort").value });
    if (get("kind").value) params.set("kind", get("kind").value);
    if (append && next !== null) params.set("offset", next);
    get("more").hidden = true;
    get("status").textContent = "Carregando mídias…";
    try {
      const result = await api(`/api/library?${params}`);
      if (version !== queryVersion) return;
      if (!append) get("items").replaceChildren();
      get("items").append(...result.items.map(card));
      next = result.next;
      get("more").hidden = next === null;
      get("status").textContent = get("items").children.length ? "" : "Nenhuma mídia encontrada.";
    } catch (err) {
      if (version === queryVersion) get("status").textContent = err.message;
    }
  }

  function transfer(file, meter, status) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      upload = xhr;
      xhr.open("POST", "/api/library");
      xhr.responseType = "json";
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        meter.value = event.loaded / event.total;
        status.textContent = `Enviando · ${Math.floor(meter.value * 100)}%`;
      };
      xhr.upload.onload = () => {
        meter.removeAttribute("value");
        status.textContent = "Verificando e salvando no servidor…";
      };
      xhr.onload = () => {
        if (xhr.status === 201 || xhr.status === 200) resolve(xhr.status);
        else reject(new Error(typeof xhr.response?.detail === "string"
          ? xhr.response.detail : `O servidor recusou o arquivo (HTTP ${xhr.status}).`));
      };
      xhr.onabort = () => reject(new Error("Envio interrompido no navegador."));
      xhr.onerror = () => reject(new Error("A conexão falhou durante o envio."));
      const body = new FormData();
      body.append("file", file);
      xhr.send(body);
    });
  }

  async function importFiles(files) {
    if (importing || !files.length) return;
    importing = true;
    stopQueue = false;
    get("files").disabled = true;
    get("cancel").hidden = false;
    get("queue").replaceChildren();
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
          const code = await transfer(file, meter, status);
          meter.value = 1;
          status.textContent = code === 201 ? "Disponível na biblioteca" : "Este conteúdo já está na biblioteca";
        } catch (err) {
          meter.hidden = true;
          status.textContent = err.message;
        } finally { upload = null; }
      }
    } finally {
      importing = false;
      get("files").disabled = false;
      get("cancel").hidden = true;
      await refresh();
    }
  }

  function openAsset(item) {
    selected = item;
    get("title").textContent = item.displayName;
    get("info").textContent = [summary(item), item.codec,
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
    const target = get("target");
    target.replaceChildren();
    if (Projects.id) {
      if (item.kind === "image" || item.extension === ".gif") {
        slots.catalog.forEach((spec) => target.add(new Option(lbl(spec.label), spec.id)));
      }
      if (item.kind === "video" || item.kind === "animation") {
        target.add(new Option("Fundo animado", "animated"));
      }
      if (item.kind === "audio") {
        sfx.catalog.filter((spec) => spec.convert || sys.at9tool || item.extension === ".at9")
          .forEach((spec) => target.add(new Option(lbl(spec.label), spec.id)));
      }
    }
    get("apply").disabled = !target.options.length;
    target.disabled = !target.options.length;
    get("apply-status").textContent = Projects.id
      ? (target.disabled ? "Nenhum destino compatível." : "") : "Abra um projeto para usar esta mídia.";
    get("asset").showModal();
  }

  async function applyAsset() {
    if (!selected || applying || anim.busy) return;
    const target = get("target");
    const slot = target.value;
    applying = true;
    const closeButton = get("asset").querySelector('button[type="submit"]');
    closeButton.disabled = true;
    get("apply").disabled = true;
    target.disabled = true;
    get("apply-status").textContent = "Processando para o projeto…";
    try {
      await useLibraryAsset(selected.id, slot);
      get("apply-status").textContent = "Mídia aplicada ao editor.";
    } catch (err) {
      get("apply-status").textContent = err.message;
    } finally {
      get("apply").disabled = false;
      target.disabled = false;
      closeButton.disabled = false;
      applying = false;
    }
  }

  function boot() {
    const panel = document.getElementById("media-library");
    panel.ontoggle = () => { if (panel.open) refresh(); };
    if (panel.open) refresh();
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
    get("cancel").onclick = () => { stopQueue = true; upload?.abort(); };
    get("apply").onclick = applyAsset;
    get("asset").addEventListener("close", () => get("preview").replaceChildren());
    get("asset").addEventListener("cancel", (event) => { if (applying) event.preventDefault(); });
  }

  return { boot };
})();
