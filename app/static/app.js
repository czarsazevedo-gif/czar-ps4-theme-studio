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
    if (tab.dataset.tab !== "preview") {
      pvPlayer.stop();
      Compositor.leavePreview();
    }
    // These three depend on what was done in the other tabs.
    if (tab.dataset.tab === "package") { refreshChecklist(); renderTree(); forgeLoadHistory(); }
    if (tab.dataset.tab === "preview") renderPreview();
    if (tab.dataset.tab === "compositor") Compositor.activate();
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

/* ---- Diagnóstico inteligente pós-importação (Pilar II, não-bloqueante e cancelável) ---- */

const animDiag = {
  taskId: null,
  jobId: null,
  pollTimer: 0,
  running: false,
  runSeq: 0,
};

function syncAnimDiagLink(jobId = anim.jobId) {
  const link = $("anim-diag-ouroboros");
  if (!link) return;
  const u = new URL("/static/ouroboros.html", location.origin);
  if (Projects.id) u.searchParams.set("project", Projects.id);
  if (jobId) u.searchParams.set("video", `animated/${jobId}`);
  link.href = u.pathname + u.search;
}

function animDiagBadgeClass(aptidao) {
  if (aptidao === "FAVORÁVEL A LOOP NATURAL") return "is-good";
  if (aptidao === "ADEQUADO À SÍNTESE" || aptidao === "CORRIGÍVEL") return "is-synth";
  if (aptidao === "DIFÍCIL / RESULTADO CONDICIONAL") return "is-warn";
  return "is-bad";
}

function renderAnimDiagnostic(diag) {
  const body = $("anim-diag-body");
  if (!body || !diag || !diag.fields) return;
  const f = diag.fields;
  body.replaceChildren();

  const banner = document.createElement("div");
  banner.className = "anim-diag-banner";
  const badge = document.createElement("span");
  badge.className = `anim-diag-badge ${animDiagBadgeClass(diag.aptidao)}`;
  badge.textContent = diag.aptidao;
  const summary = document.createElement("p");
  summary.className = "anim-diag-summary";
  summary.textContent = diag.resumo || "";
  banner.append(badge, summary);
  body.append(banner);

  const kvPairs = [
    ["MATERIAL", f.material],
    ["DURAÇÃO", f.duracao],
    ["RESOLUÇÃO", f.resolucao],
    ["FPS", f.fps],
    ["QUALIDADE", f.qualidade],
    ["MOVIMENTO", f.movimento],
    ["ESTABILIDADE", f.estabilidade],
    ["PERIODICIDADE", f.periodicidade],
    ["APTIDÃO", f.aptidao],
    ["MELHOR TRECHO", f.melhor_trecho],
    ["MELHOR RESOLUÇÃO", f.melhor_resolucao],
    ["MELHOR CADÊNCIA", f.melhor_cadencia],
    ["MELHOR ESTRATÉGIA", f.melhor_estrategia],
    ["QUALIDADE ESPERADA", f.qualidade_esperada],
    ["CONFIANÇA", f.confianca],
    ["PRÓXIMO PASSO", f.proximo_passo],
  ];

  const dl = document.createElement("dl");
  dl.className = "anim-diag-kv";
  for (const [label, val] of kvPairs) {
    const item = document.createElement("div");
    item.className = "anim-diag-item";
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = val || "—";
    item.append(dt, dd);
    dl.append(item);
  }
  body.append(dl);

  const listSpecs = [
    ["PONTOS FORTES", f.pontos_fortes],
    ["PONTOS FRACOS", f.pontos_fracos],
    ["O QUE FALTA", f.o_que_falta],
    ["O QUE PODE SER CORRIGIDO", f.o_que_pode_ser_corrigido],
    ["O QUE NÃO PODE SER GARANTIDO", f.o_que_nao_pode_ser_garantido],
  ];
  const lists = document.createElement("div");
  lists.className = "anim-diag-lists";
  for (const [title, items] of listSpecs) {
    const card = document.createElement("article");
    card.className = "anim-diag-list-card";
    const h4 = document.createElement("h4");
    h4.textContent = title;
    const ul = document.createElement("ul");
    const arr = Array.isArray(items) && items.length ? items : ["Nenhum ponto registrado."];
    for (const entry of arr) {
      const li = document.createElement("li");
      li.textContent = entry;
      ul.append(li);
    }
    card.append(h4, ul);
    lists.append(card);
  }
  body.append(lists);
  body.hidden = false;
}

function setAnimDiagRunning(running) {
  animDiag.running = running;
  const runBtn = $("anim-diag-run");
  const cancelBtn = $("anim-diag-cancel");
  const prog = $("anim-diag-progress");
  if (runBtn) runBtn.disabled = running || !anim.jobId || !Projects.id;
  if (cancelBtn) cancelBtn.hidden = !running;
  if (prog) prog.hidden = !running;
}

async function cancelAnimDiagnostic({ quiet = false } = {}) {
  clearTimeout(animDiag.pollTimer);
  animDiag.pollTimer = 0;
  animDiag.runSeq++;
  const activeTask = animDiag.taskId;
  animDiag.taskId = null;
  setAnimDiagRunning(false);
  if (activeTask && Projects.id) {
    api(`/api/ouroboros/jobs/${encodeURIComponent(activeTask)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).catch(() => { /* best effort cancel */ });
  }
  if (!quiet && $("anim-diag-status")) {
    $("anim-diag-status").textContent =
      "Diagnóstico cancelado. O vídeo importado permanece disponível para edição e exportação.";
  }
}

function resetAnimDiagnostic() {
  cancelAnimDiagnostic({ quiet: true });
  animDiag.jobId = null;
  const sec = $("anim-diagnostic");
  const body = $("anim-diag-body");
  if (sec) sec.hidden = true;
  if (body) { body.replaceChildren(); body.hidden = true; }
  if ($("anim-diag-status")) $("anim-diag-status").textContent = "Aguardando análise…";
  if ($("anim-diag-progress")) $("anim-diag-progress").value = 0;
  syncAnimDiagLink(null);
}

function prepareAnimDiagnosticPanel(jobId, { autoRun = false } = {}) {
  const sec = $("anim-diagnostic");
  if (!sec || !jobId) return;
  animDiag.jobId = jobId;
  sec.hidden = false;
  syncAnimDiagLink(jobId);
  if (!Projects.id) {
    setAnimDiagRunning(false);
    if ($("anim-diag-body")) { $("anim-diag-body").replaceChildren(); $("anim-diag-body").hidden = true; }
    if ($("anim-diag-status")) {
      $("anim-diag-status").textContent =
        "Sessão avulsa: abra ou crie um projeto para executar o diagnóstico automático inteligente.";
    }
    return;
  }
  if (autoRun) {
    startAnimDiagnostic(jobId);
  } else {
    setAnimDiagRunning(false);
    if ($("anim-diag-status")) {
      $("anim-diag-status").textContent =
        "Vídeo carregado no projeto. Toque em 'Reanalisar vídeo' para consultar ou gerar o diagnóstico completo.";
    }
  }
}

async function startAnimDiagnostic(jobId = anim.jobId) {
  if (!jobId || !Projects.id) {
    prepareAnimDiagnosticPanel(jobId, { autoRun: false });
    return;
  }
  await cancelAnimDiagnostic({ quiet: true });
  const seq = ++animDiag.runSeq;
  animDiag.jobId = jobId;
  const sec = $("anim-diagnostic");
  const statusEl = $("anim-diag-status");
  const progEl = $("anim-diag-progress");
  const bodyEl = $("anim-diag-body");
  if (sec) sec.hidden = false;
  syncAnimDiagLink(jobId);
  if (bodyEl) bodyEl.hidden = true;
  if (progEl) progEl.value = 5;
  if (statusEl) statusEl.textContent = "Iniciando diagnóstico automático em segundo plano…";
  setAnimDiagRunning(true);

  try {
    const ticket = await api("/api/ouroboros/deluxe/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "animated", id: jobId }),
    });
    if (seq !== animDiag.runSeq) return;
    animDiag.taskId = ticket.id;

    const poll = async () => {
      if (seq !== animDiag.runSeq || !animDiag.taskId) return;
      try {
        const st = await api(`/api/ouroboros/jobs/${encodeURIComponent(animDiag.taskId)}`);
        if (seq !== animDiag.runSeq) return;
        if (st.state === "working") {
          if (statusEl) statusEl.textContent = st.stage || "Analisando movimento, estabilidade e periodicidade…";
          if (progEl) progEl.value = Number(st.percent || 10);
          animDiag.pollTimer = setTimeout(poll, 450);
          return;
        }
        animDiag.taskId = null;
        setAnimDiagRunning(false);
        if (st.state === "cancelled") {
          if (statusEl) {
            statusEl.textContent =
              "Diagnóstico cancelado. O vídeo importado permanece disponível para edição e exportação.";
          }
          return;
        }
        if (st.state !== "ready" || !st.key) {
          if (statusEl) statusEl.textContent = `Diagnóstico indisponível: ${st.stage || "falha na análise"}.`;
          return;
        }
        const report = await api(`/api/ouroboros/result/${encodeURIComponent(st.key)}`);
        if (seq !== animDiag.runSeq) return;
        if (report?.mediaDiagnostic) {
          renderAnimDiagnostic(report.mediaDiagnostic);
          if (statusEl) {
            statusEl.textContent =
              `Diagnóstico concluído (${report.mediaDiagnostic.aptidao}) · cache compartilhado com o Ouroboros Deluxe.`;
          }
        } else if (statusEl) {
          statusEl.textContent = "Diagnóstico concluído sem relatório detalhado.";
        }
      } catch (err) {
        if (seq !== animDiag.runSeq) return;
        animDiag.taskId = null;
        setAnimDiagRunning(false);
        if (statusEl) {
          statusEl.textContent = `Não foi possível concluir o diagnóstico (${err.message}). O vídeo continua pronto para uso.`;
        }
      }
    };

    poll();
  } catch (err) {
    if (seq !== animDiag.runSeq) return;
    animDiag.taskId = null;
    setAnimDiagRunning(false);
    if (statusEl) {
      statusEl.textContent = `Diagnóstico em espera (${err.message}). O vídeo continua pronto para edição e exportação.`;
    }
  }
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
    prepareAnimDiagnosticPanel(data.job_id, { autoRun: true });
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
  appendComposition(fd);

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
  resetAnimDiagnostic();
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
 * With psgfx-at9 or at9tool present anything is converted; without it, the
 * file has to arrive as .at9 and is only validated. */
const bgm = { jobId: null, gain: 0, busy: false, loudness: null, meta: null };
const forgeAudio = { stagingId: null, fileName: null, duration: 0, busy: false };

/** Loudness of the bgm_home.at9 from the reference theme that plays on the
 *  console. A yardstick: a normal song lands ~13 dB above it. */
const BGM_LOUD_REFERENCIA = -32.0;

function paintBgm(d) {
  const info = $("bgm-info");
  const playerBox = $("bgm-player-box");
  const audioEl = $("bgm-player");
  const seamEl = $("bgm-seam-player");
  const badgeEl = $("bgm-validation-badge");
  if (d) {
    bgm.jobId = d.job_id;
    bgm.loudness = d.loudness;
    bgm.meta = d;
    if (d.gain_db != null) {
      bgm.gain = d.gain_db;
      $("bgm-gain").value = String(d.gain_db);
    }
    guardarSesion();
    const loud = d.loudness == null ? "" : ` · ${d.loudness} LKFS`;
    info.textContent = `${d.name} — ${d.codec} ${d.rate} Hz, ${d.channels}ch, ${d.duration}s${loud}`;
    info.className = "bgm-info on";
    $("bgm-clear").hidden = false;
    $("bgm-gain-row").hidden = !sys.at9tool;
    pintarGanancia(d.loudness);
    if (playerBox && audioEl) {
      const prevUrl = d.preview || `/api/audio/${d.job_id}/preview`;
      audioEl.src = Projects.mediaUrl(prevUrl);
      if (seamEl) {
        const seamUrl = d.seam_preview || `/api/audio/${d.job_id}/seam-preview`;
        seamEl.src = Projects.mediaUrl(seamUrl);
      }
      if (badgeEl) {
        const v = d.validation;
        const loopTxt = v?.has_smpl ? `loop smpl (playCount=${v.play_count})` : "sem smpl";
        const cfgTxt = v?.config_data_hex ? `configData ${v.config_data_hex}` : "";
        const encTxt = d.encoder_backend ? ` · ${d.encoder_backend}` : "";
        badgeEl.textContent = v?.valid
          ? `ATRAC9 validado (${v.validator}): 48 kHz · 144 kbps · ${cfgTxt} · ${loopTxt}${encTxt}`
          : `Áudio aplicado: ${d.codec} ${d.rate} Hz · ${fmtBytes(d.bytes)}${encTxt}`;
      }
      playerBox.hidden = false;
    }
  } else {
    bgm.jobId = null;
    bgm.gain = 0;
    bgm.loudness = null;
    bgm.meta = null;
    $("bgm-gain").value = "0";
    info.textContent = t("snd.none");
    info.className = "bgm-info";
    $("bgm-clear").hidden = true;
    $("bgm-gain-row").hidden = true;
    if (playerBox) playerBox.hidden = true;
    if (audioEl) { audioEl.pause(); audioEl.removeAttribute("src"); audioEl.load(); }
    if (seamEl) { seamEl.pause(); seamEl.removeAttribute("src"); seamEl.load(); }
    if (badgeEl) badgeEl.textContent = "";
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
    refreshChecklist();
    renderTree();
  } catch (err) {
    toast(err.message);
  }
}

function _forgeAudioParams() {
  const fd = new FormData();
  if (forgeAudio.stagingId) fd.append("staging_id", forgeAudio.stagingId);
  else if (bgm.jobId) fd.append("job_id", bgm.jobId);
  fd.append("loop_mode", $("forge-loop-mode")?.value || "whole");
  const startVal = $("forge-loop-start")?.value?.trim();
  const endVal = $("forge-loop-end")?.value?.trim();
  if (startVal) fd.append("start_sec", startVal);
  if (endVal) fd.append("end_sec", endVal);
  fd.append("trim_silence", $("forge-loop-trim")?.checked ? "true" : "false");
  fd.append("crossfade_ms", $("forge-loop-xfade")?.value || "0");
  const allowFade = Boolean($("forge-loop-fade")?.checked);
  fd.append("allow_fade", allowFade ? "true" : "false");
  if (allowFade) {
    fd.append("fade_in_ms", "15");
    fd.append("fade_out_ms", "15");
  }
  fd.append("volume_mode", $("forge-volume-mode")?.value || "original");
  fd.append("gain_db", $("forge-volume-gain")?.value || "0");
  const lufsVal = $("forge-volume-lufs")?.value?.trim();
  if (lufsVal) fd.append("target_lufs", lufsVal);
  return fd;
}

function _setForgeAudioBusy(busy, statusText = "") {
  forgeAudio.busy = busy;
  bgm.busy = busy;
  const ready = Boolean(forgeAudio.stagingId || bgm.jobId);
  if ($("forge-audio-preview-btn")) $("forge-audio-preview-btn").disabled = busy || !ready;
  if ($("forge-bgm-apply")) $("forge-bgm-apply").disabled = busy || !ready;
  if (statusText && $("forge-audio-status")) {
    $("forge-audio-diagnostics").hidden = false;
    $("forge-audio-status").textContent = statusText;
  }
}

async function forgeInspectFile(file) {
  _setForgeAudioBusy(true, "Analisando formato real, loudness EBU R128 e pontos de loop…");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const d = await api("/api/forge/audio/inspect", { method: "POST", body: fd });
    forgeAudio.stagingId = d.staging_id;
    forgeAudio.fileName = d.name;
    forgeAudio.duration = d.duration;
    $("forge-audio-src-info").textContent =
      `${d.name} · ${d.codec} ${d.sample_rate} Hz · ${d.channels}ch · ${d.duration}s · ${fmtBytes(d.bytes)}`;
    if ($("forge-loop-end")) $("forge-loop-end").placeholder = String(d.duration);
    $("forge-player-before").src = Projects.mediaUrl(d.preview_url);
    $("forge-player-after").src = Projects.mediaUrl(d.preview_url);
    $("forge-player-seam").src = Projects.mediaUrl(d.seam_preview_url);
    $("forge-audio-ab-players").hidden = false;
    const la = d.loop_analysis;
    const ld = d.loudness;
    const lines = [
      `Codec origem: ${d.codec} (${d.sample_rate} Hz, ${d.channels}ch, ${d.duration}s)`,
      `Loudness integrado: ${ld.integrated_lufs ?? "silêncio"} LUFS · True Peak: ${ld.true_peak_dbtp ?? "—"} dBTP · Clipping: ${ld.clipped_samples}`,
      `Delta vs ref. PS4 (-32,0 LKFS): ${ld.delta_vs_reference_db ?? "—"} dB`,
      `Costura original: clique=${la.click_risk} · similaridade=${la.spectral_similarity} · fase=${la.phase_correlation} · energia=${la.energy_delta_db} dB`,
      la.convincing ? "Loop avaliado: convincente" : "Loop avaliado: atenção (recomenda-se ajuste)",
      ...(la.warnings || []).map((w) => `Aviso: ${w}`),
    ];
    $("forge-audio-metrics").textContent = lines.join("\n");
    $("forge-audio-status").textContent = "Áudio inspecionado. Ajuste loop/volume ou clique em CONVERTER E APLICAR MÚSICA.";
  } catch (err) {
    toast(err.message);
    $("forge-audio-status").textContent = `Falhou: ${err.message}`;
  } finally {
    _setForgeAudioBusy(false);
  }
}

async function forgePreviewProcess() {
  if (forgeAudio.busy) return;
  _setForgeAudioBusy(true, "Processando prévia de loop e volume (sem alterar o projeto)…");
  try {
    const d = await api("/api/forge/audio/preview-process", { method: "POST", body: _forgeAudioParams() });
    forgeAudio.stagingId = d.staging_id;
    $("forge-player-before").src = Projects.mediaUrl(d.original_preview_url);
    $("forge-player-after").src = Projects.mediaUrl(d.preview_url);
    $("forge-player-seam").src = Projects.mediaUrl(d.seam_preview_url);
    $("forge-audio-ab-players").hidden = false;
    const lr = d.loop_report;
    const bl = d.before_loudness;
    const al = d.after_loudness;
    const lines = [
      `Modo volume: ${d.volume_params.mode} (ganho efetivo ${d.volume_params.applied_gain_db >= 0 ? "+" : ""}${d.volume_params.applied_gain_db} dB${d.volume_params.limiter_applied ? " · limitador ativo" : ""})`,
      `Antes: ${bl.integrated_lufs ?? "—"} LUFS (TP ${bl.true_peak_dbtp ?? "—"} dBTP) → Depois: ${al.integrated_lufs ?? "—"} LUFS (TP ${al.true_peak_dbtp ?? "—"} dBTP)`,
      `Loop (${lr.mode}): ${lr.duration_seconds}s · clique ${lr.click_risk_before} → ${lr.click_risk_after} · similaridade ${lr.spectral_similarity} · crossfade ${lr.crossfade_ms} ms`,
      lr.convincing ? "Resultado do loop: convincente" : "Resultado do loop: atenção — verifique a prévia da costura",
      ...(lr.warnings || []).map((w) => `Aviso: ${w}`),
    ];
    $("forge-audio-metrics").textContent = lines.join("\n");
    $("forge-audio-status").textContent = "Prévia pronta. Ouça Antes, Depois e a Costura do loop antes de aplicar.";
  } catch (err) {
    toast(err.message);
    $("forge-audio-status").textContent = `Falhou: ${err.message}`;
  } finally {
    _setForgeAudioBusy(false);
  }
}

async function forgeConvertAndApplyBgm() {
  if (forgeAudio.busy) return;
  _setForgeAudioBusy(true, "Convertendo para ATRAC9 real (48 kHz · 144 kbps), validando com FFmpeg e aplicando…");
  try {
    const d = await api("/api/forge/audio/convert-apply", { method: "POST", body: _forgeAudioParams() });
    paintBgm(d);
    refreshChecklist();
    renderTree();
    const v = d.validation;
    const pl = d.post_at9_loop;
    const lines = [
      `CONCLUÍDO · sound/bgm_home.at9 (${fmtBytes(d.bytes)}, ${d.duration}s)`,
      `Validação independente (${v.validator}): codec=${v.codec}, ${v.sample_rate} Hz, ${v.channels}ch, ${v.byte_rate} B/s, align=${v.block_align}, configData=${v.config_data_hex}`,
      `Loop smpl: início=${v.loop_start_sample}, fim=${v.loop_end_sample}, playCount=${v.play_count}`,
      `Loudness final (bgmhome_loud): ${d.loudness ?? "—"} LKFS`,
      pl ? `Loop pós-ATRAC9: clique=${pl.click_risk}, similaridade=${pl.spectral_similarity}, fase=${pl.phase_correlation}, convincente=${pl.convincing ? "Sim" : "Atenção"}` : "",
      `SHA-256 derivado: ${d.converted_sha256}`,
    ].filter(Boolean);
    $("forge-audio-metrics").textContent = lines.join("\n");
    $("forge-audio-status").textContent = "Música convertida, validada e aplicada ao projeto.";
    toast("Música ATRAC9 convertida, validada e aplicada!", "ok");
  } catch (err) {
    toast(err.message);
    $("forge-audio-status").textContent = `Falhou: ${err.message}`;
  } finally {
    _setForgeAudioBusy(false);
  }
}

const sfx = { chosen: {}, catalog: [], meta: {} };

async function uploadSfx(slotId, file, row) {
  const fd = new FormData();
  fd.append("file", file);
  const info = row.querySelector(".bgm-info");
  info.textContent = t("snd.converting");
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
  sfx.meta[data.slot] = data;
  guardarSesion();
  if (!row) return;
  const info = row.querySelector(".bgm-info");
  info.textContent = `${data.name} → ${data.codec}, ${data.duration}s, ${fmtBytes(data.bytes)}`;
  info.className = "bgm-info on";
  row.querySelector(".sfx-clear").hidden = false;
  const audio = row.querySelector(".sfx-preview");
  if (audio) {
    audio.src = Projects.mediaUrl(data.preview || `/api/audio/${data.job_id}/preview`);
    audio.hidden = false;
  }
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
      prepareAnimDiagnosticPanel(data.job_id, { autoRun: true });
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
  $("bgm-file").setAttribute("accept", sys.at9tool ? "audio/*,.at9,.wav,.mp3,.flac" : ".at9");
}

async function bootSfx() {
  try {
    sfx.catalog = (await api("/api/audio/slots")).slots;
  } catch {
    sfx.catalog = [];
  }
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
      <span class="bgm-info">${t("snd.none.sfx")}</span>
      <audio class="sfx-preview" controls preload="none" hidden></audio>`;
    const input = row.querySelector("input");
    input.addEventListener("change", () => {
      if (input.files?.[0]) uploadSfx(spec.id, input.files[0], row);
      input.value = "";
    });
    row.querySelector(".sfx-clear").addEventListener("click", () => {
      delete sfx.chosen[spec.id];
      delete sfx.meta[spec.id];
      row.querySelector(".bgm-info").textContent = t("snd.none.sfx");
      row.querySelector(".bgm-info").className = "bgm-info";
      row.querySelector(".sfx-clear").hidden = true;
      const audio = row.querySelector(".sfx-preview");
      if (audio) { audio.pause(); audio.removeAttribute("src"); audio.hidden = true; }
      guardarSesion();
    });
    if (sfx.chosen[spec.id]) {
      const m = sfx.meta[spec.id];
      const info = row.querySelector(".bgm-info");
      if (m) {
        info.textContent = `${m.name} → ${m.codec}, ${m.duration}s${m.bytes ? `, ${fmtBytes(m.bytes)}` : ""}`;
      }
      info.className = "bgm-info on";
      row.querySelector(".sfx-clear").hidden = false;
      const audio = row.querySelector(".sfx-preview");
      if (audio) {
        audio.src = Projects.mediaUrl(m?.preview || `/api/audio/${sfx.chosen[spec.id]}/preview`);
        audio.hidden = false;
      }
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
    paintSlot(slotId, Projects.mediaUrl(`/api/slot/${jobId}/preview`));
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
  const composition = Projects.id ? Compositor.capture() : null;
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
    ...(composition && { composition }),
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
  if (project) Compositor.restore(d.composition);

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
      // paintBgm already knows how to draw name, loudness, preview and volume.
      let audio = await api(`/api/audio/${d.bgm}/info`);
      if (project && sys?.at9tool && Math.abs((audio.gain_db ?? 0) - (d.bgmGain ?? 0)) > 0.01) {
        try {
          const body = new FormData();
          body.append("gain_db", String(d.bgmGain));
          audio = await api(`/api/audio/${d.bgm}/gain`, { method: "POST", body });
        } catch { /* Preserve loaded audio even if re-encode is skipped */ }
      }
      paintBgm(audio);
      if ($("forge-audio-preview-btn")) $("forge-audio-preview-btn").disabled = false;
      if ($("forge-bgm-apply")) $("forge-bgm-apply").disabled = false;
      vivos++;
    } catch { perdidos++; }
  }

  for (const [slotId, jobId] of Object.entries(d.sfx || {})) {
    try {
      const a = await api(`/api/audio/${jobId}/info`);
      sfx.chosen[slotId] = jobId;
      sfx.meta[slotId] = a;
      const fila = document.querySelector(`#sfx-rows .sfx-row[data-slot="${CSS.escape(slotId)}"]`)
        || [...document.querySelectorAll("#sfx-rows .sfx-row")]
          .find((r) => r.querySelector(".sfx-name")?.textContent === lbl(a.slot_label));
      if (fila) {
        fila.querySelector(".bgm-info").textContent =
          `${a.name} → ${a.codec}, ${a.duration}s`;
        fila.querySelector(".bgm-info").className = "bgm-info on";
        fila.querySelector(".sfx-clear").hidden = false;
        const audio = fila.querySelector(".sfx-preview");
        if (audio) {
          audio.src = Projects.mediaUrl(a.preview || `/api/audio/${jobId}/preview`);
          audio.hidden = false;
        }
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
        prepareAnimDiagnosticPanel(data.job_id, { autoRun: false });
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
  const composition = Compositor.previewReady() ? Compositor.previewImage(slotId) : null;
  if (composition) return composition;
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
    if (this.on || document.hidden || !$("panel-preview").classList.contains("is-active") ||
        !(Compositor.previewFrameList() || anim.bitmaps).length) return;
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
    const frames = Compositor.previewFrameList() || anim.bitmaps;
    if (!frames.length) { pvPlayer.stop(); return; }
    pvPlayer.index %= frames.length;
    pvPlayer.acc += dt;
    while (pvPlayer.acc >= per) {
      pvPlayer.acc -= per;
      pvPlayer.index = (pvPlayer.index + 1) % frames.length;
    }
    const bmp = frames[pvPlayer.index];
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
  if ($("panel-preview").classList.contains("is-active") &&
      Compositor.previewNeeded() && !Compositor.previewReady()) {
    stage.hidden = true;
    pvPlayer.stop();
    Compositor.refreshPreview();
    return;
  }
  stage.hidden = false;
  $("comp-preview-status").hidden = true;
  $("comp-preview-retry").hidden = true;
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
    const u = slotImage(spec.id);
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
  const propios = fns.filter((x) => slots.chosen[x.id] || Compositor.has(x.id)).length;
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
  const count = (g) => slots.catalog.filter((x) => x.group === g && (slots.chosen[x.id] || Compositor.has(x.id))).length;
  const fondos = count("background"), fn = count("function_icon"), ct = count("content_icon");
  const home = Boolean(slots.chosen.home || Compositor.has("home"));
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

  $("pkg-build").disabled = !home || !sys.pkg_tools || forgeState.busy;
  $("pkg-assets").hidden = !Projects.id;
  $("pkg-assets").disabled = !home || forgeState.busy;
  $("pkg-hint").textContent = t(
    !sys.pkg_tools ? "pkg.hint.noTools"
      : home ? "pkg.hint.ready" : "pkg.hint.needHome");

  forgeRefreshSummary({ home, fondos, fn, ct, nSfx });
}

/* ================= CZAR FORGE — VALIDAÇÃO, BUNDLE E PKG ================= */

const forgeState = {
  busy: false,
  activeTaskId: null,
  pollTimer: null,
  lastValidation: null,
  lastBundle: null,
  lastPkg: null,
  history: [],
};

function forgeEsc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function forgeSetStatus(state) {
  const badge = $("forge-status-badge");
  if (!badge) return;
  const clsMap = {
    "PRONTO": "is-ready",
    "PROCESSANDO": "is-busy",
    "VALIDANDO": "is-busy",
    "CONCLUÍDO": "is-ok",
    "FALHOU": "is-fail",
    "PENDENTE DE TESTE FÍSICO": "is-pending",
  };
  badge.textContent = state;
  badge.dataset.state = state;
  badge.className = `forge-badge ${clsMap[state] || "is-ready"}`;
}

function forgeSetCard(cardName, text, tone = "") {
  const el = $(`forge-sum-${cardName}`);
  if (!el) return;
  el.textContent = text;
  const box = el.closest(".forge-stat");
  if (box) {
    box.classList.remove("is-ok", "is-warn", "is-fail");
    if (tone) box.classList.add(tone);
  }
}

function forgeRefreshSummary(counts) {
  if (!$("forge-summary-grid")) return;
  const home = counts.home;
  forgeSetCard("media", home ? `${counts.fondos} fundo(s)` : "Falta imagem inicial", home ? "is-ok" : "is-warn");
  forgeSetCard(
    "animated",
    animActive() ? `${anim.frameCount} quadros · ${anim.wait.toFixed(2)}s` : (bgMode() === "animated" ? "Aguardando vídeo" : "Estático"),
    animActive() ? "is-ok" : "",
  );
  forgeSetCard("icons", `${counts.fn}/9 func · ${counts.ct}/10 cont`, (counts.fn || counts.ct) ? "is-ok" : "");
  forgeSetCard("music", bgm.jobId ? "ATRAC9 144 kbps" : "Sem música", bgm.jobId ? "is-ok" : "");
  forgeSetCard("sfx", counts.nSfx ? `${counts.nSfx}/3 VAG` : "Padrão PS4", counts.nSfx ? "is-ok" : "");

  if (forgeState.lastValidation) {
    const v = forgeState.lastValidation;
    const fc = (v.files || []).length;
    forgeSetCard(
      "validation",
      v.valid ? `Aprovado (${fc} arq.)` : `${(v.errors || []).length} erro(s)`,
      v.valid ? "is-ok" : "is-fail",
    );
  } else {
    forgeSetCard("validation", "Não executada", "");
  }

  if (forgeState.lastBundle) {
    forgeSetCard("bundle", `ZIP · ${fmtBytes(forgeState.lastBundle.bytes)}`, "is-ok");
  } else {
    forgeSetCard("bundle", home ? "Pronto para exportar" : "Adicione a imagem inicial", home ? "" : "is-warn");
  }

  if (forgeState.lastPkg) {
    const pr = forgeState.lastPkg.pkg_report;
    const valid = pr?.structure_validated;
    forgeSetCard(
      "pkg",
      valid ? `${fmtBytes(forgeState.lastPkg.pkg_bytes)} · Validado` : "Falhou",
      valid ? "is-ok" : "is-fail",
    );
  } else if (!sys.pkg_tools) {
    forgeSetCard("pkg", "Empacotador ausente", "is-warn");
  } else if (!home) {
    forgeSetCard("pkg", "Adicione a imagem inicial", "is-warn");
  } else if (forgeState.busy) {
    forgeSetCard("pkg", "Empacotando…", "is-warn");
  } else if (forgeState.lastValidation && !forgeState.lastValidation.valid) {
    forgeSetCard("pkg", "Corrija erros de validação", "is-fail");
  } else {
    forgeSetCard("pkg", "Pronto para gerar", "");
  }

  const busy = forgeState.busy;
  if ($("forge-btn-validate")) $("forge-btn-validate").disabled = !home || busy;
  if ($("forge-btn-bundle")) $("forge-btn-bundle").disabled = !home || busy;
  if ($("forge-btn-pkg")) $("forge-btn-pkg").disabled = !home || !sys.pkg_tools || busy;

  const dl = $("forge-btn-download-pkg");
  const rep = $("forge-btn-report");
  const hasPkg = Boolean(forgeState.lastPkg && forgeState.lastPkg.download);
  if (dl) {
    if (hasPkg && !busy) {
      dl.href = Projects.mediaUrl(forgeState.lastPkg.download);
      dl.download = forgeState.lastPkg.pkg_name || "tema-ps4.pkg";
      dl.classList.remove("is-disabled");
      dl.removeAttribute("aria-disabled");
    } else {
      dl.removeAttribute("href");
      dl.classList.add("is-disabled");
      dl.setAttribute("aria-disabled", "true");
    }
  }
  if (rep) rep.disabled = (!hasPkg && !forgeState.lastValidation) || busy;
}

function forgeBuildFormData(useSaved) {
  const fd = new FormData();
  fd.append("use_saved_project", useSaved ? "true" : "false");
  fd.append("title", $("pkg-title").value.trim());
  fd.append("title_id", $("pkg-title-id").value.trim().toUpperCase());
  fd.append("content_id", $("pkg-content-id").value.trim().toUpperCase());
  fd.append("category", pkg.defaults?.defaults?.category || "ac");
  fd.append("passcode", pkg.defaults?.defaults?.passcode || "0".repeat(32));
  fd.append("mode", pkg.defaults?.defaults?.mode || "theme");
  fd.append("slots", JSON.stringify(slots.chosen));
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
  appendComposition(fd);
  return fd;
}

async function forgeEnsureSaved() {
  if (!Projects.id) return true;
  const ok = await Projects.flush();
  if (!ok) {
    toast("Não foi possível salvar o projeto antes da operação. Resolva o conflito e tente novamente.");
  }
  return ok;
}

function forgeRenderValidation(report) {
  const box = $("forge-validation-box");
  if (!box || !report) return;
  const files = report.files || [];
  const totalBytes = files.reduce((acc, f) => acc + (f.bytes || 0), 0);
  const items = (report.checks || []).map((it) => {
    const ok = it.status === "pass" || it.status === "ok";
    const cls = ok ? "is-ok" : it.status === "warn" ? "is-warn" : "is-fail";
    const mark = ok ? "✓" : it.status === "warn" ? "!" : "✕";
    return `<li class="forge-val-item ${cls}">
      <span><b>${mark} ${forgeEsc(it.label || it.category)}</b> — ${forgeEsc(it.detail || "")}</span>
      <code class="dim">${forgeEsc(it.path || "")}</code>
    </li>`;
  }).join("");
  const errs = (report.errors || []).map((e) => `<li>${forgeEsc(e)}</li>`).join("");
  const warns = (report.warnings || []).map((w) => `<li>${forgeEsc(w)}</li>`).join("");
  box.hidden = false;
  box.innerHTML = `
    <h2 class="card-title">${report.valid ? "Validação do tema aprovada" : "Validação encontrou problemas"} · ${files.length} arquivos (${fmtBytes(totalBytes)})</h2>
    ${errs ? `<div class="notice notice-warn"><b>Erros:</b><ul>${errs}</ul></div>` : ""}
    ${warns ? `<p class="card-note"><b>Avisos:</b> <ul>${warns}</ul></p>` : ""}
    <ul class="forge-val-list">${items}</ul>
  `;
}

async function forgeValidateTheme() {
  if (forgeState.busy) return;
  if (!(await forgeEnsureSaved())) return;
  forgeState.busy = true;
  forgeSetStatus("VALIDANDO");
  refreshChecklist();
  try {
    const fd = forgeBuildFormData(Boolean(Projects.id));
    const res = await api("/api/forge/validate", { method: "POST", body: fd });
    const report = res.report || res;
    forgeState.lastValidation = report;
    forgeRenderValidation(report);
    forgeSetStatus(report.valid ? "CONCLUÍDO" : "FALHOU");
    toast(report.valid ? "Tema validado com sucesso." : `Validação falhou (${(report.errors || []).length} erro(s)).`, report.valid ? "ok" : "");
  } catch (err) {
    forgeSetStatus("FALHOU");
    toast(err.message);
  } finally {
    forgeState.busy = false;
    refreshChecklist();
  }
}

async function forgeExportBundle() {
  if (forgeState.busy) return;
  if (!(await forgeEnsureSaved())) return;
  forgeState.busy = true;
  forgeSetStatus("PROCESSANDO");
  refreshChecklist();
  try {
    const fd = forgeBuildFormData(Boolean(Projects.id));
    const r = await api("/api/forge/bundle/export", { method: "POST", body: fd });
    forgeState.lastBundle = r;
    if (r.report) {
      forgeState.lastValidation = r.report;
      forgeRenderValidation(r.report);
    }
    const dlUrl = Projects.mediaUrl(r.download);
    const fname = r.filename || "tema-bundle.zip";
    $("pkg-result").hidden = false;
    $("pkg-result").innerHTML = `
      <h2 class="card-title">Bundle validado pronto · ${fmtBytes(r.bytes)}</h2>
      <p class="card-note">Contém <code>Image0/</code>, <code>Sc0/param.sfo</code> (<code>IRO_TAG=2 · CATEGORY=ac</code>), <code>Sc0/icon0.png</code>, <code>Sc0/pic1.png</code>, <code>theme.gp4</code> e <code>forge-bundle-report.json</code>.</p>
      <a class="dl-btn" style="margin-top:10px" href="${dlUrl}" download="${forgeEsc(fname)}">Baixar recursos validados (ZIP)</a>`;
    forgeSetStatus("CONCLUÍDO");
    toast(`Bundle exportado (${fmtBytes(r.bytes)}).`, "ok");
  } catch (err) {
    forgeSetStatus("FALHOU");
    toast(err.message);
  } finally {
    forgeState.busy = false;
    refreshChecklist();
  }
}

function forgeVerificationLabel(level) {
  const map = {
    full_bundle_verified: "Completa (Cabeçalho + Sc0 + Image0 extraído + SHA-256 ↔ Bundle)",
    extracted_uncompared: "Parcial (Cabeçalho + Sc0 + Image0 extraído · sem bundle de referência)",
    header_and_sc0_only: "Parcial (Somente Cabeçalho + Sc0 · Image0 não extraído)",
    failed: "Reprovada",
  };
  return map[level] || String(level || "Indeterminada");
}

function forgeRenderPkgResult(record) {
  const pr = record.pkg_report || {};
  const valid = Boolean(pr.structure_validated);
  const dlUrl = Projects.mediaUrl(record.download || `/api/forge/pkg/${record.pkg_id}/download`);
  $("pkg-result").hidden = false;
  $("pkg-result").innerHTML = `
    <h2 class="card-title">${valid ? "PKG PS4 construído e validado" : "PKG com falha de validação"} · ${fmtBytes(record.pkg_bytes || pr.pkg_bytes || 0)}</h2>
    <div class="forge-report-kv">
      <div><span>Arquivo</span><code>${forgeEsc(record.pkg_name || "")}</code></div>
      <div><span>Tipo / Volume</span><b>${forgeEsc(pr.volume_type || "pkg_ps4_theme")}</b></div>
      <div><span>Nível de verificação</span><b>${forgeEsc(forgeVerificationLabel(pr.verification_level))}</b></div>
      <div><span>IRO_TAG</span><b>cabeçalho 0x98 = ${forgeEsc(pr.iro_tag_header)} · SFO = ${forgeEsc(pr.iro_tag_sfo)}</b></div>
      <div><span>CATEGORY</span><b>${forgeEsc(pr.category || "ac")}</b></div>
      <div><span>bgmhome_loud</span><b>${pr.bgmhome_loud ? `${forgeEsc(pr.bgmhome_loud)} LKFS` : "Não aplicável (sem música)"}</b></div>
      <div><span>Compatibilidade física</span><b>PENDENTE DE TESTE FÍSICO (PS4 Slim 12.52)</b></div>
    </div>
    <div class="forge-actions" style="margin-top:12px">
      <a class="dl-btn" href="${dlUrl}" download="${forgeEsc(record.pkg_name || "tema-ps4.pkg")}">BAIXAR PKG</a>
      <button type="button" class="btn btn-ghost" onclick="forgeOpenReport('${forgeEsc(record.pkg_id || "")}')">VER RELATÓRIO COMPLETO</button>
    </div>`;
}

async function forgePollPkg(taskId) {
  if (forgeState.pollTimer) {
    clearTimeout(forgeState.pollTimer);
    forgeState.pollTimer = null;
  }
  try {
    const d = await api(`/api/forge/pkg/jobs/${encodeURIComponent(taskId)}`);
    if ($("forge-progress-stage")) $("forge-progress-stage").textContent = d.stage || "Processando…";
    if ($("forge-progress-pct")) $("forge-progress-pct").textContent = `${d.percent ?? 0}%`;
    if ($("forge-progress-bar")) $("forge-progress-bar").value = d.percent ?? 0;
    if ($("forge-progress-elapsed")) $("forge-progress-elapsed").textContent = `${(d.elapsed ?? 0).toFixed(1)}s`;

    if (d.status === "processing" || d.status === "validating" || d.status === "queued" || d.status === "running") {
      if (d.status === "validating" || (d.percent ?? 0) >= 70 || (d.stage || "").toLowerCase().includes("validando")) {
        forgeSetStatus("VALIDANDO");
      } else {
        forgeSetStatus("PROCESSANDO");
      }
      forgeState.pollTimer = setTimeout(() => forgePollPkg(taskId), 400);
      return;
    }

    forgeState.busy = false;
    forgeState.activeTaskId = null;
    if ($("forge-progress-box")) $("forge-progress-box").hidden = true;

    if (d.status === "completed") {
      forgeState.lastPkg = d;
      if (d.bundle_report) {
        forgeState.lastValidation = d.bundle_report;
        forgeRenderValidation(d.bundle_report);
      }
      forgeRenderPkgResult(d);
      forgeSetStatus("PENDENTE DE TESTE FÍSICO");
      toast(`PKG pronto (${fmtBytes(d.pkg_bytes || 0)}) · Pendente de teste físico no PS4.`, "ok");
      await forgeLoadHistory(false);
    } else if (d.status === "cancelled") {
      forgeSetStatus("PRONTO");
      toast("Empacotamento PKG cancelado.");
    } else {
      forgeSetStatus("FALHOU");
      $("pkg-result").hidden = false;
      $("pkg-result").innerHTML = `<h2 class="card-title">Falha ao gerar PKG</h2><p class="card-note">${forgeEsc(d.error || "Erro desconhecido.")}</p>`;
      toast(d.error || "Falha ao gerar PKG.");
    }
    refreshChecklist();
  } catch (err) {
    forgeState.busy = false;
    forgeState.activeTaskId = null;
    if ($("forge-progress-box")) $("forge-progress-box").hidden = true;
    forgeSetStatus("FALHOU");
    toast(err.message);
    refreshChecklist();
  }
}

async function forgeBuildPkg() {
  if (forgeState.busy) return;
  if (!(await forgeEnsureSaved())) return;
  forgeState.busy = true;
  forgeSetStatus("PROCESSANDO");
  if ($("forge-progress-box")) {
    $("forge-progress-box").hidden = false;
    $("forge-progress-stage").textContent = "Iniciando empacotamento PKG…";
    $("forge-progress-pct").textContent = "5%";
    $("forge-progress-bar").value = 5;
    $("forge-progress-elapsed").textContent = "0.0s";
  }
  refreshChecklist();
  try {
    const fd = forgeBuildFormData(Boolean(Projects.id));
    const launch = await api("/api/forge/pkg/build", { method: "POST", body: fd });
    forgeState.activeTaskId = launch.task_id;
    forgePollPkg(launch.task_id);
  } catch (err) {
    forgeState.busy = false;
    if ($("forge-progress-box")) $("forge-progress-box").hidden = true;
    forgeSetStatus("FALHOU");
    toast(err.message);
    refreshChecklist();
  }
}

async function forgeCancelPkg() {
  if (!forgeState.activeTaskId) return;
  try {
    await api(`/api/forge/pkg/jobs/${encodeURIComponent(forgeState.activeTaskId)}/cancel`, { method: "POST" });
    toast("Solicitação de cancelamento enviada.");
  } catch (err) {
    toast(err.message);
  }
}

async function forgeLoadHistory(checkActive = true) {
  if (!$("forge-history-card")) return;
  try {
    // CZAR-AUD-013: First check whether this project/session has an active in-flight PKG build
    // so reloading or re-entering the Package tab immediately resumes progress and unlocks download on completion.
    if (checkActive) {
      try {
        const act = await api("/api/forge/pkg/active");
        const running = act?.active;
        if (running && (running.status === "processing" || running.status === "validating")) {
          const alreadyTracking = forgeState.busy && forgeState.activeTaskId === running.task_id;
          forgeState.busy = true;
          forgeState.activeTaskId = running.task_id;
          if ($("forge-progress-box")) {
            $("forge-progress-box").hidden = false;
            $("forge-progress-stage").textContent = running.stage || "Processando…";
            $("forge-progress-pct").textContent = `${running.percent ?? 0}%`;
            $("forge-progress-bar").value = running.percent ?? 0;
            $("forge-progress-elapsed").textContent = `${(running.elapsed ?? 0).toFixed(1)}s`;
          }
          forgeSetStatus(running.status === "validating" ? "VALIDANDO" : "PROCESSANDO");
          refreshChecklist();
          if (!alreadyTracking) {
            forgePollPkg(running.task_id);
          }
        }
      } catch { /* ignore active check failure */ }
    }

    const d = await api("/api/forge/pkg/history");
    const items = d.items || [];
    forgeState.history = items;
    if (!items.length) {
      $("forge-history-card").hidden = true;
      refreshChecklist();
      return;
    }
    $("forge-history-card").hidden = false;
    $("forge-history-list").innerHTML = items.map((it) => `
      <div class="forge-history-row">
        <div>
          <b><code>${forgeEsc(it.pkg_name)}</code></b>
          <div class="dim" style="font-size:12px">
            ${fmtBytes(it.pkg_bytes || 0)} · IRO_TAG ${forgeEsc(it.iro_tag_header ?? 2)}/${forgeEsc(it.iro_tag_sfo ?? 2)} · CATEGORY ${forgeEsc(it.category || "ac")} · ${it.structure_validated ? "Validado" : "Falhou"} · Pendente de teste físico
          </div>
        </div>
        <div class="forge-history-actions">
          <a class="btn btn-ghost" href="${Projects.mediaUrl(it.download)}" download="${forgeEsc(it.pkg_name)}">Baixar</a>
          <button type="button" class="btn btn-ghost" onclick="forgeOpenReport('${forgeEsc(it.pkg_id)}')">Relatório</button>
        </div>
      </div>`).join("");

    if (items[0]?.pkg_id && (!forgeState.lastPkg || (!forgeState.busy && forgeState.lastPkg.pkg_id !== items[0].pkg_id))) {
      try {
        const rep = await api(`/api/forge/pkg/${encodeURIComponent(items[0].pkg_id)}/report`);
        forgeState.lastPkg = {
          ...rep,
          download: items[0].download,
          report_url: items[0].report_url,
        };
        if (rep.bundle_report) forgeState.lastValidation = rep.bundle_report;
        if (rep.pkg_report?.structure_validated && !forgeState.busy) {
          forgeSetStatus("PENDENTE DE TESTE FÍSICO");
        }
      } catch { /* ignore transient history report fetch */ }
    }
    refreshChecklist();
  } catch { /* standalone or uninitialized workspace */ }
}

async function forgeOpenReport(pkgId = "") {
  const dlg = $("forge-report-dialog");
  const body = $("forge-report-body");
  if (!dlg || !body) return;

  let record = forgeState.lastPkg;
  if (pkgId && (!record || record.pkg_id !== pkgId)) {
    try {
      record = await api(`/api/forge/pkg/${encodeURIComponent(pkgId)}/report`);
    } catch (err) {
      toast(err.message);
      return;
    }
  }

  const pr = record?.pkg_report;
  const br = record?.bundle_report || forgeState.lastValidation;
  const parts = [];

  if (pr) {
    const rows = (pr.files || []).map((f) => `
      <tr>
        <td><code>${forgeEsc(f.partition || "")}</code></td>
        <td><code>${forgeEsc(f.path)}</code></td>
        <td>${fmtBytes(f.bytes || 0)}</td>
        <td><code>${forgeEsc((f.sha256 || "").slice(0, 16))}…</code></td>
      </tr>`).join("");
    const cmpText = pr.bundle_compared
      ? (pr.bundle_hashes_matched ? "100% correspondente (SHA-256)" : "Divergente do bundle")
      : (pr.image0_extracted ? "Image0 extraído (sem bundle de referência)" : "Image0 não extraído");
    parts.push(`
      <div class="forge-report-kv">
        <div><span>PKG CRIADO</span><b>${pr.pkg_created ? "Sim" : "Não"}</b></div>
        <div><span>ESTRUTURA VALIDADA</span><b>${pr.structure_validated ? "Sim" : "Não"}</b></div>
        <div><span>NÍVEL DE VERIFICAÇÃO</span><b>${forgeEsc(forgeVerificationLabel(pr.verification_level))}</b></div>
        <div><span>CABEÇALHO &amp; Sc0</span><b>Cabeçalho: ${pr.header_and_crypto_valid ? "OK" : "Falhou"} · Sc0: ${pr.sc0_validated ? "OK" : "Falhou"}</b></div>
        <div><span>TIPO DE TEMA</span><b>${forgeEsc(pr.volume_type)}</b></div>
        <div><span>CONTENT_ID</span><code>${forgeEsc(pr.content_id)}</code></div>
        <div><span>TITLE_ID</span><code>${forgeEsc(pr.title_id)}</code></div>
        <div><span>IRO_TAG</span><b>Cabeçalho 0x98 = ${forgeEsc(pr.iro_tag_header)} · SFO = ${forgeEsc(pr.iro_tag_sfo)}</b></div>
        <div><span>CATEGORY</span><b>${forgeEsc(pr.category)}</b></div>
        <div><span>bgmhome_loud</span><b>${pr.bgmhome_loud ? `${forgeEsc(pr.bgmhome_loud)} LKFS` : "Não aplicável"}</b></div>
        <div><span>SHA-256 do PKG</span><code>${forgeEsc(pr.pkg_sha256 || "")}</code></div>
        <div><span>Extração ↔ Bundle (SHA-256)</span><b>${forgeEsc(cmpText)}</b></div>
        <div><span>Builder / Validador</span><code>${forgeEsc(pr.builder_backend)} / ${forgeEsc(pr.validator_backend)}</code></div>
        <div><span>COMPATIBILIDADE FÍSICA</span><b>PENDENTE DE TESTE FÍSICO (PS4 Slim 12.52)</b></div>
      </div>
      ${(pr.errors || []).length ? `<div class="notice notice-warn"><b>Erros PKG:</b> ${(pr.errors || []).map(forgeEsc).join("; ")}</div>` : ""}
      ${(pr.warnings || []).length ? `<p class="card-note"><b>Avisos PKG:</b> ${(pr.warnings || []).map(forgeEsc).join("; ")}</p>` : ""}
      <h3 style="margin:6px 0 0">Arquivos e hashes verificados (${(pr.files || []).length})</h3>
      <table class="forge-files-table">
        <thead><tr><th>Partição</th><th>Caminho</th><th>Tamanho</th><th>SHA-256</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `);
  }

  if (br) {
    const bFiles = br.files || [];
    parts.push(`
      <h3 style="margin:8px 0 0">Validação do Bundle (${br.valid ? "Aprovado" : "Com erros"} · ${bFiles.length} arquivos)</h3>
      <ul class="forge-val-list">${(br.checks || []).map((it) => {
        const ok = it.status === "pass" || it.status === "ok";
        return `<li class="forge-val-item ${ok ? "is-ok" : it.status === "warn" ? "is-warn" : "is-fail"}">
          <span><b>${forgeEsc(it.label || it.category)}</b> — ${forgeEsc(it.detail || "")}</span>
          <code>${forgeEsc(it.path || "")}</code>
        </li>`;
      }).join("")}</ul>
    `);
  }

  body.innerHTML = parts.join("") || `<p class="card-note">Nenhum relatório disponível ainda. Clique em "VALIDAR MEU TEMA" ou "GERAR PKG".</p>`;
  dlg.showModal();
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
  if (bgm.jobId) {
    rows.push([t("snd.music"), "sound/bgm_home.at9", "ATRAC9 · 48 kHz · 144 kbps"]);
  }
  for (const slotId of Object.keys(sfx.chosen)) {
    rows.push([`SFX (${slotId})`, `sound/${slotId}.vag`, "PS-ADPCM · 48 kHz mono"]);
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

function appendComposition(fd) {
  if (!Projects.id) return;
  fd.append("composition", JSON.stringify(Compositor.capture() || { version: 1, surfaces: {} }));
  fd.append("composition_mode", bgMode());
  fd.append("animation_size", $("anim-size").value);
}

async function packageBuild(assetsOnly = false) {
  assetsOnly = assetsOnly === true;
  if (assetsOnly) {
    return forgeExportBundle();
  }
  return forgeBuildPkg();
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
  $("pkg-build").addEventListener("click", () => forgeBuildPkg());
  $("pkg-assets").addEventListener("click", () => forgeExportBundle());
  $("forge-btn-validate")?.addEventListener("click", () => forgeValidateTheme());
  $("forge-btn-bundle")?.addEventListener("click", () => forgeExportBundle());
  $("forge-btn-pkg")?.addEventListener("click", () => forgeBuildPkg());
  $("forge-btn-report")?.addEventListener("click", () => forgeOpenReport());
  $("forge-btn-cancel-pkg")?.addEventListener("click", () => forgeCancelPkg());
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
  Compositor.boot();
  forgeLoadHistory();
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

if ($("forge-audio-file")) {
  $("forge-audio-file").addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) forgeInspectFile(f);
    e.target.value = "";
  });
}
if ($("forge-audio-preview-btn")) {
  $("forge-audio-preview-btn").addEventListener("click", forgePreviewProcess);
}
if ($("forge-bgm-apply")) {
  $("forge-bgm-apply").addEventListener("click", forgeConvertAndApplyBgm);
}

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
if ($("anim-diag-run")) {
  $("anim-diag-run").addEventListener("click", () => startAnimDiagnostic(anim.jobId));
}
if ($("anim-diag-cancel")) {
  $("anim-diag-cancel").addEventListener("click", () => cancelAnimDiagnostic());
}
if ($("anim-diag-ouroboros")) {
  $("anim-diag-ouroboros").addEventListener("click", async (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const href = $("anim-diag-ouroboros").href;
    if (Projects.id) await Projects.flush();
    location.assign(href);
  });
}


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

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { player.pause(); pvPlayer.stop(); }
  else if ($("panel-preview").classList.contains("is-active")) renderPreview();
});

boot();
