/* Logical coordinates come from the canonical output, not the viewport. */
const Compositor = (() => {
  const ui = (id) => document.getElementById(`comp-${id}`);
  const canvas = ui("canvas");
  const ctx = canvas.getContext("2d");
  const clone = (value) => structuredClone(value);
  const limit = (value, min, max) => Math.min(max, Math.max(min, value));
  const radians = (degrees) => degrees * Math.PI / 180;
  const rotate = (x, y, theta) => ({ x: x * Math.cos(theta) - y * Math.sin(theta),
                                     y: x * Math.sin(theta) + y * Math.cos(theta) });
  const tab = (name) => document.querySelector(`.tab[data-tab="${name}"]`);
  const active = (name) => tab(name).classList.contains("is-active");
  const locked = (layer) => !layer || layer.locked;
  let state = { version: 1, surfaces: {} };
  let selection = null;
  let past = [], future = [];
  const assets = new Map(), baseImages = new Map();
  let view = { zoom: 1, x: 0, y: 0 };
  let pan = false, gesture = null;
  const pointers = new Map();
  let readyKey = "", requestKey = "", errorKey = "", previewAttempt = 0;
  let previews = {}, animation = [];
  let previewAbort = null;

  const destination = () => ui("target").value || "home";
  function dimensions(target = destination()) {
    return target === "animated" ? sys.sizes.static
      : slots.catalog.find((spec) => spec.id === target)?.size || sys.sizes.static;
  }
  function surface(target = destination()) {
    return state.surfaces[target] || { base: target === "home" || target === "function", layers: [] };
  }
  const selected = () => surface().layers.find((layer) => layer.id === selection);
  const has = (target) => Boolean(state.surfaces[target]?.layers.length);
  const capture = () => Object.keys(state.surfaces).length ? clone(state) : null;
  const status = (message) => { ui("status").textContent = message; };
  const supported = (item) => ["image", "animation"].includes(item.kind) &&
    sys.accepted.image.includes(item.extension);

  function restore(value) {
    state = value ? clone(value) : { version: 1, surfaces: {} };
    past = []; future = []; selection = null;
  }

  function remember(before) {
    if (JSON.stringify(before) === JSON.stringify(state)) return;
    past.push(before);
    if (past.length > 50) past.shift();
    future = [];
    sync();
  }

  function sync() {
    guardarSesion();
    redrawControls();
    draw();
    refreshChecklist();
  }

  function amend(change) {
    const before = clone(state);
    change();
    remember(before);
  }

  function history(source, next) {
    if (!source.length) return;
    next.push(clone(state));
    state = source.pop();
    sync();
  }

  function sourceFor(target) {
    return slots.chosen[target] || slots.chosen[slots.catalog.find((spec) => spec.id === target)?.fallback];
  }

  function loadImage(map, id, url) {
    if (map.has(id)) return map.get(id);
    const image = new Image();
    const result = { image, ready: false, error: false };
    map.set(id, result);
    image.onload = () => { result.ready = true; draw(); };
    image.onerror = () => { result.error = true; draw(); };
    image.src = Projects.mediaUrl(url);
    return result;
  }

  function cropBox(sourceWidth, sourceHeight, layer) {
    let sx = 0, sy = 0, sw = sourceWidth, sh = sourceHeight;
    let w = layer.width, h = layer.height;
    if (layer.fit === "contain") {
      const factor = Math.min(w / sw, h / sh);
      w = sw * factor; h = sh * factor;
    } else if (layer.fit === "cover") {
      if (sw * h > sh * w) {
        sw = Math.max(1, Math.round(sh * w / h));
        sx = Math.floor((sourceWidth - sw) / 2);
      } else {
        sh = Math.max(1, Math.round(sw * h / w));
        sy = Math.floor((sourceHeight - sh) / 2);
      }
    }
    return [sx, sy, sw, sh, -w / 2, -h / 2, w, h];
  }

  function screenRadius() {
    return canvas.width / (canvas.getBoundingClientRect().width || canvas.width) * 22 / view.zoom;
  }
  function handles(layer) {
    const theta = radians(layer.angle);
    const at = (x, y) => {
      const pos = rotate(x, y, theta);
      return { x: pos.x + layer.x, y: pos.y + layer.y };
    };
    return { resize: at(layer.width / 2, layer.height / 2),
             rotate: at(0, -layer.height / 2 - screenRadius() * 2.3) };
  }

  function draw() {
    if (!Projects.id || !sys || !active("compositor")) return;
    const target = destination();
    const [w, h] = dimensions();
    if (canvas.width !== w || canvas.height !== h) [canvas.width, canvas.height] = [w, h];
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.setTransform(view.zoom, 0, 0, view.zoom, view.x, view.y);
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip();
    const current = surface();
    if (target === "home" || target === "function") {
      ctx.fillStyle = "#000"; ctx.fillRect(0, 0, w, h);
    }
    if (target === "animated" && anim.bitmaps.length) {
      ctx.drawImage(Motion.base(draw) || anim.bitmaps[0], 0, 0, w, h);
    }
    const base = current.base && target !== "animated" && sourceFor(target);
    if (base) {
      const item = loadImage(baseImages, base, `/api/slot/${base}/preview`);
      if (item.ready) {
        const sourceWidth = item.image.naturalWidth, sourceHeight = item.image.naturalHeight;
        const factor = Math.max(w / sourceWidth, h / sourceHeight);
        ctx.drawImage(item.image, (w - sourceWidth * factor) / 2, (h - sourceHeight * factor) / 2,
                      sourceWidth * factor, sourceHeight * factor);
      }
    }
    let missing = false;
    for (const layer of current.layers) {
      const item = loadImage(assets, layer.asset, `/api/composition/raster/${layer.asset}`);
      missing ||= item.error;
      if (!item.ready || !layer.visible) continue;
      ctx.save();
      ctx.translate(layer.x, layer.y); ctx.rotate(radians(layer.angle));
      ctx.globalAlpha = layer.opacity;
      ctx.drawImage(item.image, ...cropBox(item.image.naturalWidth, item.image.naturalHeight, layer));
      ctx.restore();
    }
    ctx.restore();
    ctx.strokeStyle = "#8abdff90";
    ctx.lineWidth = screenRadius() / 15;
    if (ui("snap").checked) {
      ctx.setLineDash([screenRadius() / 3, screenRadius() / 3]);
      ctx.beginPath(); ctx.moveTo(w / 2, 0); ctx.lineTo(w / 2, h);
      ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
      ctx.setLineDash([]);
    }
    const layer = selected();
    if (layer && layer.visible) {
      ctx.save();
      ctx.translate(layer.x, layer.y); ctx.rotate(radians(layer.angle));
      ctx.strokeStyle = "#78c9ff"; ctx.lineWidth = screenRadius() / 9;
      ctx.strokeRect(-layer.width / 2, -layer.height / 2, layer.width, layer.height);
      ctx.restore();
      if (!layer.locked) {
        ctx.fillStyle = "#1c6da9"; ctx.strokeStyle = "#fff";
        for (const point of Object.values(handles(layer))) {
          ctx.beginPath(); ctx.arc(point.x, point.y, screenRadius() * 0.72, 0, Math.PI * 2);
          ctx.fill(); ctx.stroke();
        }
      }
    }
    if (missing) status("Uma camada está indisponível. Restaure a mídia ou remova a referência antes de exportar.");
    const [aw, ah] = anim.size || sys.animated_sizes[0].size;
    const [ex, ey] = layer ? extents(layer) : [0, 0];
    const outside = layer && (layer.x - ex < -0.01 || layer.y - ey < -0.01 ||
                             layer.x + ex > w + 0.01 || layer.y + ey > h + 0.01);
    const hint = target === "animated"
      ? `Coordenadas ${w} × ${h} · saída ${aw} × ${ah} · ${anim.jobId ? "quadro de referência" : "importe a fonte animada na aba Fundo"}`
      : `${w} × ${h} px · ${layer?.fit === "cover" ? "recorte central ativo" :
                    layer?.fit === "stretch" ? "imagem esticada pelo enquadramento" : "sem recorte de enquadramento"}`;
    ui("hint").textContent = outside ? `${hint} · parte da camada fica fora do quadro` : hint;
    ui("zoom").textContent = `${Math.round(view.zoom * 100)}%`;
  }

  function redrawControls() {
    if (!Projects.id || !sys) return;
    const layer = selected(), current = surface();
    ui("undo").disabled = !past.length;
    ui("redo").disabled = !future.length;
    ui("base").checked = current.base;
    ui("base").disabled = destination() === "animated" || !current.layers.length;
    ui("layers").replaceChildren(...[...current.layers].reverse().map((item) => {
      const row = document.createElement("li"), button = document.createElement("button");
      button.className = "btn btn-ghost comp-layer";
      button.textContent = item.name + (item.visible ? "" : " · oculta") + (item.locked ? " · bloqueada" : "");
      button.setAttribute("aria-pressed", String(item.id === selection));
      button.onclick = () => select(item.id);
      row.append(button);
      return row;
    }));
    ui("properties").disabled = locked(layer);
    for (const input of ui("properties").querySelectorAll("[data-field]")) {
      const value = layer?.[input.dataset.field];
      input.value = value == null ? "" : Math.round(value * (input.dataset.field === "opacity" ? 100 : 1) * 100) / 100;
    }
    ui("proportion").checked = layer?.proportional ?? true;
    ui("fit").value = layer?.fit || "contain";
    for (const button of ui("layer-actions").querySelectorAll("button")) {
      const action = button.dataset.action;
      button.disabled = !layer || (layer.locked && action !== "lock");
      if (action === "visibility") button.textContent = layer?.visible ? "Ocultar" : "Mostrar";
      if (action === "lock") button.textContent = layer?.locked ? "Desbloquear" : "Bloquear";
    }
  }

  function select(id) {
    selection = id;
    redrawControls();
    draw();
  }
  function room(target) {
    return surface(target).layers.length < 16 &&
      Object.values(state.surfaces).reduce((count, item) => count + item.layers.length, 0) < 48;
  }
  function add(item, target = destination()) {
    if (!Projects.id || !supported(item)) throw new Error("Escolha uma imagem raster da biblioteca.");
    if (!room(target)) throw new Error("Limite de 16 camadas por superfície e 48 por projeto.");
    const [w, h] = dimensions(target);
    const aspect = item.width && item.height ? item.width / item.height : w / h;
    const width = Math.min(w * 0.8, h * 0.8 * aspect);
    const layer = {
      id: crypto.randomUUID(), asset: item.id, name: (item.displayName || "Camada").slice(0, 160),
      x: w / 2, y: h / 2, width: Math.max(1, width), height: Math.max(1, width / aspect),
      angle: 0, opacity: 1, fit: "contain", proportional: true, visible: true, locked: false,
    };
    amend(() => {
      state.surfaces[target] ||= surface(target);
      state.surfaces[target].layers.push(layer);
      if (target === destination()) selection = layer.id;
    });
    status(`Camada adicionada · ${layer.name}`);
  }

  function setSize(layer, width, height, driver) {
    if (layer.proportional) {
      const requested = driver === "height" ? height / layer.height : width / layer.width;
      const scale = limit(requested, 1 / Math.min(layer.width, layer.height), 7680 / Math.max(layer.width, layer.height));
      layer.width *= scale; layer.height *= scale;
    } else {
      layer.width = limit(width, 1, 7680);
      layer.height = limit(height, 1, 7680);
    }
  }

  function setNumber(input) {
    const layer = selected(), value = input.valueAsNumber, field = input.dataset.field;
    if (locked(layer) || !Number.isFinite(value)) { redrawControls(); return; }
    amend(() => {
      if (field === "width" || field === "height") {
        setSize(layer, field === "width" ? value : layer.width, field === "height" ? value : layer.height, field);
      } else if (field === "opacity") layer.opacity = limit(value / 100, 0, 1);
      else if (field === "angle") layer.angle = limit(value, -360, 360);
      else layer[field] = limit(value, -7680, 7680);
    });
    redrawControls();
  }

  function extents(layer) {
    const c = Math.abs(Math.cos(radians(layer.angle))), s = Math.abs(Math.sin(radians(layer.angle)));
    return [(c * layer.width + s * layer.height) / 2, (s * layer.width + c * layer.height) / 2];
  }
  function align(action) {
    const layer = selected();
    if (locked(layer)) return;
    const [w, h] = dimensions();
    amend(() => {
      if (action === "fit" || action === "fill") {
        Object.assign(layer, { x: w / 2, y: h / 2, width: w, height: h, angle: 0,
                               fit: action === "fit" ? "contain" : "cover" });
      } else if (action === "natural") {
        const item = assets.get(layer.asset);
        if (!item?.ready) return;
        const crop = cropBox(item.image.naturalWidth, item.image.naturalHeight, { ...layer, fit: "contain" });
        Object.assign(layer, { width: Math.max(1, crop[6]), height: Math.max(1, crop[7]), fit: "contain" });
      } else if (action.startsWith("distribute-")) {
        const field = action.at(-1);
        const items = surface().layers.filter((item) => item.visible && !item.locked)
          .sort((a, b) => a[field] - b[field]);
        if (items.length < 3) { status("Distribuir requer três camadas visíveis e desbloqueadas."); return; }
        const start = items[0][field], end = items.at(-1)[field];
        items.forEach((item, index) => { item[field] = start + index * (end - start) / (items.length - 1); });
      } else {
        const [dx, dy] = extents(layer);
        if (["left", "center-x", "right"].includes(action)) {
          layer.x = limit({ left: dx, "center-x": w / 2, right: w - dx }[action], -7680, 7680);
        } else {
          layer.y = limit({ top: dy, "center-y": h / 2, bottom: h - dy }[action], -7680, 7680);
        }
      }
    });
  }

  function layerAction(action) {
    const layer = selected(), current = surface();
    if (!layer || (layer.locked && action !== "lock")) return;
    amend(() => {
      const index = current.layers.indexOf(layer);
      if (action === "lock") layer.locked = !layer.locked;
      if (action === "visibility") layer.visible = !layer.visible;
      if (action === "front" && index + 1 < current.layers.length) {
        [current.layers[index], current.layers[index + 1]] = [current.layers[index + 1], layer];
      }
      if (action === "back" && index > 0) {
        [current.layers[index], current.layers[index - 1]] = [current.layers[index - 1], layer];
      }
      if (action === "remove") {
        current.layers.splice(index, 1); selection = null;
        if (!current.layers.length) delete state.surfaces[destination()];
      }
      if (action === "duplicate") {
        if (!room(destination())) { status("Limite de camadas atingido."); return; }
        const newLayer = { ...layer, id: crypto.randomUUID(), x: limit(layer.x + 16, -7680, 7680),
                           y: limit(layer.y + 16, -7680, 7680) };
        current.layers.splice(index + 1, 0, newLayer); selection = newLayer.id;
      }
    });
  }

  function screen(event) {
    const rect = canvas.getBoundingClientRect();
    return { x: (event.clientX - rect.left) * canvas.width / rect.width,
             y: (event.clientY - rect.top) * canvas.height / rect.height };
  }
  const world = (point) => ({ x: (point.x - view.x) / view.zoom, y: (point.y - view.y) / view.zoom });
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const midpoint = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  function zoomAt(zoom, point, anchor = world(point)) {
    view.zoom = limit(zoom, 0.5, 4);
    view.x = point.x - anchor.x * view.zoom;
    view.y = point.y - anchor.y * view.zoom;
    draw();
  }
  function pointerDown(event) {
    if (event.button !== 0 || pointers.size >= 2) return;
    canvas.setPointerCapture(event.pointerId);
    const point = screen(event);
    pointers.set(event.pointerId, point);
    if (pointers.size === 2) {
      if (gesture?.before) state = gesture.before;
      const [a, b] = pointers.values();
      gesture = { mode: "pinch", anchor: world(midpoint(a, b)),
                  distance: Math.max(distance(a, b), 1), zoom: view.zoom };
      redrawControls(); draw();
      return;
    }
    if (pan || event.shiftKey) {
      gesture = { mode: "pan", point, base: { ...view } };
      return;
    }
    const loc = world(point);
    let layer = selected(), mode = "drag";
    if (!locked(layer) && layer.visible) {
      const points = handles(layer);
      if (distance(loc, points.rotate) <= screenRadius()) mode = "rotate";
      else if (distance(loc, points.resize) <= screenRadius()) mode = "resize";
    }
    if (mode === "drag") {
      layer = [...surface().layers].reverse().find((item) => {
        const q = rotate(loc.x - item.x, loc.y - item.y, -radians(item.angle));
        return item.visible && Math.abs(q.x) <= item.width / 2 && Math.abs(q.y) <= item.height / 2;
      });
      select(layer?.id ?? null);
    }
    if (!locked(layer)) gesture = { mode, loc, layer, first: { ...layer }, before: clone(state) };
  }

  function move(event) {
    if (!pointers.has(event.pointerId)) return;
    const point = screen(event);
    pointers.set(event.pointerId, point);
    if (!gesture) return;
    const g = gesture;
    if (g.mode === "pinch") {
      if (pointers.size === 2) {
        const [a, b] = pointers.values();
        zoomAt(g.zoom * distance(a, b) / g.distance, midpoint(a, b), g.anchor);
      }
    } else if (g.mode === "pan") {
      view.x = g.base.x + point.x - g.point.x; view.y = g.base.y + point.y - g.point.y;
      draw();
    } else {
      const now = world(point), dx = now.x - g.loc.x, dy = now.y - g.loc.y;
      const layer = g.layer;
      Object.assign(layer, g.first);
      if (g.mode === "drag") {
        const [w, h] = dimensions(), [ex, ey] = extents(layer);
        const snap = (value, guides) => ui("snap").checked
          ? guides.find((guide) => Math.abs(value - guide) < screenRadius() / 3) ?? value : value;
        layer.x = limit(snap(layer.x + dx, [ex, w / 2, w - ex]), -7680, 7680);
        layer.y = limit(snap(layer.y + dy, [ey, h / 2, h - ey]), -7680, 7680);
      } else if (g.mode === "rotate") {
        const angle = (p) => Math.atan2(p.y - layer.y, p.x - layer.x);
        layer.angle = (g.first.angle + (angle(now) - angle(g.loc)) * 180 / Math.PI) % 360;
      } else {
        const local = rotate(dx, dy, -radians(layer.angle));
        setSize(layer, layer.width + local.x * 2, layer.height + local.y * 2,
                Math.abs(local.x / layer.width) >= Math.abs(local.y / layer.height) ? "width" : "height");
      }
      draw();
    }
  }

  function pointerEnd(event, cancel = false) {
    if (!pointers.delete(event.pointerId) || pointers.size) return;
    if (gesture?.before) {
      if (cancel) { state = gesture.before; redrawControls(); draw(); }
      else remember(gesture.before);
    }
    gesture = null;
  }

  const previewNeeded = () => (bgMode() === "animated" && !!Motion.activeKey()) || Object.keys(state.surfaces).some((target) =>
    has(target) && (target !== "animated" || bgMode() === "animated"));
  const key = () => JSON.stringify([state, slots.chosen, bgMode(), anim.jobId, anim.size, anim.frameCount, Motion.activeKey()]);
  const previewReady = () => readyKey === key();
  const previewImage = (target) => previews[target] ||
    (fallsBackToHome(target) && !slots.chosen[target] && previews.home) || null;
  function fallsBackToHome(target) {
    return slots.catalog.find((spec) => spec.id === target)?.fallback === "home";
  }
  function previewFrameList() { return previewReady() && animation.length ? animation : null; }

  function release(images, frames) {
    Object.values(images).forEach((url) => URL.revokeObjectURL(url));
    frames.forEach((frame) => frame.close());
  }
  function leavePreview() {
    previewAttempt++;
    previewAbort?.abort();
    requestKey = ""; errorKey = "";
  }

  function wireTouchButtons() {
    const editor = ui("editor");
    let pressed = null, handled = false;
    document.addEventListener("pointerdown", () => { handled = false; }, true);
    editor.addEventListener("pointerdown", (event) => {
      const button = event.target.closest("button");
      if (event.pointerType === "touch" && button && !button.disabled) {
        pressed = { button, id: event.pointerId, x: event.clientX, y: event.clientY };
      }
    });
    editor.addEventListener("pointercancel", () => { pressed = null; });
    editor.addEventListener("pointerup", (event) => {
      if (!pressed || pressed.id !== event.pointerId) return;
      const { button, x, y } = pressed;
      pressed = null;
      const released = document.elementFromPoint(event.clientX, event.clientY)?.closest("button");
      if (released !== button || Math.hypot(event.clientX - x, event.clientY - y) > 12 || button.disabled) return;
      // Chrome can suppress a synthesized click after a canvas drag. Activate
      // a finger release on its original control, and ignore any native click
      // that follows this same release. Mouse and keyboard use native clicks.
      handled = true;
      button.click();
    });
    document.addEventListener("click", (event) => {
      if (handled && event.isTrusted && event.detail) {
        handled = false;
        event.stopImmediatePropagation();
        event.preventDefault();
      }
    }, true);
  }

  async function refreshPreview(retry = false) {
    const signature = key();
    if (!active("preview") || (signature === requestKey && !retry) ||
        (signature === errorKey && !retry)) return;
    if (readyKey === signature) { renderPreview(); return; }
    previewAbort?.abort();
    const controller = new AbortController();
    previewAbort = controller;
    const attempt = ++previewAttempt;
    requestKey = signature;
    ui("preview-status").hidden = false;
    ui("preview-status").textContent = "Renderizando a composição para a prévia PS4…";
    ui("preview-retry").hidden = true;
    const images = {}, frames = [];
    const post = (endpoint, body) => Projects.fetchMedia(endpoint, {
      method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" },
      signal: controller.signal,
    });
    async function check(response) {
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(typeof data.detail === "string" ? data.detail : "Falha ao renderizar a composição.");
      }
      return response;
    }
    try {
      if (bgMode() === "animated" && Motion.activeKey()) frames.push(...await Motion.previewFrames(controller.signal));
      for (const [target, part] of Object.entries(state.surfaces)) {
        if (!part.layers.length) continue;
        if (target === "animated") {
          if (bgMode() !== "animated" || Motion.activeKey()) continue;
          if (!anim.jobId) throw new Error("Importe a fonte animada para conferir a composição.");
          const response = await check(await post("/api/composition/animation", {
            job: anim.jobId, size: anim.size, frames: anim.frameCount, surface: part,
          }));
          const packet = await response.json();
          for (const url of packet.frames) {
            const image = await check(await fetch(Projects.mediaUrl(url), { signal: controller.signal }));
            frames.push(await createImageBitmap(await image.blob()));
          }
        } else {
          const result = await check(await post("/api/composition/image", { target, surface: part, base: sourceFor(target) }));
          images[target] = URL.createObjectURL(await result.blob());
        }
      }
      if (attempt !== previewAttempt || signature !== key()) { release(images, frames); return; }
      release(previews, animation);
      previews = images; animation = frames; readyKey = signature; errorKey = "";
      renderPreview();
    } catch (err) {
      release(images, frames);
      if (attempt !== previewAttempt) return;
      errorKey = signature;
      ui("preview-status").textContent = err.message;
      ui("preview-retry").hidden = false;
    } finally {
      if (attempt === previewAttempt) {
        requestKey = "";
        if (signature !== key()) renderPreview();
      }
    }
  }

  function boot() {
    ui("preview-retry").onclick = () => refreshPreview(true);
    if (!Projects.id) return;
    ui("editor").hidden = false;
    ui("no-project").hidden = true;
    const targets = slots.catalog.filter((spec) => ["home", "function"].includes(spec.id) ||
      ["function_icon", "content_icon"].includes(spec.group));
    for (const spec of targets) ui("target").add(new Option(`${lbl(spec.label)} · ${spec.size.join(" × ")}`, spec.id));
    ui("target").add(new Option("Sobreposições do fundo animado", "animated"), 2);
    ui("target").onchange = () => {
      selection = null; view = { zoom: 1, x: 0, y: 0 }; status(""); redrawControls(); draw();
    };
    ui("files").accept = sys.accepted.image.join(",");
    ui("files").onchange = async () => {
      const files = [...ui("files").files], target = destination();
      ui("files").value = "";
      const panel = document.getElementById("media-library");
      panel.open = true;
      await Library.importFiles(files, (asset) => add(asset, target));
    };
    ui("library").onclick = () => {
      const panel = document.getElementById("media-library");
      panel.open = true; panel.scrollIntoView({ block: "start" });
    };
    ui("ps4").onclick = () => tab("preview").click();
    ui("undo").onclick = () => history(past, future);
    ui("redo").onclick = () => history(future, past);
    ui("base").onchange = () => amend(() => { surface().base = ui("base").checked; });
    ui("proportion").onchange = () => amend(() => { selected().proportional = ui("proportion").checked; });
    ui("fit").onchange = () => amend(() => { selected().fit = ui("fit").value; });
    ui("snap").onchange = draw;
    ui("properties").onchange = (e) => { if (e.target.dataset.field) setNumber(e.target); };
    ui("layer-actions").onclick = (e) => { if (e.target.dataset.action) layerAction(e.target.dataset.action); };
    ui("align").onclick = (e) => { if (e.target.dataset.action) align(e.target.dataset.action); };
    ui("pan").onclick = () => { pan = !pan; ui("pan").setAttribute("aria-pressed", String(pan)); };
    ui("zoom").onclick = () => { view = { zoom: 1, x: 0, y: 0 }; draw(); };
    ui("plus").onclick = () => zoomAt(view.zoom * 1.25, { x: canvas.width / 2, y: canvas.height / 2 });
    ui("minus").onclick = () => zoomAt(view.zoom / 1.25, { x: canvas.width / 2, y: canvas.height / 2 });
    canvas.onpointerdown = pointerDown; canvas.onpointermove = move;
    canvas.onpointerup = pointerEnd;
    canvas.onpointercancel = (event) => pointerEnd(event, true);
    canvas.onlostpointercapture = (event) => pointerEnd(event, true);
    wireTouchButtons();
    new ResizeObserver(draw).observe(canvas);
    document.addEventListener("keydown", (event) => {
      if (!active("compositor") || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z" ||
          event.target.matches("input, textarea, select")) return;
      event.preventDefault();
      if (event.shiftKey) history(future, past); else history(past, future);
    });
    redrawControls();
  }

  function activate() { redrawControls(); draw(); }
  return { boot, activate, restore, capture, has, supported, add, previewNeeded, previewReady, previewImage,
           previewFrameList, refreshPreview, leavePreview };
})();
