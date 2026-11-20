"use strict";
const by = (id) => document.getElementById(id);
const fmt = (n) => Number(n).toLocaleString("pt-BR", { maximumFractionDigits: 3 });
let analysis,
  ticket,
  busy = false,
  frames = [],
  wait = 0,
  index = 0,
  animation = 0,
  generation = 0;
const params = new URLSearchParams(location.search);
function url(path) {
  const u = new URL(path, location.origin);
  if (by("project").value) u.searchParams.set("project", by("project").value);
  return u;
}
async function request(path, data) {
  const r = await fetch(
    url(path),
    data === undefined
      ? {}
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) },
  );
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
  by("player").hidden = true;
}
function draw() {
  const cv = document.querySelector("canvas"),
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
  const begin = performance.now(),
    first = index,
    n = frames.length;
  by("play").textContent = "Pausar";
  let previous = -1;
  function tick(now) {
    animation = requestAnimationFrame(tick);
    const t = Math.max(0, Math.floor((now - begin) / (wait * 1000)));
    if (t === previous) return;
    previous = t;
    index = seam ? [n - 2, n - 1, 0, 1][t % 4] : (first + t) % n;
    draw();
    by("counter").textContent = seam
      ? "Inspeção da junção"
      : `Quadro ${index + 1}/${n} · ${Math.floor(t / n)} voltas completas`;
  }
  animation = requestAnimationFrame(tick);
}
function selectSource() {
  generation++;
  release();
  analysis = null;
  by("diag").hidden = true;
  by("results").hidden = true;
  by("status").textContent = "";
  by("run").disabled = !by("video").value || !by("project").value;
}
async function sources() {
  selectSource();
  const revision = generation;
  by("video").replaceChildren();
  by("run").disabled = true;
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
  by("run").disabled = !options.length;
  if (!options.length)
    by("status").textContent = "Importe um vídeo na Biblioteca ou selecione um fundo animado no editor.";
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
async function operate(kind, payload) {
  if (busy) return;
  busy = true;
  stop();
  by("project").disabled = by("video").disabled = by("run").disabled = true;
  by("progress").hidden = false;
  by("progress").value = 0;
  try {
    let t = await request("/api/ouroboros/" + kind, payload);
    ticket = t.id;
    by("cancel").hidden = false;
    while (t.state === "working") {
      by("status").textContent = t.stage;
      by("progress").value = t.percent;
      await new Promise((r) => setTimeout(r, 300));
      t = await request("/api/ouroboros/jobs/" + ticket);
    }
    by("cancel").hidden = true;
    if (t.state !== "ready") throw Error(t.stage);
    const r = await request("/api/ouroboros/result/" + t.key);
    if (kind === "analyze") {
      release();
      analysis = r;
      display();
    } else {
      const loaded = [];
      try {
        for (let i = 0; i < r.frames; i++) {
          const res = await fetch(url(`/api/ouroboros/result/${t.key}/${String(i).padStart(2, "0")}.webp`));
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
      const clip = analysis.groups[payload.resolution].candidates[payload.candidate];
      by("measurement").textContent =
        `${r.size.join(" × ")} · ${fmt(clip.start)} → ${fmt(clip.end)} s. Fonte · ${r.sourceVerdict}. DDS · ${r.quality.verdict}. ${r.quality.reason} ${fmt(r.bytes)} bytes reais · espera ${fmt(wait)} s. A prévia mede os DDS. A aplicação no PS4 exige teste físico.`;
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
    by("run").disabled = by("project").disabled = by("video").disabled = false;
  }
}
by("run").onclick = () => {
  const [kind, id] = by("video").value.split("/");
  operate("analyze", { kind, id });
};
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
(async () => {
  const list = await request("/api/projects");
  by("project").replaceChildren(...list.map((p) => new Option(p.name, p.id)));
  if (list.some((p) => p.id === params.get("project"))) by("project").value = params.get("project");
  await sources();
})().catch((e) => (by("status").textContent = e.message));
