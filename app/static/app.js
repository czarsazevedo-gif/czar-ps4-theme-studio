/* Asset generator interface.
 *
 * The part that matters is the player: .dds files cannot be drawn in an <img>,
 * so the backend also emits the same frames as .webp and they are animated
 * here on a <canvas>, honouring the `wait` from index.xml. */

const $ = (id) => document.getElementById(id);

/** A label coming from the backend: arrives as {en, es} and is picked by
 *  language. A plain string also works, in case a field is left untranslated. */
const lbl = (x) => (typeof x === "string" ? x : (x?.[lang()] ?? x?.en ?? ""));

/* The <case> `wait` is in SECONDS and takes decimals. Confirmed by reading a
 * real theme's scene: it uses 0.06 (~16 fps) for the rain. */

let sys = null;

/* ------------------------------------------------------------------ util */

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(2)} MB`;
}

let toastTimer = null;
function toast(msg, kind = "error") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (kind === "ok" ? " ok" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === "ok" ? 3500 : 7000);
}

async function api(url, options) {
  const res = await Projects.fetchMedia(url, options);
  let body = null;
  try { body = await res.json(); } catch { /* respuesta sin json */ }
  if (!res.ok) {
    throw new Error(body?.detail || `${res.status} ${res.statusText}`);
  }
  return body;
}

/** Cablea una dropzone: click (input nativo) + arrastrar y soltar. */
function wireDropzone(zoneId, inputId, onFile) {
  const zone = $(zoneId);
  const input = $(inputId);

  input.addEventListener("change", () => {
    if (input.files?.[0]) onFile(input.files[0]);
    input.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.add("is-over");
    })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    zone.addEventListener(ev, () => zone.classList.remove("is-over"))
  );
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("is-over");
    const file = e.dataTransfer?.files?.[0];
    if (file) onFile(file);
  });
}

/* ------------------------------------------------------------------ tabs */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("is-active"));
    tab.classList.add("is-active");
    $(`panel-${tab.dataset.tab}`).classList.add("is-active");
    // Pause what is not visible: each player burns CPU in its own tab.
    if (tab.dataset.tab !== "backgrounds") player.pause();
    if (tab.dataset.tab !== "preview") pvPlayer.stop();
    // These three depend on what was done in the other tabs.
    if (tab.dataset.tab === "package") { refreshChecklist(); renderTree(); }
    if (tab.dataset.tab === "preview") renderPreview();
  });
});

/* ================================================== FONDO ANIMADO */

const anim = {
  jobId: null,
  size: null,        // [w, h]; boot() la fija
  bitmaps: [],
  frameCount: 24,
  wait: 0.1,          // segundos; boot() lo pisa con el valor del backend
  busy: false,
};

/** Selected background mode: "static" or "animated". */
const bgMode = () => (document.querySelector('input[name="bgmode"]:checked')?.value || "static");

/** The animated background only counts when that mode is selected too.
 *
 *  Work is kept when toggling between the two modes instead of being dropped:
 *  changing your mind should not cost you the upload. What the mode does is
 *  decide what gets packaged. */
const animActive = () => bgMode() === "animated" && Boolean(anim.jobId);

const player = {
  playing: false,
  index: 0,
  acc: 0,
  lastTs: 0,
  rafId: 0,

  frameMs() { return anim.wait * 1000; },

  start() {
    if (this.playing || anim.bitmaps.length === 0) return;
    this.playing = true;
    this.lastTs = 0;
    this.rafId = requestAnimationFrame(this.tick);
    $("anim-play").firstElementChild.className = "ico-pause";
    $("anim-play").setAttribute("aria-label", t("bg.pause.aria"));
  },

  pause() {
    if (!this.playing) return;
    this.playing = false;
    cancelAnimationFrame(this.rafId);
    $("anim-play").firstElementChild.className = "ico-play";
    $("anim-play").setAttribute("aria-label", t("bg.play.aria"));
  },

  toggle() { this.playing ? this.pause() : this.start(); },

  tick: (ts) => {
    if (!player.playing) return;
    if (!player.lastTs) player.lastTs = ts;
    // If the tab was in the background the delta can be huge; clamping it
    // stops the animation from jumping when you come back.
    const dt = Math.min(ts - player.lastTs, 250);
    player.lastTs = ts;

    const per = player.frameMs();
    player.acc += dt;
    while (player.acc >= per) {
      player.acc -= per;
      player.index = (player.index + 1) % anim.bitmaps.length;
    }
    player.draw();
    player.rafId = requestAnimationFrame(player.tick);
  },

  seek(i) {
    player.index = i;
    player.acc = 0;
    player.draw();
  },

  draw() {
    const bmp = anim.bitmaps[player.index];
    if (!bmp) return;
    const cv = $("anim-canvas");
    if (anim.size && cv.width !== anim.size[0]) {
      [cv.width, cv.height] = anim.size;
      $("anim-stage-badge").textContent = `${anim.size[0]} × ${anim.size[1]}`;
    }
    cv.getContext("2d").drawImage(bmp, 0, 0, cv.width, cv.height);
    $("anim-scrub").value = String(player.index);
    $("anim-frame-read").textContent = String(player.index).padStart(2, "0");
  },
};

/** Weight bar. DXT1 is fixed-rate, so the total is known exactly without
 *  compressing anything: frames x 460_928 bytes. That is why it can track the
 *  slider live. */
function currentSize() {
  // The <option> carries "640x360" with an ASCII x, not the pretty label:
  // that label used to travel to the backend, fail to parse, and the chosen
  // resolution was silently ignored.
  const v = $("anim-size").value;
  return sys.animated_sizes.find((x) => sizeValue(x) === v) || sys.animated_sizes[0];
}

/** The value that travels to the backend for a resolution. No pretty chars. */
const sizeValue = (x) => `${x.size[0]}x${x.size[1]}`;

function updateBudget() {
  const sz = currentSize();
  const n = anim.frameCount;
  const total = n * sz.frame_bytes;
  const pct = (total / sys.limit_bytes) * 100;
  const over = total > sys.limit_bytes;

  $("budget-figure").textContent = `${fmtBytes(total)} / ${fmtBytes(sys.limit_bytes)}`;

  const fill = $("budget-fill");
  fill.style.width = `${Math.min(pct, 100)}%`;
  fill.className = "budget-fill" + (pct > 90 ? " warn" : "");

  const note = $("budget-note");
  note.className = "budget-note";
  note.textContent = n >= sz.max_frames
    ? t("bg.budget.max", { label: sz.label })
    : t("bg.budget.each", { bytes: fmtBytes(sz.frame_bytes) });

  // `over` should never happen: the slider no longer reaches. If it did, it
  // would be our bug, and better not to allow the export.
  $("anim-export").disabled = over || anim.busy;
}

/** Draws the frame counter as "N / cap": with the number alone, lowering the
 *  resolution made the control look stuck. */
function pintarFrames() {
  $("anim-frames-out").textContent = `${anim.frameCount} / ${currentSize().max_frames}`;
}

/** Adjusts the slider and the texts to the chosen resolution. */
function syncSizeUi() {
  const sz = currentSize();
  const fr = $("anim-frames");
  const tope = sz.max_frames;
  const topeAnterior = Number(fr.max) || tope;

  // If you were AT THE CAP, you stay at the cap. In this app lowering the
  // resolution is for exactly that — fitting more frames — so leaving the
  // value pinned made the resolution change look like a no-op.
  // If you had picked a value below the cap, it is respected: it is yours.
  if (anim.frameCount >= topeAnterior) anim.frameCount = tope;
  anim.frameCount = Math.max(2, Math.min(anim.frameCount, tope));

  // The slider stops exactly at the cap: you cannot pick a combination the
  // console rejects. It used to allow overshooting, and that built a corrupt
  // theme.
  fr.max = String(tope);
  fr.value = String(anim.frameCount);
  pintarFrames();

  const porBytes = Math.floor(sys.limit_bytes / sz.frame_bytes);
  const manda = t(porBytes <= sz.max_frames ? "bg.size.byweight" : "bg.size.bycount");
  $("anim-size-note").textContent =
    t("bg.size.note", { bytes: fmtBytes(sz.frame_bytes), max: tope, limit: manda });
  updateBudget();
}

function updateTiming() {
  const per = anim.wait * 1000;
  $("anim-wait-ms").textContent = per.toFixed(0);
  $("anim-per-frame").textContent = `${per.toFixed(0)} ms`;
  $("anim-eff-fps").textContent = (1000 / per).toFixed(1);
  $("anim-loop-secs").textContent = `${((anim.frameCount * per) / 1000).toFixed(2)} s`;
}

async function loadBitmaps(urls) {
  const results = await Promise.allSettled(
    urls.map(async (url) => {
      const response = await fetch(Projects.mediaUrl(url));
      if (!response.ok) throw new Error("Não foi possível carregar os quadros da animação.");
      return createImageBitmap(await response.blob());
    })
  );
  const failed = results.find((result) => result.status === "rejected");
  if (failed) {
    results.forEach((result) => { if (result.status === "fulfilled") result.value.close?.(); });
    throw failed.reason;
  }
  anim.bitmaps.forEach((bitmap) => bitmap.close?.());
  anim.bitmaps = results.map((result) => result.value);
}

function applyAnimatedPayload(data) {
  anim.jobId = data.job_id;
  syncRafOverlap();
  anim.frameCount = data.budget.frames;
  anim.size = data.budget.size;

  const s = data.source;
  $("anim-src-name").textContent = s.name;
  $("anim-src-meta").textContent =
    t("bg.src.meta", { w: s.width, h: s.height, d: s.duration,
                       codec: s.codec, fps: data.fps_used });

  $("anim-frames").value = String(anim.frameCount);
  pintarFrames();
  $("anim-scrub").max = String(anim.bitmaps.length - 1);
  $("anim-frame-total").textContent = String(anim.bitmaps.length).padStart(2, "0");

  player.seek(0);
  updateBudget();
  updateTiming();
  guardarSesion();
}

function setAnimBusy(busy) {
  anim.busy = busy;
  $("anim-loading").hidden = !busy;
  $("anim-frames").disabled = busy;
  $("anim-export").disabled = busy || anim.frameCount * sys.animated_frame_bytes > sys.limit_bytes;
}

async function animatedUpload(file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("frames", String(anim.frameCount));
  fd.append("size", $("anim-size").value);

  $("anim-drop").hidden = true;
  $("anim-workspace").hidden = false;
  $("anim-result").hidden = true;
  setAnimBusy(true);
  player.pause();

  try {
    const data = await api("/api/animated/upload", { method: "POST", body: fd });
    await loadBitmaps(data.frames);
    applyAnimatedPayload(data);
    player.start();
  } catch (err) {
    toast(err.message);
    resetAnimated();
  } finally {
    setAnimBusy(false);
  }
}

/** Regenerates the frames without re-uploading: the original is still in the
 *  job's workspace. */
async function animatedReframe() {
  if (!anim.jobId || anim.busy) return;
  const wasPlaying = player.playing;
  player.pause();
  setAnimBusy(true);
  $("anim-result").hidden = true;

  const fd = new FormData();
  fd.append("frames", String(anim.frameCount));
  fd.append("size", $("anim-size").value);

  try {
    const data = await api(`/api/animated/${anim.jobId}/frames`, { method: "POST", body: fd });
    await loadBitmaps(data.frames);
    applyAnimatedPayload(data);
    if (wasPlaying) player.start(); else player.draw();
  } catch (err) {
    toast(err.message);
  } finally {
    setAnimBusy(false);
  }
}

async function animatedExport() {
  if (!anim.jobId) return;
  const btn = $("anim-export");
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Convirtiendo a .dds…";

  const fd = new FormData();
  fd.append("frames", String(anim.frameCount));
  fd.append("wait_seconds", String(anim.wait));
  fd.append("backend", $("anim-backend").value || sys.default_backend);

  try {
    const r = await api(`/api/animated/${anim.jobId}/export`, { method: "POST", body: fd });
    $("anim-result").hidden = false;
    $("anim-result").innerHTML = `
      <p class="result-ok">${t("bg.result.ok", { n: r.frames, s: r.elapsed })}</p>
      <div class="result-rows">
        <div class="result-row"><span>.dds totales</span><b>${fmtBytes(r.dds_bytes)}</b></div>
        <div class="result-row"><span>zip</span><b>${fmtBytes(r.zip_bytes)}</b></div>
        <div class="result-row"><span>wait</span><b>${r.wait}</b></div>
        <div class="result-row"><span>encoder</span><b>${r.backend}</b></div>
      </div>
      <pre>${r.index_xml.replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))}</pre>
      <a class="dl-btn" href="${Projects.mediaUrl(r.download)}" download>${t("bg.export")} background.raf.zip</a>`;
    toast("Exportado correctamente.", "ok");
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
    updateBudget();
  }
}

function resetAnimated() {
  player.pause();
  anim.bitmaps.forEach((b) => b.close?.());
  anim.bitmaps = [];
  anim.jobId = null;
  syncRafOverlap();
  guardarSesion();
  $("anim-drop").hidden = false;
  $("anim-workspace").hidden = true;
  $("anim-result").hidden = true;
}

/* ================================================== SLOTS (fondos e iconos)
 *
 * Every theme slot (home background, menu background, each icon...) is
 * independent. The backend applies the fallbacks: whatever you leave empty
 * copies from another slot, so a single image already yields a full theme. */

const slots = {
  catalog: [],          // definiciones que manda el backend
  groups: [],           // grupos, para poder repintar las rejillas
  chosen: {},           // slot -> job_id de lo que has subido
};

/** Marks the backgrounds the animated scene will cover.
 *
 *  With background.raf the console draws the scene on the home screen AND the
 *  top menu, so homescreen.png and functionscreen.png are never seen. Verified
 *  on a console. The slot is not removed because the thumbnail, the icon and
 *  pic1 come from the home one; but saying so saves you from uploading an
 *  image and not understanding why it never shows. */
function syncRafOverlap() {
  // This follows the chosen MODE, not animActive(): the moment you say
  // "animated" it makes no sense to still ask for a top-menu background, even
  // before the video is uploaded. What gets packaged does depend on
  // animActive().
  const hay = bgMode() === "animated";

  document.querySelectorAll(".slot[data-hidden-by-raf]").forEach((el) => {
    const spec = slots.catalog.find((s) => s.id === el.dataset.slot);
    if (!spec) return;
    if (!spec.raf_label) {
      // This one is useless with a scene: out of sight.
      el.hidden = hay;
      return;
    }
    // This one still serves a purpose, just a different one: it gets renamed,
    // and the subtitle stops advertising a file size that is no longer made.
    el.querySelector(".slot-name").textContent = lbl(hay ? spec.raf_label : spec.label);
    el.querySelector(".slot-sub").textContent =
      hay ? "miniatura · icono · ficha" : el.dataset.sub;
    el.classList.toggle("is-repurposed", hay);
  });

  // Slots copying from the home one must call it by its current name.
  document.querySelectorAll(".slot-badge.fb").forEach((b) => {
    b.textContent = hay ? t("slot.copyTheme") : t("slot.copyOf", { from: b.dataset.from });
  });

  $("slots-raf-note").hidden = !hay;
  $("slots-lead").hidden = hay;
  $("bg-animated").hidden = bgMode() !== "animated";
}

function slotCard(spec) {
  const el = document.createElement("div");
  el.className = "slot";
  el.dataset.slot = spec.id;

  const badge = spec.required
    ? `<span class="slot-badge req">${t("slot.required")}</span>`
    : spec.fallback
      ? `<span class="slot-badge fb" data-from="${spec.fallback}">${
          t("slot.copyOf", { from: spec.fallback })}</span>`
      : `<span class="slot-badge">${t("slot.optional")}</span>`;

  if (spec.hidden_by_raf) el.dataset.hiddenByRaf = "1";
  // The original subtitle, to restore it when going back to a still image.
  el.dataset.sub = (spec.file ? `${spec.file}.png · ` : "")
    + spec.size.join("×") + (spec.glow ? " + glow" : "");

  el.innerHTML = `
    <button class="slot-clear" title="Quitar">×</button>
    <div class="slot-shot">
      <span class="slot-empty">${t("slot.empty")}</span>
      ${badge}
    </div>
    <div class="slot-meta">
      <div class="slot-name">${lbl(spec.label)}</div>
      <div class="slot-sub">${spec.file ? `${spec.file}.png · ` : ""}${spec.size.join("×")}${spec.glow ? ` + glow` : ""}</div>
      <div class="slot-raf-warn">lo tapa el fondo animado</div>
    </div>
    <input type="file" accept="image/*">`;

  const input = el.querySelector("input");
  input.addEventListener("change", () => {
    if (input.files?.[0]) uploadSlot(spec.id, input.files[0]);
    input.value = "";
  });

  el.querySelector(".slot-clear").addEventListener("click", (e) => {
    e.preventDefault(); e.stopPropagation();
    clearSlot(spec.id);
  });

  ["dragenter", "dragover"].forEach((ev) =>
    el.addEventListener(ev, (e) => { e.preventDefault(); el.classList.add("is-over"); }));
  ["dragleave", "dragend"].forEach((ev) =>
    el.addEventListener(ev, () => el.classList.remove("is-over")));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("is-over");
    const f = e.dataTransfer?.files?.[0];
    if (f) uploadSlot(spec.id, f);
  });

  return el;
}

function paintSlot(slotId, previewUrl) {
  const el = document.querySelector(`.slot[data-slot="${CSS.escape(slotId)}"]`);
  if (!el) return;
  const shot = el.querySelector(".slot-shot");
  const old = shot.querySelector("img");
  if (old) old.remove();
  if (previewUrl) {
    const img = new Image();
    img.src = previewUrl;
    img.alt = "";
    shot.prepend(img);
    el.classList.add("filled");
  } else {
    el.classList.remove("filled");
  }
}

async function uploadSlot(slotId, file) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    const d = await api(`/api/slot/${encodeURIComponent(slotId)}/upload`, {
      method: "POST", body: fd,
    });
    applySlotPayload(d);
  } catch (err) {
    toast(err.message);
  }
}

function applySlotPayload(data) {
  slots.chosen[data.slot] = data.job_id;
  paintSlot(data.slot, Projects.mediaUrl(data.preview));
  guardarSesion();
}

function clearSlot(slotId) {
  delete slots.chosen[slotId];
  paintSlot(slotId, null);
  guardarSesion();
}

/** Uploads the same image to every slot in a group. */
async function bulkUpload(group, file) {
  const ids = slots.catalog.filter((s) => s.group === group).map((s) => s.id);
  for (const id of ids) await uploadSlot(id, file);
  toast(t("misc.appliedTo", { n: ids.length }), "ok");
}

/* --- musica del menu ---------------------------------------------------
 * With at9tool present anything is converted; without it, the file has to
 * arrive as .at9 and is only validated. */
const bgm = { jobId: null, gain: 0, busy: false, loudness: null };

/** Loudness of the bgm_home.at9 from the reference theme that plays on the
 *  console. A yardstick: a normal song lands ~13 dB above it. */
const BGM_LOUD_REFERENCIA = -32.0;

function paintBgm(d) {
  const info = $("bgm-info");
  if (d) {
    bgm.jobId = d.job_id;
    bgm.loudness = d.loudness;
    if (d.gain_db != null) {
      bgm.gain = d.gain_db;
      $("bgm-gain").value = String(d.gain_db);
    }
    guardarSesion();
    const loud = d.loudness == null ? "" : ` · ${d.loudness} LKFS`;
    info.textContent = `${d.name} — ${d.codec} ${d.rate} Hz, ${d.channels}ch, ${d.duration}s${loud}`;
    info.className = "bgm-info on";
    $("bgm-clear").hidden = false;
    // Changing the volume means re-encoding the .at9, and that needs at9tool.
    // Without it the control could only throw an error on release, so it is
    // not shown. Same rule as everywhere else: what cannot be done safely is
    // not offered.
    $("bgm-gain-row").hidden = !sys.at9tool;
    pintarGanancia(d.loudness);
  } else {
    bgm.jobId = null;
    bgm.gain = 0;
    $("bgm-gain").value = "0";
    info.textContent = t("snd.none");
    info.className = "bgm-info";
    $("bgm-clear").hidden = true;
    $("bgm-gain-row").hidden = true;
    guardarSesion();
  }
}

/** The dB alone says nothing; what orients you is what it compares against. */
function pintarGanancia(loudness) {
  $("bgm-gain-out").textContent = `${bgm.gain >= 0 ? "+" : ""}${bgm.gain.toFixed(1)} dB`;
  const nota = $("bgm-gain-note");
  if (loudness == null) {
    nota.textContent = t("snd.gain.plain");
    return;
  }
  const dif = loudness - BGM_LOUD_REFERENCIA;
  const comparado = Math.abs(dif) < 1.5
    ? t("snd.gain.same")
    : t(dif > 0 ? "snd.gain.above" : "snd.gain.below",
        { n: Math.abs(dif).toFixed(1) });
  nota.textContent = t("snd.gain.vs", { loud: loudness, compared: comparado });
  nota.className = "ctl-note" + (dif > 6 ? " warn" : "");
}

async function aplicarGanancia() {
  if (!bgm.jobId || bgm.busy) return;
  bgm.busy = true;
  $("bgm-gain").disabled = true;
  $("bgm-gain-note").textContent = t("snd.reencoding");
  try {
    const fd = new FormData();
    fd.append("gain_db", String(bgm.gain));
    paintBgm(await api(`/api/audio/${bgm.jobId}/gain`, { method: "POST", body: fd }));
  } catch (err) {
    toast(err.message);
  } finally {
    bgm.busy = false;
    $("bgm-gain").disabled = false;
  }
}

async function uploadBgm(file) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    paintBgm(await api("/api/audio/bgm/upload", { method: "POST", body: fd }));
  } catch (err) {
    toast(err.message);
  }
}

const sfx = { chosen: {}, catalog: [] };

async function uploadSfx(slotId, file, row) {
  const fd = new FormData();
  fd.append("file", file);
  const info = row.querySelector(".bgm-info");
  info.textContent = "convirtiendo…";
  try {
    const d = await api(`/api/audio/${slotId}/upload`, { method: "POST", body: fd });
    applySfxPayload(d, row);
  } catch (err) {
    info.textContent = t("snd.none.sfx");
    info.className = "bgm-info";
    toast(err.message);
  }
}

function applySfxPayload(data, row) {
  sfx.chosen[data.slot] = data.job_id;
  guardarSesion();
  if (!row) return;
  const info = row.querySelector(".bgm-info");
  info.textContent = `${data.name} → ${data.codec}, ${data.duration}s, ${fmtBytes(data.bytes)}`;
  info.className = "bgm-info on";
  row.querySelector(".sfx-clear").hidden = false;
}

async function useLibraryAsset(id, slot) {
  const animated = slot === "animated";
  if (animated) setAnimBusy(true);
  try {
    const data = await api(`/api/library/${id}/apply`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot, size: $("anim-size").value, frames: anim.frameCount }),
    });
    if (animated) {
      await loadBitmaps(data.frames);
      player.pause();
      $("bgmode-animated").checked = true;
      $("anim-drop").hidden = true;
      $("anim-workspace").hidden = false;
      $("anim-result").hidden = true;
      applyAnimatedPayload(data);
      if ($("panel-backgrounds").classList.contains("is-active")) player.start();
    } else if (slot === "bgm") {
      paintBgm(data);
    } else if (sfx.catalog.some((spec) => spec.id === slot)) {
      applySfxPayload(data, document.querySelector(`#sfx-rows [data-slot="${slot}"]`));
    } else {
      applySlotPayload(data);
    }
    refreshChecklist();
    renderTree();
    renderPreview();
  } finally {
    if (animated) setAnimBusy(false);
  }
}

function describeBgmSupport() {
  const nota = $("bgm-note");
  if (!nota) return;
  nota.innerHTML = t(sys.at9tool ? "snd.any" : "snd.at9only");
  $("bgm-file").setAttribute("accept", sys.at9tool ? "audio/*,.at9" : ".at9");
}

async function bootSfx() {
  sfx.catalog = (await api("/api/audio/slots")).slots;
  pintarSfxRows();
}

/** Redraws the effect rows from the already loaded catalogue.
 *  Split out of bootSfx so they can be rebuilt on a language change without
 *  re-fetching anything, keeping whatever is already uploaded. */
function pintarSfxRows() {
  const rows = $("sfx-rows");
  rows.innerHTML = "";
  sfx.catalog.filter((x) => x.convert).forEach((spec) => {
    const row = document.createElement("div");
    row.className = "sfx-row";
    row.dataset.slot = spec.id;
    row.innerHTML = `
      <span class="sfx-name">${lbl(spec.label)}</span>
      <label class="btn btn-ghost">${t("snd.pick")}<input type="file" accept="audio/*" hidden></label>
      <button class="btn btn-ghost sfx-clear" hidden>${t("snd.remove")}</button>
      <span class="bgm-info">${t("snd.none.sfx")}</span>`;
    const input = row.querySelector("input");
    input.addEventListener("change", () => {
      if (input.files?.[0]) uploadSfx(spec.id, input.files[0], row);
      input.value = "";
    });
    row.querySelector(".sfx-clear").addEventListener("click", () => {
      delete sfx.chosen[spec.id];
      row.querySelector(".bgm-info").textContent = t("snd.none.sfx");
      row.querySelector(".bgm-info").className = "bgm-info";
      row.querySelector(".sfx-clear").hidden = true;
      guardarSesion();
    });
    if (sfx.chosen[spec.id]) {
      // Redraw with something already uploaded: keep the state, do not reset.
      row.querySelector(".bgm-info").className = "bgm-info on";
      row.querySelector(".sfx-clear").hidden = false;
    }
    rows.append(row);
  });
}

/** Rebuilds the slot grids from the already loaded catalogue.
 *
 *  Used on startup and on a language change. Images already uploaded are
 *  redrawn from slots.chosen: switching language should not cost you the work
 *  you have done. */
function pintarSlots() {
  for (const group of slots.groups) {
    const grid = $(`grid-${group.id}`);
    if (!grid) continue;
    grid.innerHTML = "";
    slots.catalog
      .filter((s) => s.group === group.id)
      .forEach((s) => grid.append(slotCard(s)));
  }
  for (const [slotId, jobId] of Object.entries(slots.chosen)) {
    paintSlot(slotId, `/api/slot/${jobId}/preview`);
  }
  syncRafOverlap();
}

async function bootSlots() {
  const d = await api("/api/slots");
  slots.catalog = d.slots;
  slots.groups = d.groups;
  pintarSlots();

  const bulk = (inputId, group) => {
    const input = $(inputId);
    input.addEventListener("change", () => {
      if (input.files?.[0]) bulkUpload(group, input.files[0]);
      input.value = "";
    });
  };
  bulk("bulk-fn", "function_icon");
  bulk("bulk-ct", "content_icon");
  const bgmInput = $("bgm-file");
  bgmInput.addEventListener("change", () => {
    if (bgmInput.files?.[0]) uploadBgm(bgmInput.files[0]);
    bgmInput.value = "";
  });
  $("bgm-clear").addEventListener("click", () => paintBgm(null));

  $("clear-icons").addEventListener("click", () => {
    slots.catalog
      .filter((s) => s.group !== "background")
      .forEach((s) => clearSlot(s.id));
  });
}

/* The standalone converter was removed from the interface: the audio slots
 * already convert whatever you throw at them, so it was a second way of doing
 * the same thing. The /api/convert/* endpoints are still live and tested
 * (tests/test_convert.py) in case you want them by hand. */

/* ================================================== SESION
 *
 * A reload used to wipe all 23 uploads. The files are NOT lost: they are
 * still in workspace/ on the server side, the only thing forgotten was each
 * job's id. They go into localStorage and are validated one by one against the
 * backend on startup, because jobs.prune() may have cleaned the old ones:
 * whatever is still alive is restored and the rest is dropped silently. */

const SESION_KEY = "czar.recovery.sesion.v1";
const PKG_CAMPOS = ["pkg-title", "pkg-title-id", "pkg-content-id", "pkg-themecolor",
                    "pkg-fontcolor", "pkg-fontshadow", "pkg-focuscolor"];

let sesionTimer = null;
function captureEditorState() {
  return {
    slots: { ...slots.chosen },
    bgm: bgm.jobId,
    bgmGain: bgm.gain,
    sfx: { ...sfx.chosen },
    bgMode: bgMode(),
    contentIdTouched: pkg.contentIdTouched,
    anim: {
      jobId: anim.jobId, frames: anim.frameCount, wait: anim.wait, size: currentSize().size,
      backend: $("anim-backend").value,
    },
    campos: Object.fromEntries(PKG_CAMPOS.map((id) => [id, $(id).value])),
  };
}

function guardarSesion() {
  if (Projects.id) {
    Projects.changed();
    return;
  }
  // Debounced: writing on every keystroke of the title adds nothing.
  clearTimeout(sesionTimer);
  sesionTimer = setTimeout(() => {
    try {
      localStorage.setItem(SESION_KEY, JSON.stringify(captureEditorState()));
    } catch { /* cuota llena o navegacion privada: no vale la pena molestar */ }
  }, 400);
}

/** Is that job still alive?
 *
 *  HEAD was tried and these routes answer 405: Starlette does not add it on
 *  its own. GET wastes nothing — the .webp it pulls is exactly the one the
 *  <img> is about to draw, so it comes from cache. */
async function jobVive(url) {
  try {
    return (await fetch(url)).ok;
  } catch {
    return false;
  }
}

async function restaurarSesion() {
  let d = null;
  try { d = JSON.parse(localStorage.getItem(SESION_KEY) || "null"); } catch { return; }
  await restoreEditorState(d);
}

async function restoreEditorState(d, project = false) {
  if (!d) return 0;

  PKG_CAMPOS.forEach((id) => {
    if ($(id) && d.campos?.[id] != null) $(id).value = d.campos[id];
  });
  // Restore whether the Content ID was actually typed by hand. Inferring it
  // from "the field has a value" was wrong: it always has one, because it is
  // auto-generated on boot, so after the first reload it stayed frozen and
  // stopped following the Title ID.
  pkg.contentIdTouched = Boolean(d.contentIdTouched);

  let vivos = 0, perdidos = 0;
  for (const [slotId, jobId] of Object.entries(d.slots || {})) {
    const url = Projects.mediaUrl(`/api/slot/${jobId}/preview`);
    if (await jobVive(url)) {
      slots.chosen[slotId] = jobId;
      paintSlot(slotId, url);
      vivos++;
    } else perdidos++;
  }

  if (d.bgm) {
    try {
      // paintBgm already knows how to draw name, loudness and volume.
      let audio = await api(`/api/audio/${d.bgm}/info`);
      if (project && Math.abs(audio.gain_db - d.bgmGain) > 0.01) {
        const body = new FormData();
        body.append("gain_db", String(d.bgmGain));
        audio = await api(`/api/audio/${d.bgm}/gain`, { method: "POST", body });
      }
      paintBgm(audio);
      vivos++;
    } catch { perdidos++; }
  }

  for (const [slotId, jobId] of Object.entries(d.sfx || {})) {
    try {
      const a = await api(`/api/audio/${jobId}/info`);
      sfx.chosen[slotId] = jobId;
      const fila = [...document.querySelectorAll("#sfx-rows .sfx-row")]
        .find((r) => r.querySelector(".sfx-name")?.textContent === lbl(a.slot_label));
      if (fila) {
        fila.querySelector(".bgm-info").textContent =
          `${a.name} → ${a.codec}, ${a.duration}s`;
        fila.querySelector(".bgm-info").className = "bgm-info on";
        fila.querySelector(".sfx-clear").hidden = false;
      }
      vivos++;
    } catch { perdidos++; }
  }

  if (d.bgMode === "animated") $("bgmode-animated").checked = true;
  if (project && d.anim) {
    const { size, wait, frames, backend, jobId } = d.anim;
    $("anim-size").value = sizeValue({ size });
    $("anim-frames").max = String(currentSize().max_frames);
    anim.frameCount = frames;
    anim.wait = wait;
    $("anim-wait").value = String(wait);
    $("anim-wait-out").textContent = wait.toFixed(2);
    if (sys.backends.includes(backend)) $("anim-backend").value = backend;
    syncSizeUi();
    updateTiming();
    if (jobId) {
      const body = new FormData();
      body.append("size", sizeValue({ size }));
      body.append("frames", String(frames));
      try {
        const data = await api(`/api/animated/${jobId}/frames`, { method: "POST", body });
        await loadBitmaps(data.frames);
        $("anim-drop").hidden = true;
        $("anim-workspace").hidden = false;
        applyAnimatedPayload(data);
        player.draw();
        vivos++;
      } catch { perdidos++; }
    }
  }
  syncRafOverlap();

  if (vivos) {
    toast(t("misc.session", { n: vivos })
          + (perdidos ? t("misc.sessionExpired", { n: perdidos }) : ""), "ok");
  }
  refreshChecklist();
  renderTree();
  $("pkg-themecolor").dispatchEvent(new Event("change"));
  renderPreview();
  return perdidos;
}

/* ================================================== VISTA PREVIA
 *
 * Mock-up of the screens a theme touches. It does not aim to be pixel exact:
 * it is there to check contrast, whether the background swallows the icons and
 * how the theme.xml colours look together — the things you otherwise had to
 * verify by installing the package on the console.
 *
 * The views map onto the backgrounds, which is why with an animated scene they
 * show the same thing: that is exactly what the console does. */

/** theme.xml's themecolor 0-7, approximated to how the console tints. */
const PV_THEME_COLORS = [
  "#1e9bff", "#ff5fa2", "#e8413c", "#2c4a7c",
  "#6b7280", "#d9a441", "#a8c0d8", "#8b5cf6",
];

const pvView = () =>
  document.querySelector('input[name="pvview"]:checked')?.value || "home";

/** URL of a slot's image, following the fallback chain.
 *
 *  If you did not upload the top-menu background, the console uses the home
 *  one; the mock-up has to do the same or it would show a black gap that does
 *  not exist on the console. */
function slotImage(slotId) {
  const visto = new Set();
  let id = slotId;
  while (id && !visto.has(id)) {
    visto.add(id);
    if (slots.chosen[id]) return Projects.mediaUrl(`/api/slot/${slots.chosen[id]}/preview`);
    id = slots.catalog.find((x) => x.id === id)?.fallback;
  }
  return null;
}

/** Animated background player inside the mock-up.
 *
 *  A separate loop from the Background tab's on purpose: both read
 *  anim.bitmaps but never run at once, because only one tab is visible.
 *  Sharing the player would mean teaching it to draw in two places. */
const pvPlayer = {
  rafId: 0, index: 0, acc: 0, lastTs: 0, on: false,

  start() {
    if (this.on || !anim.bitmaps.length) return;
    this.on = true; this.lastTs = 0;
    this.rafId = requestAnimationFrame(pvPlayer.tick);
  },
  stop() {
    this.on = false;
    cancelAnimationFrame(this.rafId);
  },
  tick(ts) {
    if (!pvPlayer.on) return;
    if (!pvPlayer.lastTs) pvPlayer.lastTs = ts;
    const dt = Math.min(ts - pvPlayer.lastTs, 250);
    pvPlayer.lastTs = ts;
    const per = anim.wait * 1000;
    pvPlayer.acc += dt;
    while (pvPlayer.acc >= per) {
      pvPlayer.acc -= per;
      pvPlayer.index = (pvPlayer.index + 1) % anim.bitmaps.length;
    }
    const bmp = anim.bitmaps[pvPlayer.index];
    const cv = $("pv-canvas");
    if (bmp && cv) {
      if (anim.size && cv.width !== anim.size[0]) [cv.width, cv.height] = anim.size;
      cv.getContext("2d").drawImage(bmp, 0, 0, cv.width, cv.height);
    }
    pvPlayer.rafId = requestAnimationFrame(pvPlayer.tick);
  },
};

function renderPreview() {
  const stage = $("pv-stage");
  if (!stage || !slots.catalog.length) return;
  const vista = pvView();
  const animado = animActive();

  // Colours come from the Package tab: the ones going into theme.xml.
  const val = (id, fallback) => ($(id)?.value || fallback);
  stage.style.setProperty("--pv-font", val("pkg-fontcolor", "#ffffff"));
  stage.style.setProperty("--pv-shadow", val("pkg-fontshadow", "#000000"));
  stage.style.setProperty("--pv-focus", val("pkg-focuscolor", "#1e9bff"));
  stage.style.setProperty(
    "--pv-theme", PV_THEME_COLORS[Number(val("pkg-themecolor", "0"))] || PV_THEME_COLORS[0]);

  // --- fondo ---
  const img = $("pv-bg"), cv = $("pv-canvas");
  // With an animated scene the view does not matter: the console draws it on
  // both.
  const url = animado ? null : slotImage(vista === "function" ? "function" : "home");
  // On "apply theme" the background behind is the Settings one, not the
  // theme's: the theme is not applied yet. Dimmed so it reads that way.
  $("pv-stage").classList.toggle("pv-en-ajustes", vista === "apply");
  if (animado) {
    img.hidden = true; cv.hidden = false;
    pvPlayer.start();
  } else {
    pvPlayer.stop();
    cv.hidden = true;
    img.hidden = !url;
    if (url && img.getAttribute("src") !== url) img.src = url;
  }
  const hayFondo = animado || Boolean(url);
  $("pv-empty").hidden = hayFondo;
  $("pv-home").hidden = !hayFondo || vista !== "home";
  $("pv-func").hidden = !hayFondo || vista !== "function";
  $("pv-apply").hidden = !hayFondo || vista !== "apply";

  // --- apply theme screen ---
  // The only view where texture/preview.png shows up; it appears in neither of
  // the other two.
  if (vista === "apply") {
    const mini = slotImage("preview");
    const shot = $("pv-ap-shot");
    if (mini && shot.getAttribute("src") !== mini) shot.src = mini;
    shot.hidden = !mini;
    $("pv-ap-title").textContent = $("pkg-title")?.value || t("misc.myTheme");
    // The rows are filler: what matters is how the colour reads over them.
    $("pv-ap-rows").innerHTML = t("pv.settingsRows")
      .map((fila) => `<div class="pv-ap-row"><i></i>${fila}</div>`).join("");
  }

  // --- function icons ---
  const fns = slots.catalog.filter((x) => x.group === "function_icon");
  const glifo = (spec) => {
    const u = slots.chosen[spec.id] ? Projects.mediaUrl(`/api/slot/${slots.chosen[spec.id]}/preview`) : null;
    return { url: u, propio: Boolean(u) };
  };

  $("pv-fnrow").innerHTML = fns.map((spec) => {
    const g = glifo(spec);
    return `<span class="pv-fn${g.propio ? "" : " is-default"}" title="${lbl(spec.label)}">${
      g.url ? `<img src="${g.url}" alt="">` : ""}</span>`;
  }).join("");

  $("pv-funcrow").innerHTML = fns.map((spec, i) => {
    const g = glifo(spec);
    return `<span class="pv-fnbig${g.propio ? "" : " is-default"}${i === 0 ? " is-sel" : ""}">
      <span class="pv-glyph">${g.url ? `<img src="${g.url}" alt="">` : ""}</span>
      <span>${lbl(spec.label)}</span>
    </span>`;
  }).join("");

  // --- content row: the content icons stand in as tiles ---
  // The selected one is the SECOND in the row, not the first: on the console
  // normal a su izquierda (medido: normal al 5.1% del ancho, elegido al 17.1%).
  const SEL = 1;
  const cts = slots.catalog.filter((x) => x.group === "content_icon").slice(0, 6);
  $("pv-tiles").innerHTML = cts.map((spec, i) => {
    const g = glifo(spec);
    return `<span class="pv-tile${i === SEL ? " is-sel" : ""}">${
      g.url ? `<img src="${g.url}" alt="">` : ""}</span>`;
  }).join("");
  $("pv-tilename").textContent = lbl(cts[SEL]?.label) || "—";
  $("pv-tiledesc").textContent = t("pv.tiledesc");
  $("pv-user").textContent = ($("pkg-title")?.value || t("misc.myTheme")).slice(0, 22);

  // --- pie ---
  const propios = fns.filter((x) => slots.chosen[x.id]).length;
  if (!hayFondo) {
    $("pv-note").textContent = t("pv.note.empty");
  } else if (vista === "apply") {
    $("pv-note").textContent = t("pv.note.apply");
  } else {
    $("pv-note").textContent =
      (animado ? t("pv.note.raf") : "")
      + t("pv.note.icons", { own: propios, total: fns.length });
  }
}

/* ================================================== EMPAQUETAR */

const pkg = { defaults: null, contentIdTouched: false };

/** Fills the system colour dropdown, keeping the current selection.
 *
 *  Split out so it can be redrawn on a language change: the colour names come
 *  from the backend as {en, es}. */
function pintarColoresDelSistema() {
  const sel = $("pkg-themecolor");
  const actual = sel.value || String(pkg.defaults.theme_defaults.themecolor);
  sel.innerHTML = pkg.defaults.theme_colors
    .map((c) => `<option value="${c.value}">${c.value} — ${lbl(c.label)}</option>`)
    .join("");
  sel.value = actual;
}

/** <input type=color> gives #RRGGBB and theme.xml wants opaque #AARRGGBB. */
const hexToArgb = (hex) => ("#FF" + hex.replace("#", "")).toUpperCase();
const argbToHex = (argb) => "#" + argb.replace("#", "").slice(2).toLowerCase();

/** Flags which assets are ready. Recomputed on entering the tab because the
 *  other tabs are edited independently. */
function refreshChecklist() {
  const count = (g) => slots.catalog.filter((x) => x.group === g && slots.chosen[x.id]).length;
  const fondos = count("background"), fn = count("function_icon"), ct = count("content_icon");
  const home = Boolean(slots.chosen.home);
  const nSfx = Object.keys(sfx.chosen).length;
  const rows = [
    // Named differently per mode, like its slot in the Background tab.
    [bgMode() === "animated" ? "chk.themeImg" : "chk.homeBg",
     home, home ? `${t("chk.required")} \u2713` : t("chk.needed"), true],
    ["chk.otherBg", fondos > 1,
     fondos > 1 ? t("chk.own", { n: fondos - 1 }) : t("chk.copyHome"), false],
    ["chk.fnIcons", fn, fn ? t("chk.nOf", { n: fn, total: 9 }) : t("chk.default"), false],
    ["chk.ctIcons", ct, ct ? t("chk.nOf", { n: ct, total: 10 }) : t("chk.default"), false],
    ["chk.sfx", nSfx, nSfx ? t("chk.nOf", { n: nSfx, total: 3 }) : t("chk.systemOnes"), false],
    ["chk.music", bgm.jobId, bgm.jobId ? "bgm_home.at9" : t("chk.systemMusic"), false],
    ["chk.animated", animActive(),
     animActive() ? `${anim.frameCount} frames \u00b7 ${anim.wait.toFixed(2)}s`
                  : (anim.jobId ? t("chk.loadedNotUsed") : t("chk.optional")), false],
  ];
  $("pkg-checklist").innerHTML = rows
    .map(([clave, ready, detail, required]) => `
      <li class="${ready ? "on" : "off"}">
        <span class="mark">${ready ? "\u2713" : ""}</span>
        <span>${clave.startsWith("chk.") ? t(clave) : clave}${
          required && !ready ? ` <b>\u2014 ${t("chk.required")}</b>` : ""}</span>
        <span class="detail">${detail}</span>
      </li>`)
    .join("");

  $("pkg-build").disabled = !slots.chosen.home || !sys.pkg_tools;
  $("pkg-hint").textContent = t(
    !sys.pkg_tools ? "pkg.hint.noTools"
      : slots.chosen.home ? "pkg.hint.ready" : "pkg.hint.needHome");
}

/** Content tree, built from the slot catalogue.
 *  Fixed paths: these are the names the console looks for. */
function renderTree() {
  const rows = [
    ["theme.xml", "theme.xml", t("tree.themeXml")],
    // With an animated scene NEITHER texture/background/ path is written, not
    // even the home one: its image enters the package through the thumbnail
    // and the icon, which are separate slots. The tree has to show the package
    // that will be built, not the slot catalogue.
    ...slots.catalog
      .filter((s) => s.group === "background")
      .filter((s) => !(bgMode() === "animated" && s.hidden_by_raf))
      .map((s) => [lbl(s.label), s.path, s.size.join("×")]),
    [t("tree.fnIcons"), "texture/function_icon/", "128\u00d7128 + glow"],
    [t("tree.ctIcons"), "texture/content_icon/", "512\u00d7512"],
  ];
  if (animActive()) {
    rows.push([t("tree.animated"), "scene/background.raf/", t("tree.animated.note")]);
  }

  $("pkg-tree").innerHTML =
    `<div class="tree-row tree-fixed"><span class="tree-k">Image0/</span></div>` +
    rows.map(([k, path, note], i) => `
      <div class="tree-row tree-fixed">
        <span class="tree-k">${i === rows.length - 1 ? "└─" : "├─"} ${k}</span>
        <code>${path}</code>
        <span class="dim" style="font-size:11px">${note}</span>
      </div>`).join("");
}

async function syncContentId() {
  if (pkg.contentIdTouched) return;
  const titleId = $("pkg-title-id").value.trim().toUpperCase();
  const title = $("pkg-title").value;
  if (!/^[A-Z]{4}[0-9]{5}$/.test(titleId)) return;
  try {
    const q = new URLSearchParams({ title_id: titleId, title });
    const d = await api(`/api/package/content-id?${q}`);
    if (pkg.contentIdTouched || $("pkg-title").value !== title ||
        $("pkg-title-id").value.trim().toUpperCase() !== titleId) return;
    $("pkg-content-id").value = d.content_id;
    guardarSesion();
  } catch { /* mientras se escribe puede quedar a medias, no molestar */ }
}

async function packageBuild() {
  const btn = $("pkg-build");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Empaquetando…";
  $("pkg-result").hidden = true;

  const fd = new FormData();
  fd.append("title", $("pkg-title").value.trim());
  fd.append("title_id", $("pkg-title-id").value.trim().toUpperCase());
  fd.append("content_id", $("pkg-content-id").value.trim().toUpperCase());
  fd.append("category", pkg.defaults.defaults.category);
  // Fixed at 32 zeros: it encrypts the package and does not travel inside it,
  // so the installer has to take it for granted. See the Package tab note.
  fd.append("passcode", pkg.defaults.defaults.passcode);
  fd.append("mode", pkg.defaults.defaults.mode);
  fd.append("slots", JSON.stringify(slots.chosen));
  // Only goes into the package when the chosen mode is animated.
  fd.append("animated_job", animActive() ? anim.jobId : "");
  fd.append("audio_job", bgm.jobId || "");
  fd.append("sfx", JSON.stringify(sfx.chosen));
  fd.append("frames", String(anim.frameCount));
  fd.append("wait_seconds", String(anim.wait));
  fd.append("backend", $("anim-backend").value || sys.default_backend);
  fd.append("themecolor", $("pkg-themecolor").value);
  fd.append("fontcolor", hexToArgb($("pkg-fontcolor").value));
  fd.append("fontshadowcolor", hexToArgb($("pkg-fontshadow").value));
  fd.append("focuscolor", hexToArgb($("pkg-focuscolor").value));

  try {
    const r = await api("/api/package/build", { method: "POST", body: fd });
    const rows = r.included
      .map((i) => `<div class="result-row"><span>${i.asset}</span><b>${fmtBytes(i.bytes)}</b></div>`)
      .join("");
    // The subcommand says more than the exe name, which repeats every time.
    const steps = r.log
      .map((s) => {
        const parts = s.cmd.split(" ");
        return `<div class="log-step"><b>${s.label}</b><span>${parts[1] || parts[0]}</span><span class="secs">${s.seconds}s</span></div>`;
      })
      .join("");
    $("pkg-result").hidden = false;
    $("pkg-result").innerHTML = `
      <h2 class="card-title">${t("pkg.result.ok", { size: fmtBytes(r.pkg_bytes), s: r.elapsed })}</h2>
      <div class="result-rows">${rows}</div>
      <div class="log">${steps}</div>
      <p class="card-note"><code>${r.pkg_name}</code></p>
      <a class="dl-btn" style="margin-top:14px" href="${Projects.mediaUrl(r.download)}" download>${t("pkg.download")}</a>`;
    toast(t("pkg.result.ok", { size: fmtBytes(r.pkg_bytes), s: r.elapsed }), "ok");
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
    refreshChecklist();
  }
}

async function bootPackage() {
  let d;
  try {
    d = await api("/api/package/defaults");
  } catch (err) {
    // Staying quiet here leaves the package button with no listener and no
    // hint as to why it does nothing.
    toast(`No se pudo preparar el empaquetado: ${err.message}`);
    return;
  }
  pkg.defaults = d;
  $("pkg-missing").hidden = d.available;

  $("pkg-title").value = d.defaults.title;
  $("pkg-title-id").value = d.defaults.title_id;
  $("pkg-content-id").value = d.defaults.content_id;


  const modo = d.modes.find((m) => m.id === d.defaults.mode);
  $("pkg-fixed-fmt").textContent =
    `${modo.volume_type} · ${d.defaults.category} · IRO_TAG ${modo.iro_tag}`;

  pintarColoresDelSistema();
  $("pkg-fontcolor").value = argbToHex(d.theme_defaults.fontcolor);
  $("pkg-fontshadow").value = argbToHex(d.theme_defaults.fontshadowcolor);
  $("pkg-focuscolor").value = argbToHex(d.theme_defaults.focuscolor);

  renderTree();

  // Colours and title feed the mock-up: make it react as you touch them.
  // The swatch keeps you from picking the system colour blind: the dropdown
  // only gave a number and a name.
  const pintarMuestra = () => {
    $("pkg-themecolor-swatch").style.background =
      PV_THEME_COLORS[Number($("pkg-themecolor").value)] || PV_THEME_COLORS[0];
  };
  $("pkg-themecolor").addEventListener("change", pintarMuestra);
  pintarMuestra();

  ["pkg-fontcolor", "pkg-fontshadow", "pkg-focuscolor", "pkg-themecolor", "pkg-title"]
    .forEach((id) => $(id).addEventListener("input", renderPreview));
  PKG_CAMPOS.forEach((id) => $(id)?.addEventListener("input", guardarSesion));

  $("pkg-title").addEventListener("input", syncContentId);
  $("pkg-title-id").addEventListener("input", syncContentId);
  $("pkg-content-id").addEventListener("input", () => { pkg.contentIdTouched = true; });
  $("pkg-build").addEventListener("click", packageBuild);
}


/* ------------------------------------------------------------------ idioma */

/** Redraws everything that is generated from JS.
 *
 *  applyI18n() only touches nodes marked with data-i18n; the slot grids, the
 *  checklist, the tree and the mock-up are built by hand, so they have to be
 *  rebuilt. Uploads are kept: only the text changes. */
function relocalizarTodo() {
  if (!sys) return;
  const sel = $("anim-size");
  const resActual = sel.value;
  sel.innerHTML = sys.animated_sizes
    .map((x) => `<option value="${sizeValue(x)}">${
      t("bg.size.opt", { label: x.label, max: x.max_frames })}</option>`)
    .join("");
  sel.value = resActual;

  pintarSlots();
  pintarColoresDelSistema();
  describeBgmSupport();
  pintarSfxRows();
  syncSizeUi();
  syncRafOverlap();
  refreshChecklist();
  renderTree();
  renderPreview();
  if (bgm.jobId) pintarGanancia(bgm.loudness);
}

document.addEventListener("langchange", relocalizarTodo);

/* ================================================== arranque */

async function boot() {
  if (Projects.id) document.querySelector("main").inert = true;
  applyI18n();
  try {
    sys = await api("/api/system");
  } catch {
    toast("No se pudo contactar al backend.");
    return;
  }

  anim.wait = sys.default_wait_seconds;

  $("sysinfo").innerHTML = [
    `<span class="pill ${sys.ffmpeg ? "ok" : "off"}">ffmpeg</span>`,
    `<span class="pill ${sys.magick ? "ok" : "off"}">ImageMagick${sys.magick ? "" : " (no instalado)"}</span>`,
    `<span class="pill ok">encoder ${sys.default_backend}</span>`,
    `<span class="pill ${sys.pkg_tools ? "ok" : "off"}">pkg tools</span>`,
  ].join("");

  // Encoder selector only when there is more than one option.
  const sel = $("anim-backend");
  sel.innerHTML = sys.backends
    .map((b) => `<option value="${b}"${b === sys.default_backend ? " selected" : ""}>${b}</option>`)
    .join("");
  $("backend-row").hidden = sys.backends.length < 2;

  // syncSizeUi() handles the max, the value and the counter.
  const sizeSel = $("anim-size");
  sizeSel.innerHTML = sys.animated_sizes
    .map((x) => `<option value="${sizeValue(x)}">${
      t("bg.size.opt", { label: x.label, max: x.max_frames })}</option>`)
    .join("");
  sizeSel.addEventListener("change", () => {
    syncSizeUi();
    if (anim.jobId) animatedReframe();
    else guardarSesion();
  });
  sel.addEventListener("change", guardarSesion);
  syncSizeUi();

  const wt = $("anim-wait");
  wt.min = String(sys.min_wait_seconds);
  wt.max = String(sys.max_wait_seconds);
  wt.step = "0.01";
  wt.value = String(anim.wait);
  $("anim-wait-out").textContent = anim.wait.toFixed(2);

  // Picking "Still image" / "Animated background" only changes what is shown
  // and packaged; the other mode's work is kept in case you come back.
  document.querySelectorAll('input[name="bgmode"]').forEach((r) =>
    r.addEventListener("change", () => {
      if (bgMode() !== "animated") player.pause();
      guardarSesion();
      syncRafOverlap();
      refreshChecklist();
      renderTree();
      renderPreview();
    })
  );

  const langSel = $("lang-select");
  langSel.value = lang();
  langSel.addEventListener("change", () => setLang(langSel.value));

  document.querySelectorAll('input[name="pvview"]').forEach((r) =>
    r.addEventListener("change", renderPreview));

  updateBudget();
  updateTiming();
  await bootSlots();
  describeBgmSupport();
  await bootSfx();
  await bootPackage();
  refreshChecklist();

  // Last: it needs the grids and the fields already in place to redraw them
  // with whatever survives from the previous session.
  if (!Projects.id) await restaurarSesion();
  await Projects.boot();
  Library.boot();
}

/* ------------------------------------------------------------ listeners */

wireDropzone("anim-drop", "anim-file", animatedUpload);

// While dragging only the numbers update; the frames are requested on release,
// so a slider pixel does not trigger an extraction.
$("anim-frames").addEventListener("input", (e) => {
  anim.frameCount = Number(e.target.value);
  pintarFrames();
  updateBudget();
  updateTiming();
});
$("anim-frames").addEventListener("change", () => {
  if (anim.jobId) animatedReframe();
  else guardarSesion();
});

// Volume means re-encoding the whole .at9, so it applies on release, like the
// frames do.
$("bgm-gain").addEventListener("input", (e) => {
  bgm.gain = Number(e.target.value);
  $("bgm-gain-out").textContent = `${bgm.gain >= 0 ? "+" : ""}${bgm.gain.toFixed(1)} dB`;
});
$("bgm-gain").addEventListener("change", aplicarGanancia);

// wait does not change the frames, only the timing and the xml: applied live.
$("anim-wait").addEventListener("input", (e) => {
  anim.wait = Number(e.target.value);
  $("anim-wait-out").textContent = anim.wait.toFixed(2);
  updateTiming();
  guardarSesion();
});

$("anim-play").addEventListener("click", () => player.toggle());
$("anim-scrub").addEventListener("input", (e) => {
  player.pause();
  player.seek(Number(e.target.value));
});
$("anim-export").addEventListener("click", animatedExport);
$("anim-reset").addEventListener("click", resetAnimated);


// Space = play/pause, arrows = frame by frame. Only with the player visible:
// in still-image mode those keys are not ours.
document.addEventListener("keydown", (e) => {
  if (!$("panel-backgrounds").classList.contains("is-active")) return;
  if (bgMode() !== "animated") return;
  if (e.target.matches("input, select, textarea")) return;
  if (e.code === "Space") { e.preventDefault(); player.toggle(); }
  if (e.code === "ArrowRight" && anim.bitmaps.length) {
    player.pause();
    player.seek((player.index + 1) % anim.bitmaps.length);
  }
  if (e.code === "ArrowLeft" && anim.bitmaps.length) {
    player.pause();
    player.seek((player.index - 1 + anim.bitmaps.length) % anim.bitmaps.length);
  }
});

boot();
