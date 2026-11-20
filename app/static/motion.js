/* The source clock always advances. Preload one measured sequence before playback. */
const Motion = (() => {
  const el = (name) => document.getElementById(`motion-${name}`);
  const fmt = (v, digits = 3) => Number(v).toLocaleString("pt-BR", { maximumFractionDigits: digits });
  const rounded = (v) => Number(v.toFixed(6));
  const tab = (name) => document.querySelector(`.tab[data-tab="${name}"]`);
  const endpoint = (key, file = "") => `/api/motion/results/${key}${file ? "/" + file : ""}`;
  const fileUrl = (key, file) => Projects.mediaUrl(endpoint(key, file));
  const fileName = (variant, index) => `${variant}-${String(index).padStart(2, "0")}.webp`;
  const currentSurface = () => Compositor.capture()?.surfaces.animated || null;
  const request = (url, data) =>
    api(
      url,
      data === undefined
        ? undefined
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
          },
    );
  const words = {
    candidato: "Candidato",
    revisar: "Revisão visual",
    inadequado: "Inadequado",
    inversao: "mudança de direção",
    repeticao: "quadro repetido",
    corte: "corte ou salto visual",
    luminosidade: "variação de luz",
    salto: "deslocamento abrupto",
    saturacao: "salto de saturação",
    pouca_evidencia: "pouca evidência de movimento",
  };
  let source = null,
    state = null,
    analysis = null,
    report = null,
    job = null,
    pending = false;
  let past = [],
    future = [],
    images = [],
    loadedKey = "",
    loadVersion = 0,
    playing = 0;
  let index = 0,
    turns = 0,
    startClock = 0,
    initialIndex = 0,
    seamOnly = false,
    previewWait = 0;
  let baseImage = null,
    baseKey = "",
    savedReport = "",
    draftValid = true,
    appliedInfo = null;
  const capture = () => (state && state.job === anim.jobId ? structuredClone(state) : null);
  const activeKey = () => (state?.job === anim.jobId && state?.applied) || null;
  const status = (text) => {
    el("status").textContent = text;
  };
  function stop() {
    cancelAnimationFrame(playing);
    playing = 0;
    el("play").textContent = "Reproduzir";
  }
  function release() {
    stop();
    ++loadVersion;
    images.forEach((f) => f.close());
    images = [];
    loadedKey = "";
    const canvas = el("canvas");
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    el("position").textContent = "";
    el("turns").hidden = true;
  }
  function restore(value) {
    release();
    past = [];
    future = [];
    state = value ? structuredClone(value) : null;
    analysis = null;
    report = null;
    appliedInfo = null;
    savedReport = "";
    baseImage = null;
    baseKey = "";
    el("strip").replaceChildren();
    el("points").replaceChildren();
    el("candidates").replaceChildren();
  }
  function sourceChanged(data) {
    if (state && state.job !== data?.job_id) restore(null);
    source = data ? { job: data.job_id, ...data.source } : null;
    if (!source) restore(null);
    el("active-note").hidden = !activeKey();
    if (tab("motion").classList.contains("is-active")) activate();
  }
  function captureChange(next, history = true) {
    if (JSON.stringify(state) === JSON.stringify(next)) return;
    if (history) {
      past.push(structuredClone(state));
      if (past.length > 35) past.shift();
      future = [];
    }
    state = next;
    Projects.changed();
    el("active-note").hidden = !activeKey();
    el("off").hidden = !activeKey();
    el("undo").disabled = !past.length;
    el("redo").disabled = !future.length;
  }
  function initial() {
    const end = Math.max(0.06, Math.min(1.5, source.duration - 0.001));
    return {
      version: 1,
      job: source.job,
      clip: {
        start: 0,
        end: rounded(end),
        frames: Math.max(3, Math.min(36, Math.round(end * 16))),
        size: [640, 360],
        profile: "smooth",
        strategy: "cut",
        protect: null,
      },
      analysis: null,
      rendered: null,
      applied: null,
    };
  }
  function renderSettings() {
    const c = (state || initial()).clip;
    for (const k of ["start", "end", "frames", "strategy", "profile"]) el(k).value = c[k];
    el("size").value = c.size.join("x");
    el("length").value = rounded(c.end - c.start);
    el("fps").value = Number((c.frames / (c.end - c.start)).toFixed(3));
    el("protect").checked = !!c.protect;
    (c.protect || [0, 0, 0.5, 1]).forEach((n, i) => {
      el(["left", "top", "right", "bottom"][i]).value = rounded(n * 100);
    });
    el("mask").hidden = c.strategy !== "align";
    el("cursor").max = source.duration;
    el("undo").disabled = !past.length;
    el("redo").disabled = !future.length;
    el("off").hidden = !activeKey();
    budget(c);
    markers();
  }
  function budget(c) {
    const size = sys.animated_sizes.find((s) => s.size.join("x") === c.size.join("x"));
    const wait = (c.end - c.start) / c.frames,
      bytes = size.frame_bytes * c.frames;
    draftValid =
      Number.isInteger(c.frames) &&
      c.frames >= 3 &&
      c.frames <= size.max_frames &&
      c.start >= 0 &&
      c.end <= source.duration + 0.000001 &&
      wait >= 0.02 &&
      wait <= 5 &&
      (!c.protect ||
        (c.protect.every((n) => n >= 0 && n <= 1) &&
          c.protect[0] < c.protect[2] &&
          c.protect[1] < c.protect[3]));
    el("budget-bar").value = (100 * bytes) / sys.limit_bytes;
    el("budget").textContent =
      `${draftValid ? "Dentro do limite" : "Ajuste os valores"} · ${c.frames}/${size.max_frames} quadros nesta resolução · ${fmt(bytes, 0)} B estimados (${fmt((100 * bytes) / sys.limit_bytes, 1)}%) · ${fmt(1 / wait)} FPS · ${fmt(wait, 5)} s por quadro.`;
    if (report) el("budget").textContent += ` Último RAF medido · ${fmt(report.ddsBytes, 0)} B.`;
    el("render").disabled = !draftValid || pending;
    return draftValid;
  }
  function edit(id) {
    const next = structuredClone(state || initial()),
      c = next.clip;
    c.start = el("start").valueAsNumber;
    c.end = el("end").valueAsNumber;
    c.frames = el("frames").valueAsNumber;
    c.size = el("size").value.split("x").map(Number);
    c.strategy = el("strategy").value;
    c.profile = el("profile").value;
    c.protect =
      c.strategy === "align" && el("protect").checked
        ? ["left", "top", "right", "bottom"].map((k) => el(k).valueAsNumber / 100)
        : null;
    if (id === "length") {
      c.end = rounded(c.start + el("length").valueAsNumber);
      el("end").value = c.end;
    }
    if (id === "fps") {
      c.frames = Math.round((c.end - c.start) * el("fps").valueAsNumber);
      el("frames").value = c.frames;
    }
    if (id === "profile" && c.profile !== "custom") {
      const alternative = analysis?.candidates
        .flatMap((k) => k.alternatives)
        .find((a) => a.start === c.start && a.end === c.end && a.profile === c.profile);
      let [size, rate] = { smooth: [[640, 360], 24], cinema: [[1280, 720], 12], long: [[960, 540], 12] }[
        c.profile
      ];
      if (c.profile === "long" && (c.end - c.start) * Math.min(rate, analysis?.source.fps || rate) > 24)
        size = [640, 360];
      const cap = sys.animated_sizes.find((s) => s.size[0] === size[0]).max_frames;
      c.size = alternative?.size || size;
      c.frames =
        alternative?.frames ||
        Math.max(
          3,
          Math.min(cap, Math.round((c.end - c.start) * Math.min(rate, analysis?.source.fps || rate))),
        );
    }
    if (["size", "fps", "frames"].includes(id)) c.profile = "custom";
    if (!budget(c)) {
      status("Confira os pontos, a região protegida e o limite de quadros.");
      el("apply").disabled = true;
      return;
    }
    captureChange(next);
    renderSettings();
    presentReport();
  }
  function history(from, to) {
    if (!from.length) return;
    to.push(structuredClone(state));
    captureChange(from.pop(), false);
    renderSettings();
    hydrate();
  }
  function markers() {
    const c = (state || initial()).clip,
      cursor = el("cursor").valueAsNumber;
    el("time").textContent = `${fmt(cursor)} s`;
    el("selection").style.left = `${(100 * c.start) / source.duration}%`;
    el("selection").style.width = `${(100 * (c.end - c.start)) / source.duration}%`;
    el("playhead").style.left = `${(100 * cursor) / source.duration}%`;
    for (const b of el("strip").children) {
      const t = Number(b.dataset.t);
      b.dataset.in = String(t >= c.start && t < c.end);
      b.setAttribute("aria-current", String(Math.abs(t - cursor) < 0.002));
    }
    for (const b of el("candidates").children) {
      b.setAttribute(
        "aria-pressed",
        String(Number(b.dataset.start) === c.start && Number(b.dataset.end) === c.end),
      );
    }
  }
  function drawAnalysis() {
    el("strip").replaceChildren();
    el("candidates").replaceChildren();
    el("points").replaceChildren();
    if (!analysis) return;
    for (const mark of analysis.marks) {
      const b = document.createElement("button"),
        img = document.createElement("img");
      img.src = fileUrl(analysis.key, mark.file);
      img.loading = "lazy";
      img.alt = "";
      b.dataset.t = mark.t;
      b.append(img, `${fmt(mark.t, 2)} s · Δ${fmt(mark.motion, 0)}${mark.cut ? " · corte" : ""}`);
      b.onclick = () => {
        el("cursor").value = mark.t;
        scrub();
      };
      el("strip").append(b);
    }
    for (const [i, item] of analysis.candidates.entries()) {
      const b = document.createElement("button"),
        c = item.clip;
      b.dataset.start = c.start;
      b.dataset.end = c.end;
      const point = document.createElement("i");
      point.style.left = `${(100 * c.start) / source.duration}%`;
      el("points").append(point);
      b.textContent = `${i + 1} · ${fmt(c.start)} → ${fmt(c.end)} s · fonte ${words[item.quality.status].toLowerCase()} · DDS de análise ${words[item.proxyDds.status].toLowerCase()} · ${fmt(1 / ((c.end - c.start) / c.frames), 1)} FPS`;
      const flags = [...new Set([...item.quality.flags, ...item.proxyDds.flags])];
      if (flags.length) b.append(document.createElement("br"), flags.map((f) => words[f]).join(", "));
      b.onclick = () => {
        const next = structuredClone(state || initial());
        next.clip = { ...c, strategy: next.clip.strategy, protect: next.clip.protect };
        captureChange(next);
        renderSettings();
        presentReport();
      };
      el("candidates").append(b);
    }
    markers();
  }
  function scrub() {
    const t = el("cursor").valueAsNumber;
    markers();
    if (el("view").value === "original") {
      if (Number.isFinite(el("original").duration)) el("original").currentTime = t;
    } else if (images.length) {
      stop();
      index = Math.max(0, Math.min(images.length - 1, Math.floor((t - report.clip.start) / report.wait)));
      draw();
    }
  }
  function draw() {
    if (!images[index]) return;
    const cv = el("canvas"),
      img = images[index];
    if (cv.width !== img.width || cv.height !== img.height) {
      cv.width = img.width;
      cv.height = img.height;
    }
    cv.getContext("2d").drawImage(img, 0, 0);
    el("playhead").style.left = `${(100 * (report.clip.start + index * report.wait)) / source.duration}%`;
    el("position").textContent =
      `Quadro ${index + 1}/${images.length} · ${fmt(report.clip.start + index * report.wait)} s`;
    el("turns").textContent = `${turns} voltas`;
    el("turns").hidden = !el("count").checked;
  }
  function play(atSeam = false) {
    if (!images.length) return;
    stop();
    seamOnly = atSeam;
    const n = images.length;
    if (atSeam) index = Math.max(0, n - 2);
    initialIndex = index;
    startClock = performance.now();
    turns = 0;
    el("play").textContent = "Pausar";
    // Index by the monotonic clock. No seek, fetch or timer at the wrap.
    function tick(now) {
      const steps = Math.floor(((now - startClock) * Number(el("speed").value)) / (1000 * report.wait));
      if (!el("repeat").checked && initialIndex + steps >= n && !seamOnly) {
        index = n - 1;
        draw();
        stop();
        return;
      }
      if (seamOnly) {
        const strip = n > 3 ? [n - 2, n - 1, 0, 1] : [n - 1, 0, 1];
        index = strip[steps % strip.length];
        turns = Math.floor(steps / strip.length);
      } else {
        index = (initialIndex + steps) % n;
        turns = Math.floor((initialIndex + steps) / n);
      }
      draw();
      playing = requestAnimationFrame(tick);
    }
    playing = requestAnimationFrame(tick);
  }
  async function fetchFrames(key, variant, n, isCurrent = () => true) {
    const list = [];
    try {
      for (let i = 0; i < n; i++) {
        if (!isCurrent()) throw new Error("Visualização substituída.");
        const response = await fetch(fileUrl(key, fileName(variant, i)));
        if (!response.ok) throw new Error("Quadro indisponível. Renderize o trecho novamente.");
        list.push(await createImageBitmap(await response.blob()));
      }
      return list;
    } catch (e) {
      list.forEach((f) => f.close());
      throw e;
    }
  }
  async function showView() {
    const variant = el("view").value,
      original = variant === "original",
      gif = /\.(gif|webp|apng)$/i.test(source?.name || "");
    el("canvas").hidden = original;
    el("original").hidden = !original || gif;
    el("gif").hidden = !original || !gif;
    el("transport").hidden = original;
    el("placeholder").hidden = original || !!report;
    el("original").pause();
    if (original) {
      release();
      const player = el(gif ? "gif" : "original"),
        url = Projects.mediaUrl(`/api/motion/original/${source.job}`);
      if (player.getAttribute("src") !== url) player.src = url;
      return;
    }
    if (!report) {
      release();
      return;
    }
    const key = `${report.key}/${variant}`;
    if (loadedKey === key) {
      draw();
      return;
    }
    release();
    const version = loadVersion;
    try {
      const frames = await fetchFrames(report.key, variant, report.frames, () => version === loadVersion);
      if (version !== loadVersion) {
        frames.forEach((f) => f.close());
        return;
      }
      loadedKey = key;
      images = frames;
      index = 0;
      turns = 0;
      draw();
    } catch (e) {
      if (version === loadVersion) status(e.message);
    }
  }
  function matchesDraft() {
    if (!draftValid || !report || !state) return false;
    const a = state.clip,
      b = report.clip;
    return (
      ["start", "end", "frames", "profile", "strategy"].every((k) => a[k] === b[k]) &&
      a.size.join() === b.size.join() &&
      JSON.stringify(a.protect || null) === JSON.stringify(b.protect || null)
    );
  }
  function presentReport() {
    el("report").hidden = !report;
    if (!report) return;
    const q = report.quality;
    el("verdict").textContent =
      `${report.eligible ? words[q.raf.status] : "Inadequado para aplicar"} · ${report.refined ? "Alinhamento melhorou as medições antes e depois do DDS." : "Corte original preservado."} ${fmt(report.ddsBytes, 0)} B de DDS reais · ${fmt(report.duration)} s · ${fmt(1 / report.wait, 2)} FPS. ${matchesDraft() ? "" : "Este resultado pertence ao ajuste anterior."}`;
    el("measures").replaceChildren();
    for (const [k, label] of [
      ["source", "Fonte amostrada"],
      ["cut", "Corte com camadas"],
      ["optimized", "Loop otimizado"],
      ["raf", "RAF decodificado"],
    ]) {
      const m = q[k],
        p = document.createElement("p"),
        strong = document.createElement("strong");
      strong.textContent = label;
      p.append(
        strong,
        document.createElement("br"),
        `Diferença na junção ${fmt(m.seam)} · interna ${fmt(m.internal)}. Direção oposta ${fmt(100 * m.reverse, 1)}%. ${m.flags.map((f) => words[f]).join(", ") || words[m.status]}.`,
      );
      el("measures").append(p);
    }
    const motion = q.source;
    const direction = document.createElement("p");
    direction.textContent = `Região em movimento ${fmt(100 * motion.moving, 1)}% · ${motion.directionDegrees === null ? "sem direção dominante confirmada" : `direção dominante ${fmt(motion.directionDegrees, 0)}° (direita = 0°, baixo = 90°)`}. Variação de velocidade na junção ${fmt(100 * q.raf.speed, 1)}%.`;
    el("measures").append(direction);
    if (report.recurrence) {
      const note = document.createElement("p");
      note.textContent = report.recurrence.supported
        ? `Textura recorrente · ${fmt(100 * report.recurrence.tileAgreement, 0)}% das regiões móveis repetiram o padrão no ciclo vizinho.`
        : "Textura sem recorrência comprovada em um ciclo vizinho. Experimente outro trecho ou o corte otimizado.";
      el("measures").append(note);
    }
    const p = document.createElement("p");
    p.textContent = `DDS · erro RGB ${fmt(report.compression.rgbError)} de 255 · nitidez relativa ${fmt(100 * report.compression.sharpnessRatio, 1)}%. Bordas próximas acrescentadas ${fmt(100 * report.compression.nearbyEdges, 1)}% · gradientes achatados ${fmt(100 * report.compression.gradientPlateaus, 1)}%. Inspecione fantasmas e faixas na prévia.`;
    el("measures").append(p);
    el("apply").disabled = pending || !report.eligible || !matchesDraft();
    el("download").hidden = !report.eligible;
    el("download").href = fileUrl(report.key, "background.raf.zip");
  }
  async function hydrate() {
    if (!source) return;
    const wanted = state,
      sig = [source.job, state?.analysis, state?.rendered].join("/");
    if (savedReport !== sig) {
      try {
        const [a, r] = await Promise.all([
          wanted?.analysis ? request(endpoint(wanted.analysis)) : null,
          wanted?.rendered ? request(endpoint(wanted.rendered)) : null,
        ]);
        if (wanted !== state) return;
        analysis = a;
        report = r;
        savedReport = sig;
        drawAnalysis();
      } catch (e) {
        status(e.message);
      }
    }
    presentReport();
    if (tab("motion").classList.contains("is-active")) await showView();
  }
  async function activate() {
    el("editor").hidden = !(Projects.id && source);
    el("source").textContent =
      source && Projects.id
        ? `${source.name} · ${source.width} × ${source.height} · ${source.codec} · ${fmt(source.duration)} s`
        : "Abra um projeto e escolha um fundo animado.";
    if (!Projects.id || !source) return;
    renderSettings();
    await hydrate();
  }
  async function operate(kind) {
    if (pending || !source || (kind === "render" && !draftValid)) return;
    const wanted = structuredClone(state || initial()),
      id = source.job;
    pending = true;
    stop();
    el("progress").value = 0;
    el("progress").hidden = false;
    el("analyze").disabled = true;
    el("render").disabled = true;
    status("Preparando a mídia…");
    try {
      let task = await request(
        `/api/motion/${kind}`,
        kind === "analyze"
          ? { job: id, profile: wanted.clip.profile, strategy: wanted.clip.strategy }
          : { job: id, clip: wanted.clip, surface: currentSurface() },
      );
      job = task.id;
      el("cancel").hidden = false;
      while (task.state === "working") {
        status(task.stage);
        el("progress").value = task.percent;
        await new Promise((r) => setTimeout(r, 240));
        task = await request(`/api/motion/jobs/${job}`);
      }
      if (task.state !== "ready") throw new Error(task.stage);
      if (source?.job !== id) return;
      const result = await request(endpoint(task.key)),
        next = structuredClone(state || wanted);
      if (kind === "analyze") {
        next.analysis = task.key;
        analysis = result;
        drawAnalysis();
        const guidance = result.candidates.length
          ? "Toque em um candidato para conferir a junção na resolução final."
          : result.strategy === "texture"
            ? "A amostra não mostrou um período repetido nas regiões móveis. Experimente o corte otimizado ou marque outro intervalo."
            : "Nenhum trecho passou pela busca inicial. Marque a entrada e a saída manualmente.";
        status(
          `${result.candidates.length} candidatos · ${result.windows.length} janela(s) · fonte ${fmt(result.source.sampleFps || result.source.fps)} FPS medidos · ${result.source.irregular ? "intervalos variáveis" : "intervalos regulares na amostra"}. ${guidance}`,
        );
      } else {
        next.rendered = task.key;
        report = result;
        el("view").value = "raf";
        status(`Verificação concluída em ${fmt(result.seconds)} s.`);
      }
      captureChange(next);
      renderSettings();
      await hydrate();
    } catch (e) {
      status(e.message);
    } finally {
      job = null;
      pending = false;
      el("cancel").hidden = true;
      el("progress").hidden = true;
      el("analyze").disabled = false;
      el("render").disabled = !draftValid;
      presentReport();
    }
  }
  async function verified(key) {
    return request("/api/motion/view", { job: anim.jobId, key, surface: currentSurface() });
  }
  async function previewFrames(signal) {
    const key = activeKey(),
      r = await verified(key);
    previewWait = r.wait;
    appliedInfo = r;
    return fetchFrames(key, "raf", r.frames, () => !signal.aborted && key === activeKey());
  }
  function base(redraw) {
    const key = activeKey();
    if (!key) return null;
    if (baseKey !== key) {
      baseKey = key;
      baseImage = new Image();
      baseImage.onload = redraw;
      baseImage.src = fileUrl(key, "base.webp");
    }
    return baseImage?.naturalWidth ? baseImage : null;
  }
  function leave() {
    release();
    el("original").pause();
  }
  function boot() {
    const key = activeKey();
    if (key)
      request(endpoint(key))
        .then((result) => {
          if (activeKey() === key) {
            appliedInfo = result;
            refreshChecklist();
          }
        })
        .catch((e) => status(e.message));
    for (const s of sys.animated_sizes) el("size").add(new Option(s.size.join(" × "), s.size.join("x")));
    el("choose").onclick = () => {
      tab("backgrounds").click();
      document.getElementById("bgmode-animated").click();
    };
    for (const id of [
      "profile",
      "size",
      "start",
      "end",
      "length",
      "frames",
      "fps",
      "strategy",
      "protect",
      "left",
      "top",
      "right",
      "bottom",
    ])
      el(id).onchange = () => edit(id);
    el("analyze").onclick = () => operate("analyze");
    el("render").onclick = () => operate("render");
    el("cancel").onclick = async () => {
      if (job)
        try {
          await request(`/api/motion/jobs/${job}/cancel`, {});
        } catch (e) {
          status(e.message);
        }
    };
    el("mark-in").onclick = () => {
      el("start").value = el("cursor").value;
      edit("start");
    };
    el("mark-out").onclick = () => {
      el("end").value = el("cursor").value;
      edit("end");
    };
    el("cursor").oninput = scrub;
    el("zoom").onchange = () => el("strip").style.setProperty("--thumb", `${84 * Number(el("zoom").value)}px`);
    el("undo").onclick = () => history(past, future);
    el("redo").onclick = () => history(future, past);
    el("play").onclick = () => (playing ? stop() : play());
    el("seam").onclick = () => play(true);
    for (const [button, step] of [
      ["prev", -1],
      ["next", 1],
    ])
      el(button).onclick = () => {
        if (images.length) {
          stop();
          index = (index + step + images.length) % images.length;
          draw();
        }
      };
    el("count").onchange = draw;
    el("view").onchange = showView;
    el("speed").onchange = () => {
      if (playing) play(seamOnly);
    };
    el("apply").onclick = async () => {
      try {
        const r = await verified(report.key);
        const next = structuredClone(state);
        next.applied = r.key;
        appliedInfo = r;
        captureChange(next);
        document.getElementById("bgmode-animated").click();
        status("Loop aplicado à prévia PS4 e à exportação do tema.");
      } catch (e) {
        status(e.message);
      }
    };
    el("off").onclick = () => {
      const next = structuredClone(state);
      next.applied = null;
      captureChange(next);
      status("Animação da fonte inteira restaurada.");
    };
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) leave();
    });
    el("original").onerror = () =>
      status("O navegador não conseguiu reproduzir este original. A análise usa o decodificador do servidor.");
  }
  function description() {
    if (!activeKey()) return null;
    return appliedInfo?.key === activeKey()
      ? `${appliedInfo.frames} quadros · ${fmt(appliedInfo.wait, 5)} s · laboratório`
      : "Loop aplicado pelo laboratório";
  }
  return {
    boot,
    activate,
    leave,
    capture,
    restore,
    sourceChanged,
    activeKey,
    previewFrames,
    base,
    description,
    wait: () => previewWait,
  };
})();
