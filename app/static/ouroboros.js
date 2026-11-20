"use strict";
const by = (id) => document.getElementById(id);
const fmt = (n) => Number(n).toLocaleString("pt-BR", { maximumFractionDigits: 3 });

const RES_CAPS = [13, 24, 48];
const STRATEGY_COLORS = {
  protect: "rgba(56, 189, 248, 0.36)",
  animate: "rgba(52, 211, 153, 0.36)",
  remove_transient: "rgba(248, 113, 113, 0.36)",
  fix_seam: "rgba(251, 191, 36, 0.36)",
  adjust_motion: "rgba(192, 132, 252, 0.36)",
};
const STRATEGY_LABELS = {
  protect: "Proteger",
  animate: "Animar",
  remove_transient: "Remover transitório",
  fix_seam: "Corrigir costura",
  adjust_motion: "Ajustar movimento",
};
const CATEGORY_LABELS = {
  best_natural: "Melhor loop natural",
  best_correctable: "Melhor loop corrigível",
  best_synthesized: "Melhor loop sintetizado",
  best_convincing_duration: "Melhor duração convincente",
  best_balance: "Melhor equilíbrio qualidade/fluidez",
};
const CAL_LABELS = {
  best_fluidity: "Melhor fluidez",
  best_definition: "Melhor definição",
  best_continuity: "Melhor continuidade",
  best_duration: "Maior duração convincente",
  best_balance: "Melhor equilíbrio",
};

let analysis,
  deluxeAnalysis,
  lastDeluxeKey = null,
  ticket,
  busy = false,
  frames = [],
  wait = 0,
  index = 0,
  animation = 0,
  generation = 0;

// Regional editor state with full Undo/Redo history
let regionState = [];
let regionPast = [];
let regionFuture = [];
let editorBgImg = null;
let drawing = null;

// Real playback presentation telemetry (separates browser refresh jitter from video content!)
let telemetry = {
  startedAt: 0,
  lastDrawAt: 0,
  drawnTicks: 0,
  skippedTicks: 0,
  seamDwellMs: [],
  deltasMs: [],
};

const params = new URLSearchParams(location.search);
function url(path) {
  const u = new URL(path, location.origin);
  if (by("project").value) u.searchParams.set("project", by("project").value);
  return u;
}
async function request(path, data, method) {
  const opts =
    data === undefined
      ? {}
      : {
          method: method || "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        };
  const r = await fetch(url(path), opts);
  const body = await r.json();
  if (!r.ok) throw Error(typeof body.detail === "string" ? body.detail : "Dados inválidos para o diagnóstico.");
  return body;
}
function stop() {
  cancelAnimationFrame(animation);
  animation = 0;
  by("play").textContent = "Reproduzir";
}
function release() {
  stop();
  frames.forEach((f) => f.close());
  frames = [];
  index = 0;
  lastDeluxeKey = null;
  by("player").hidden = true;
  by("original").pause();
  by("original").removeAttribute("src");
  by("original").load();
  by("artifacts").hidden = true;
  by("dlx-apply").hidden = true;
  by("dlx-apply-status").textContent = "";
  by("dlx-diff-fig").hidden = true;
  by("dlx-metrics-table").replaceChildren();
  by("dlx-telemetry").textContent = "";
}
function draw() {
  const cv = by("player-canvas"),
    f = frames[index];
  if (!f) return;
  if (cv.width !== f.width || cv.height !== f.height) {
    cv.width = f.width;
    cv.height = f.height;
  }
  cv.getContext("2d").drawImage(f, 0, 0);
}
function play(seam = false) {
  stop();
  if (!frames.length) return;
  // Audited rAF scheduler: anchor begin on the FIRST rAF timestamp `now` to avoid start jitter.
  let begin = null;
  const first = index,
    n = frames.length,
    targetMs = wait * 1000;
  by("play").textContent = "Pausar";
  let previous = -1;
  telemetry = { startedAt: 0, lastDrawAt: 0, drawnTicks: 0, skippedTicks: 0, seamDwellMs: [], deltasMs: [] };

  function tick(now) {
    animation = requestAnimationFrame(tick);
    if (begin === null) {
      begin = now;
      telemetry.startedAt = now;
    }
    const t = Math.max(0, Math.floor((now - begin) / targetMs));
    if (t === previous) return;
    if (previous >= 0 && t > previous + 1) {
      telemetry.skippedTicks += t - previous - 1;
    }
    if (telemetry.lastDrawAt > 0) {
      const dt = now - telemetry.lastDrawAt;
      telemetry.deltasMs.push(dt);
      if (telemetry.deltasMs.length > 60) telemetry.deltasMs.shift();
      if (!seam && index === n - 1) {
        telemetry.seamDwellMs.push(dt);
        if (telemetry.seamDwellMs.length > 20) telemetry.seamDwellMs.shift();
      }
    }
    telemetry.lastDrawAt = now;
    telemetry.drawnTicks++;
    previous = t;
    index = seam ? [n - 2, n - 1, 0, 1][t % 4] : (first + t) % n;
    draw();
    const cycles = Math.floor(t / n);
    by("counter").textContent = seam
      ? "Inspeção da junção"
      : `Quadro ${index + 1}/${n} · ${cycles} voltas completas`;
    if (!seam && telemetry.deltasMs.length >= 2) {
      const avgDt = telemetry.deltasMs.reduce((a, b) => a + b, 0) / telemetry.deltasMs.length;
      const seamAvg = telemetry.seamDwellMs.length
        ? telemetry.seamDwellMs.reduce((a, b) => a + b, 0) / telemetry.seamDwellMs.length
        : avgDt;
      by("dlx-telemetry").textContent = `Telemetria player: alvo ${fmt(targetMs)} ms · médio real ${fmt(avgDt)} ms · junção ${fmt(seamAvg)} ms · saltos rAF navegador: ${telemetry.skippedTicks}`;
    }
  }
  animation = requestAnimationFrame(tick);
}

function setButtonsDisabled(flag) {
  by("project").disabled = flag;
  by("video").disabled = flag;
  by("run").disabled = flag || !by("video").value || !by("project").value;
  by("deluxe-analyze").disabled = flag || !by("video").value || !by("project").value;
  by("dlx-calibrate").disabled = flag || !by("video").value || !by("project").value;
  by("dlx-render").disabled = flag || !by("video").value || !by("project").value;
  by("dlx-save-recipe").disabled = flag || !by("project").value;
}

function updateTimelineReadout() {
  const s = Number(by("dlx-start").value) || 0;
  const e = Number(by("dlx-end").value) || 0;
  const z = Number(by("dlx-zoom").value) || 1;
  const dur = Math.max(0, e - s);
  const f = Number(by("dlx-frames").value) || 16;
  const w = Number(by("dlx-wait").value) || 0.067;
  by("dlx-timeline-read").textContent = `Trecho: ${fmt(s)} s → ${fmt(e)} s (janela ${fmt(dur)} s · zoom ${fmt(z)}×) · Saída: ${f} quadros × ${fmt(w * 1000)} ms = ${fmt(f * w)} s`;
}

function selectSource() {
  generation++;
  release();
  analysis = null;
  deluxeAnalysis = null;
  editorBgImg = null;
  by("diag").hidden = true;
  by("results").hidden = true;
  by("deluxe-diag").hidden = true;
  by("dlx-cal-recs").replaceChildren();
  by("status").textContent = "";
  setButtonsDisabled(false);
  drawRegionCanvas();
}
async function sources() {
  selectSource();
  const revision = generation;
  by("video").replaceChildren();
  setButtonsDisabled(true);
  if (!by("project").value) {
    by("status").textContent = "Crie um projeto no editor para iniciar.";
    return;
  }
  by("back").href = "/?project=" + by("project").value;
  const options = [],
    current = await request("/api/ouroboros/current");
  if (current) options.push(new Option("Fundo do projeto · " + current.name, "animated/" + current.id));
  for (const kind of ["video", "animation"]) {
    let offset = 0;
    do {
      const p = await request(`/api/library?kind=${kind}&offset=${offset}`);
      options.push(...p.items.map((a) => new Option(a.displayName, "library/" + a.id)));
      offset = p.next;
    } while (offset !== null);
  }
  if (revision !== generation) return;
  by("video").replaceChildren(...options);
  setButtonsDisabled(false);
  if (!options.length)
    by("status").textContent = "Importe um vídeo na Biblioteca ou selecione um fundo animado no editor.";
  else {
    loadSavedRecipes().catch(() => {});
  }
}

function display() {
  const s = analysis.sentinela,
    labels = {
      A: "movimento estrutural",
      B: "atividade localizada ou luminosa",
      C: "regiões mistas",
      D: "movimento problemático",
      E: "movimento insuficiente",
    };
  by("summary").textContent =
    s.categories.map((k) => k + " · " + labels[k]).join(" / ") +
    ". " +
    s.reasons.join(" ") +
    ` Simetria de trajetória ${s.pingPong ? "detectada" : "sem confirmação"}. Confiança ${s.confidence}.`;
  by("details").textContent = JSON.stringify(s, null, 2);
  by("diag").hidden = false;
  by("groups").replaceChildren();
  analysis.groups.forEach((g, ri) => {
    const h = document.createElement("h3");
    h.textContent = `${g.size.join(" × ")} · ${g.maxFrames} quadros disponíveis`;
    by("groups").append(h);
    if (!g.candidates.length) by("groups").append(g.verdict);
    if (g.unreadable) {
      const warning = document.createElement("p");
      warning.textContent = `${g.unreadable} trecho(s) ficaram fora da classificação por falha de leitura.`;
      by("groups").append(warning);
    }
    g.candidates.forEach((c, ci) => {
      const a = document.createElement("article"),
        label = document.createElement("strong"),
        p = document.createElement("p"),
        metrics = document.createElement("p"),
        button = document.createElement("button"),
        q = c.quality;
      label.textContent = q.verdict;
      p.textContent = `${fmt(c.start)} → ${fmt(c.end)} s · duração ${fmt(c.duration)} s · ${c.frames} quadros · ${fmt(c.fps)} FPS · ${fmt(c.estimatedBytes)} B estimados.`;
      metrics.textContent = `Junção ${fmt(q.seamDifference)} · interna ${fmt(q.internalDifference)} · custo visual ${fmt(q.visualCost)} · temporal ${fmt(q.temporalCost)} · direção ${q.directionMeasured ? fmt(100 * q.oppositeFraction) + "% oposta" : "sem medida confiável"}. Confiança ${q.confidence}. ${q.issues.join(" ") || q.reason}`;
      button.textContent = "Conferir DDS";
      button.onclick = () => operate("preview", { analysis: analysis.key, resolution: ri, candidate: ci });
      a.append(label, p, metrics, button);
      by("groups").append(a);
    });
  });
  by("results").hidden = false;
}

function applyCandidateToControls(c) {
  by("dlx-start").value = Number(c.start).toFixed(3);
  by("dlx-end").value = Number(c.end).toFixed(3);
  const resIdx = [1280, 960, 640].indexOf(c.size[0]);
  if (resIdx >= 0) {
    by("dlx-res").value = String(resIdx);
    by("dlx-frames").max = String(RES_CAPS[resIdx]);
  }
  by("dlx-frames").value = String(c.frames);
  by("dlx-wait").value = Number(c.wait).toFixed(4);
  if (c.recommendedStrategy === "genesis_periodic" || c.recommendedStrategy === "eulerian_synthesis") {
    by("dlx-mode").value = "genesis_2d";
  } else if (c.recommendedStrategy === "regional_reconstruction") {
    by("dlx-mode").value = "regional";
  }
  updateTimelineReadout();
}

function displayDeluxeAnalysis() {
  const s = deluxeAnalysis.sentinela;
  by("deluxe-summary").textContent =
    `Classificação Supreme: ${s.classification} (confiança ${s.confidenceLabel} · ${fmt(s.confidence * 100)}%). ` +
    `${s.reasons.join(" ")} ` +
    `Câmera: ${s.levelA.cameraMoving ? "móvel" : "fixa"} · Regiões detectadas: ${s.levelB.regions.length} · ` +
    `VFR: ${s.source?.isVFR ? "sim" : "não"} · Recorrência real: ${s.levelD.trueRecurrence ? s.levelD.periodSamples + " amostras" : "não"}.`;
  by("deluxe-details").textContent = JSON.stringify(s, null, 2);

  if (s.source?.duration) {
    by("dlx-end").value = Math.min(Number(s.source.duration), Math.max(1.0, Number(by("dlx-end").value))).toFixed(3);
  }
  updateTimelineReadout();

  const container = by("deluxe-categories");
  container.replaceChildren();
  const cats = deluxeAnalysis.categories || {};
  Object.entries(CATEGORY_LABELS).forEach(([key, title]) => {
    const c = cats[key];
    if (!c) return;
    const card = document.createElement("div");
    card.className = "dlx-card";
    const h = document.createElement("h4");
    h.textContent = title;
    const p1 = document.createElement("p");
    p1.textContent = `${c.size.join("×")} · ${fmt(c.start)}→${fmt(c.end)}s · ${c.frames}q · wait ${fmt(c.wait * 1000)} ms (${fmt(c.duration)} s)`;
    const p2 = document.createElement("p");
    p2.textContent = `${c.costs.classification} · custo ${fmt(c.costs.totalCost)} · SSIM junção ${fmt(c.evaluation.metrics.visualSimilarity.seamSsim)}`;
    const btn = document.createElement("button");
    btn.textContent = "Carregar na timeline";
    btn.onclick = () => {
      applyCandidateToControls(c);
      by("status").textContent = `Candidato '${title}' carregado na timeline.`;
    };
    card.append(h, p1, p2, btn);
    container.append(card);
  });
  by("deluxe-diag").hidden = false;
}

function displayCalibration(cal) {
  const container = by("dlx-cal-recs");
  container.replaceChildren();
  Object.entries(CAL_LABELS).forEach(([key, title]) => {
    const c = cal.recommendations?.[key];
    if (!c) return;
    const card = document.createElement("div");
    card.className = "dlx-card";
    const h = document.createElement("h4");
    h.textContent = title;
    const p1 = document.createElement("p");
    p1.textContent = `${c.size.join("×")} · ${c.frames}q · ${fmt(c.waitMs)} ms (${fmt(c.fps)} FPS) · ${fmt(c.duration)} s`;
    const p2 = document.createElement("p");
    p2.textContent = `DDS ${fmt(c.ddsBytes)} B (${fmt(c.budgetPercent)}%) · judder ${fmt(c.judderRisk)} · SSIM ${fmt(c.seamSsim)}`;
    const btn = document.createElement("button");
    btn.textContent = "Usar calibração";
    btn.onclick = () => {
      const ri = [1280, 960, 640].indexOf(c.size[0]);
      if (ri >= 0) {
        by("dlx-res").value = String(ri);
        by("dlx-frames").max = String(RES_CAPS[ri]);
      }
      by("dlx-frames").value = String(c.frames);
      by("dlx-wait").value = Number(c.wait).toFixed(4);
      updateTimelineReadout();
      by("status").textContent = `Calibração '${title}' selecionada.`;
    };
    card.append(h, p1, p2, btn);
    container.append(card);
  });
}

function renderBeforeAfterTable(cmp) {
  const host = by("dlx-metrics-table");
  host.replaceChildren();
  if (!cmp || !cmp.matrix) return;
  const tbl = document.createElement("table");
  tbl.className = "dlx-metrics-tbl";
  tbl.innerHTML =
    "<thead><tr><th>Métrica (pós-DDS)</th><th>Antes</th><th>Depois</th><th>Delta</th><th>Estado</th></tr></thead><tbody></tbody>";
  const tb = tbl.querySelector("tbody");
  Object.entries(cmp.matrix).forEach(([k, v]) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${k}</td><td>${fmt(v.before)}</td><td>${fmt(v.after)}</td><td>${fmt(v.delta)}</td><td>${v.status}</td>`;
    tb.append(tr);
  });
  host.append(tbl);
}

async function operate(kind, payload) {
  if (busy) return;
  busy = true;
  stop();
  setButtonsDisabled(true);
  by("progress").hidden = false;
  by("progress").value = 0;
  try {
    let t = await request("/api/ouroboros/" + kind, payload);
    ticket = t.id;
    by("cancel").hidden = false;
    while (t.state === "working") {
      by("status").textContent = t.stage;
      by("progress").value = t.percent;
      await new Promise((r) => setTimeout(r, 250));
      t = await request("/api/ouroboros/jobs/" + ticket);
    }
    by("cancel").hidden = true;
    if (t.state !== "ready") throw Error(t.stage);
    const r = await request("/api/ouroboros/result/" + t.key);
    if (kind === "analyze") {
      release();
      analysis = r;
      display();
    } else if (kind === "deluxe/analyze") {
      deluxeAnalysis = r;
      displayDeluxeAnalysis();
    } else if (kind === "deluxe/calibrate") {
      displayCalibration(r);
    } else {
      const isDeluxe = kind === "deluxe/render";
      const loaded = [];
      try {
        for (let i = 0; i < r.frames; i++) {
          const fname = `${String(i).padStart(2, "0")}.webp`;
          const fpath = isDeluxe
            ? `/api/ouroboros/deluxe/artifact/${t.key}/${fname}`
            : `/api/ouroboros/result/${t.key}/${fname}`;
          const res = await fetch(url(fpath));
          if (!res.ok) throw Error("Quadro indisponível.");
          loaded.push(await createImageBitmap(await res.blob()));
        }
      } catch (err) {
        loaded.forEach((f) => f.close());
        throw err;
      }
      release();
      frames = loaded;
      wait = r.wait;
      draw();
      if (kind === "genesis") {
        by("measurement").textContent =
          `DDS decodificados · ${r.frames} quadros · ${fmt(r.bytes)} bytes · ${fmt(r.wait)} s por quadro · deslocamento na junção por faixa: ${r.measured.at(-1).join(", ")} px.`;
        by("artifacts").hidden = false;
        by("original").src = url("/api/ouroboros/genesis-source/" + payload.kind + "/" + payload.id);
        by("reference").src = url("/api/ouroboros/genesis/" + t.key + "/source.png");
        by("download").href = url("/api/ouroboros/genesis/" + t.key + "/background.raf.zip");
      } else if (isDeluxe) {
        lastDeluxeKey = t.key;
        const post = r.finalPostDds;
        const stepsTxt = r.finish?.appliedSteps?.length ? r.finish.appliedSteps.join(", ") : "nenhum ajuste extra necessário";
        by("measurement").textContent =
          `Deluxe Pós-DDS · ${r.size.join(" × ")} · ${r.frames} quadros · wait ${fmt(r.wait)} s (${fmt(r.duration)} s) · ${fmt(r.bytes)} B reais · Estratégia: ${r.strategyUsed} · Acabamento: ${stepsTxt} · SSIM junção: ${fmt(post.metrics.visualSimilarity.seamSsim)} · Problemas: ${post.issues.length ? post.issues.join(" ") : "nenhum"}.`;
        by("artifacts").hidden = false;
        by("dlx-apply").hidden = false;
        by("dlx-diff-fig").hidden = false;
        by("original").src = url("/api/ouroboros/genesis-source/" + payload.kind + "/" + payload.id);
        const refUrl = url("/api/ouroboros/deluxe/artifact/" + t.key + "/source.png");
        by("reference").src = refUrl;
        by("dlx-diff-img").src = url("/api/ouroboros/deluxe/artifact/" + t.key + "/diff.png");
        by("download").href = url("/api/ouroboros/deluxe/artifact/" + t.key + "/background.raf.zip");
        renderBeforeAfterTable(r.beforeAfterRaw);
        loadEditorReference(refUrl);
      } else {
        const clip = analysis.groups[payload.resolution].candidates[payload.candidate];
        by("measurement").textContent =
          `${r.size.join(" × ")} · ${fmt(clip.start)} → ${fmt(clip.end)} s. Fonte · ${r.sourceVerdict}. DDS · ${r.quality.verdict}. ${r.quality.reason} ${fmt(r.bytes)} bytes reais · espera ${fmt(wait)} s. A prévia mede os DDS. A aplicação no PS4 exige teste físico.`;
      }
      by("player").hidden = false;
      by("player").scrollIntoView();
      play();
    }
    by("status").textContent = "Concluído.";
  } catch (e) {
    by("status").textContent = e.message;
  } finally {
    ticket = null;
    busy = false;
    by("cancel").hidden = by("progress").hidden = true;
    setButtonsDisabled(false);
  }
}

// ==================== CINEMATIC CONTENT EDITOR (TOUCH MASK CANVAS) ====================

function loadEditorReference(srcUrl) {
  const img = new Image();
  img.onload = () => {
    editorBgImg = img;
    drawRegionCanvas();
  };
  img.src = srcUrl;
}

function pushRegionHistory(prev) {
  regionPast.push(structuredClone(prev));
  if (regionPast.length > 40) regionPast.shift();
  regionFuture = [];
  syncRegionUi();
}

function syncRegionUi() {
  by("dlx-undo").disabled = !regionPast.length;
  by("dlx-redo").disabled = !regionFuture.length;
  const list = by("dlx-region-list");
  list.replaceChildren();
  regionState.forEach((reg, idx) => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = `${reg.name} (${STRATEGY_LABELS[reg.strategy] || reg.strategy}, ${reg.speedScale}×)`;
    const rm = document.createElement("button");
    rm.textContent = "Remover";
    rm.onclick = () => {
      const prev = structuredClone(regionState);
      regionState.splice(idx, 1);
      pushRegionHistory(prev);
    };
    li.append(span, rm);
    list.append(li);
  });
  drawRegionCanvas();
}

function drawRegionCanvas() {
  const cv = by("dlx-region-canvas");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const w = cv.width,
    h = cv.height;
  ctx.clearRect(0, 0, w, h);
  if (editorBgImg) {
    ctx.drawImage(editorBgImg, 0, 0, w, h);
  } else {
    ctx.fillStyle = "#111827";
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = "#64748b";
    ctx.font = "16px sans-serif";
    ctx.fillText("Desenhe regiões com toque ou mouse (renderize para ver o quadro real)", 18, 34);
  }
  const all = drawing ? [...regionState, drawing] : regionState;
  all.forEach((reg) => {
    ctx.fillStyle = STRATEGY_COLORS[reg.strategy] || "rgba(255,255,255,0.3)";
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    reg.shapes.forEach((sh) => {
      ctx.beginPath();
      if (sh.kind === "rect" && sh.bounds) {
        const [x0, y0, x1, y1] = sh.bounds;
        const rx = Math.min(x0, x1) * w,
          ry = Math.min(y0, y1) * h;
        const rw = Math.abs(x1 - x0) * w,
          rh = Math.abs(y1 - y0) * h;
        ctx.rect(rx, ry, rw, rh);
        ctx.fill();
        ctx.stroke();
      } else if (sh.kind === "ellipse" && sh.bounds) {
        const [cx, cy, rx, ry] = sh.bounds;
        ctx.ellipse(cx * w, cy * h, Math.max(2, Math.abs(rx) * w), Math.max(2, Math.abs(ry) * h), 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      } else if (sh.kind === "brush" && sh.points && sh.points.length) {
        const rad = Math.max(4, (sh.radiusNorm || 0.04) * Math.min(w, h));
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.lineWidth = rad * 2;
        ctx.strokeStyle = STRATEGY_COLORS[reg.strategy] || "rgba(255,255,255,0.35)";
        ctx.moveTo(sh.points[0][0] * w, sh.points[0][1] * h);
        for (let i = 1; i < sh.points.length; i++) {
          ctx.lineTo(sh.points[i][0] * w, sh.points[i][1] * h);
        }
        ctx.stroke();
      }
    });
  });
}

function canvasNormPoint(ev) {
  const cv = by("dlx-region-canvas");
  const rect = cv.getBoundingClientRect();
  return [
    Math.min(1, Math.max(0, (ev.clientX - rect.left) / Math.max(1, rect.width))),
    Math.min(1, Math.max(0, (ev.clientY - rect.top) / Math.max(1, rect.height))),
  ];
}

function initRegionCanvasEvents() {
  const cv = by("dlx-region-canvas");
  if (!cv) return;
  cv.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    cv.setPointerCapture(ev.pointerId);
    const [x, y] = canvasNormPoint(ev);
    const tool = by("dlx-tool").value;
    const strat = by("dlx-strategy").value;
    const id = "reg_" + (regionState.length + 1) + "_" + Math.floor(Math.random() * 999);
    const shape =
      tool === "brush"
        ? { kind: "brush", mode: "add", points: [[x, y]], radiusNorm: 0.045 }
        : tool === "ellipse"
          ? { kind: "ellipse", mode: "add", bounds: [x, y, 0.02, 0.02] }
          : { kind: "rect", mode: "add", bounds: [x, y, x, y] };
    drawing = {
      id,
      name: `${STRATEGY_LABELS[strat] || strat} #${regionState.length + 1}`,
      strategy: strat,
      shapes: [shape],
      featherPx: Number(by("dlx-feather").value) || 6,
      speedScale: Number(by("dlx-reg-speed").value) || 1.0,
      enabled: true,
      _origin: [x, y],
    };
    drawRegionCanvas();
  });
  cv.addEventListener("pointermove", (ev) => {
    if (!drawing) return;
    ev.preventDefault();
    const [x, y] = canvasNormPoint(ev);
    const sh = drawing.shapes[0];
    if (sh.kind === "rect") {
      sh.bounds[2] = x;
      sh.bounds[3] = y;
    } else if (sh.kind === "ellipse") {
      const [ox, oy] = drawing._origin;
      sh.bounds = [(ox + x) / 2, (oy + y) / 2, Math.abs(x - ox) / 2, Math.abs(y - oy) / 2];
    } else if (sh.kind === "brush") {
      sh.points.push([x, y]);
    }
    drawRegionCanvas();
  });
  const finishDraw = () => {
    if (!drawing) return;
    const prev = structuredClone(regionState);
    const { _origin, ...clean } = drawing;
    drawing = null;
    regionState.push(clean);
    pushRegionHistory(prev);
  };
  cv.addEventListener("pointerup", finishDraw);
  cv.addEventListener("pointercancel", finishDraw);
}

async function loadSavedRecipes() {
  try {
    const res = await request("/api/ouroboros/deluxe/recipes");
    if (res.recipes?.length) {
      const first = await request("/api/ouroboros/deluxe/recipes/" + encodeURIComponent(res.recipes[0].id));
      regionState = first.regions || [];
      regionPast = [];
      regionFuture = [];
      syncRegionUi();
      by("dlx-recipe-status").textContent = `Receita '${res.recipes[0].name}' carregada (${regionState.length} região(ões)).`;
    }
  } catch {
    /* Project may have no saved regional recipes yet. */
  }
}

function buildRecipePayload() {
  return {
    version: 1,
    name: "Receita Deluxe",
    sourceKey: by("video").value || "",
    regions: regionState,
  };
}

// ==================== EVENT WIRING ====================

by("run").onclick = () => {
  const [kind, id] = by("video").value.split("/");
  operate("analyze", { kind, id });
};
by("deluxe-analyze").onclick = () => {
  const [kind, id] = by("video").value.split("/");
  operate("deluxe/analyze", { kind, id });
};
by("dlx-calibrate").onclick = () => {
  const [kind, id] = by("video").value.split("/");
  operate("deluxe/calibrate", {
    kind,
    id,
    start: Number(by("dlx-start").value) || 0,
    end: Number(by("dlx-end").value) || 1.0,
    verifyDds: false,
  });
};
by("dlx-render").onclick = () => {
  const [kind, id] = by("video").value.split("/");
  const resIdx = Number(by("dlx-res").value);
  const mode = by("dlx-mode").value;
  operate("deluxe/render", {
    kind,
    id,
    start: Number(by("dlx-start").value) || 0,
    end: Number(by("dlx-end").value) || 1.0,
    frames: Math.min(RES_CAPS[resIdx], Math.max(4, Number(by("dlx-frames").value) || 16)),
    resolution: resIdx,
    wait: Number(by("dlx-wait").value) || 0.067,
    mode,
    deluxeFinish: by("dlx-finish-toggle").checked,
    stabilizeCamera: by("dlx-stab-toggle").checked,
    speedScale: Number(by("dlx-speed").value) || 1.0,
    recipe: regionState.length ? buildRecipePayload() : null,
  });
};
by("dlx-apply").onclick = async () => {
  if (!lastDeluxeKey) return;
  by("dlx-apply").disabled = true;
  by("dlx-apply-status").textContent = "Aplicando loop Deluxe ao projeto…";
  try {
    const res = await request("/api/ouroboros/deluxe/apply", { key: lastDeluxeKey });
    by("dlx-apply-status").textContent =
      `Aplicado ao projeto! Revisão ${res.revision} · Job ${res.jobId} · ${res.frames} quadros · ${fmt(res.ddsBytes)} B DDS (equivalência RAF garantida).`;
  } catch (e) {
    by("dlx-apply-status").textContent = "Erro ao aplicar: " + e.message;
  } finally {
    by("dlx-apply").disabled = false;
  }
};
by("dlx-undo").onclick = () => {
  if (!regionPast.length) return;
  regionFuture.push(structuredClone(regionState));
  regionState = regionPast.pop();
  syncRegionUi();
};
by("dlx-redo").onclick = () => {
  if (!regionFuture.length) return;
  regionPast.push(structuredClone(regionState));
  regionState = regionFuture.pop();
  syncRegionUi();
};
by("dlx-clear-regions").onclick = () => {
  if (!regionState.length) return;
  const prev = structuredClone(regionState);
  regionState = [];
  pushRegionHistory(prev);
};
by("dlx-save-recipe").onclick = async () => {
  try {
    const res = await request("/api/ouroboros/deluxe/recipes/principal", buildRecipePayload(), "PUT");
    by("dlx-recipe-status").textContent = `Receita salva no projeto (${res.regionCount} região(ões)).`;
  } catch (e) {
    by("dlx-recipe-status").textContent = "Erro ao salvar receita: " + e.message;
  }
};
by("dlx-res").onchange = () => {
  const cap = RES_CAPS[Number(by("dlx-res").value)] || 48;
  by("dlx-frames").max = String(cap);
  if (Number(by("dlx-frames").value) > cap) by("dlx-frames").value = String(cap);
  updateTimelineReadout();
};
["dlx-start", "dlx-end", "dlx-zoom", "dlx-frames", "dlx-wait"].forEach((id) => {
  by(id).oninput = updateTimelineReadout;
});

by("cancel").onclick = () =>
  request("/api/ouroboros/jobs/" + ticket + "/cancel", {}).catch((e) => (by("status").textContent = e.message));
by("video").onchange = selectSource;
by("project").onchange = () => sources().catch((e) => (by("status").textContent = e.message));
by("play").onclick = () => (animation ? stop() : play());
by("seam").onclick = () => play(true);
by("step").onclick = () => {
  stop();
  index = (index + 1) % frames.length;
  draw();
  by("counter").textContent = `Quadro ${index + 1}/${frames.length}`;
};
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stop();
});

by("synthesize").onclick = () => {
  if (!by("video").value) {
    by("status").textContent = "Selecione um vídeo importado.";
    return;
  }
  const [kind, id] = by("video").value.split("/");
  operate("genesis", {
    kind,
    id,
    resolution: by("output").selectedIndex,
    axis: by("axis").value,
    rate: Number(by("rate").value),
  });
};

initRegionCanvasEvents();
updateTimelineReadout();
drawRegionCanvas();

(async () => {
  const list = await request("/api/projects");
  by("project").replaceChildren(...list.map((p) => new Option(p.name, p.id)));
  if (list.some((p) => p.id === params.get("project"))) by("project").value = params.get("project");
  await sources();
})().catch((e) => (by("status").textContent = e.message));
