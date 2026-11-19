import { $, body, button, dialog, field, hint, node, row, stack, symbol, toast } from "./ui.js";
import { dateLabel, number, words } from "./i18n.js";
import { budget, bytes, clamp, orderedFrames, schedule, seconds, slotName, visual } from "./model.js";
import { connect, request, upload, urlFor } from "./client.js";
import { contrast, drawOverlay, drawStage, findLoop, loopComparison, setupDrag } from "./preview.js";
import { assetList, renderPanel, reportView } from "./panels.js";

const memory = "czar.current-project";
const tools = { project: "folder", background: "image", animation: "film", layers: "layers", icons: "grid",
  audio: "audio", colors: "colors", preview: "eye", validate: "check", export: "download" };
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const px = (p) => Number(p.toFixed(6));
const timestamp = (time) => `${String(Math.floor(time / 60)).padStart(2, "0")}:${(time % 60).toFixed(2).padStart(5, "0")}`;

const studio = {
  setup: null, project: null, panel: "background", backgroundSlot: "home", iconSlot: "fn:community", audioSlot: "bgm",
  layerIndex: 0, view: "home", advanced: false, overlay: false, dirty: false, time: 0, layerTime: 0, frameIndex: 0,
  manualIndex: 0, loop: true, playing: false, audioLoop: true, history: [], future: [],
  busy: null, result: null, saving: null, timer: null, mediaAction: null, audioChannels: { bgm: { serial: 0 }, sfx: { serial: 0 } },
  dialog,
  base(ident = this.project.id) { return "/projects/" + ident; },
  assetUrl(id, suffix = "content", ident = this.project.id) { return urlFor(this.base(ident) + `/assets/${id}/${suffix}`); },
  frameUrl(asset, time) { return this.assetUrl(asset.id, "frame") + `?t=${px(clamp(time, 0, Math.max(0, asset.duration - .000001)))}`; },
  jobUrl(id) { return urlFor(this.base() + "/jobs/" + id); },
  rafBudget() { return budget(this.project.settings.animation, this.setup); },
  source() { return this.project.assets[this.project.settings.animation.source?.asset]; },
  times() { return schedule(this.project.settings.animation, this.source()?.duration ?? 0); },
  draw() { return drawStage(this); },
  renderPanel() { renderPanel(this); },
  openPanel(panel) {
    if (!this.project) return;
    this.panel = panel;
    $("inspector").classList.add("open");
    this.renderNavigation();
    this.renderPanel();
    this.draw();
  },
  setView(view) {
    this.view = view;
    this.selectedPreview = view === "function" ? "fn:community" : "ct:disc";
    document.querySelectorAll("[data-view]").forEach((button) => button.setAttribute("aria-pressed", button.dataset.view === view));
    if (this.project) { drawOverlay(this); this.draw(); }
  },
  toggleOverlay() {
    this.overlay = !this.overlay;
    $("preview-mode").setAttribute("aria-pressed", this.overlay);
    drawOverlay(this);
    if (this.overlay) this.draw();
    if (this.panel === "preview") this.renderPanel();
  },
  testContrast() {
    if (!this.overlay) this.toggleOverlay();
    else contrast(this);
  },
  renderNavigation() {
    $("tools").querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", item.dataset.panel === this.panel));
  },
  editTarget() {
    const settings = this.project.settings;
    if (this.panel === "layers") {
      const i = this.layerIndex;
      return settings.layers[i] && !settings.layers[i].locked ? (s) => s.layers[i].transform : null;
    }
    if (this.panel === "animation" && settings.animation.source) return (s) => s.animation.source.transform;
    const slot = this.backgroundSlot;
    return this.panel === "background" && settings.background[slot] &&
      (settings.mode === "static" || this.view === "apply") ? (s) => s.background[slot].transform : null;
  },
  async save() {
    clearTimeout(this.timer);
    if (this.saving) {
      const ok = await this.saving;
      return ok && this.dirty ? this.save() : ok;
    }
    if (!this.project || !this.dirty) return true;
    const project = this.project;
    const edit = structuredClone(project.settings);
    this.dirty = false;
    $("save-state").textContent = "Salvando…";
    this.saving = request(this.base(), { method: "PUT", json: { settings: edit, revision: project.revision } })
      .then((saved) => {
        project.revision = saved.revision;
        project.updatedAt = saved.updatedAt;
        project.thumbnail = saved.thumbnail;
        $("save-state").textContent = this.dirty ? "Salvando…" : "Salvo";
        return true;
      }).catch((error) => {
        this.dirty = true;
        $("save-state").textContent = "Erro ao salvar";
        toast(error.message, true);
        return false;
      }).finally(() => { this.saving = null; });
    const ok = await this.saving;
    if (ok && this.dirty) return this.save();
    return ok;
  },
  dirtyState() {
    this.dirty = true;
    $("save-state").textContent = "Salvando…";
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.save(), 550);
    this.update();
  },
  changed(previous) {
    if (JSON.stringify(previous) === JSON.stringify(this.project.settings)) return;
    this.history = [...this.history.slice(-49), previous];
    this.future = [];
    this.dirtyState();
  },
  apply(mutation) {
    const previous = structuredClone(this.project.settings);
    mutation(this.project.settings);
    this.changed(previous);
  },
  undo(redo = false) {
    const from = redo ? this.future : this.history;
    const to = redo ? this.history : this.future;
    if (!from.length) return;
    to.push(this.project.settings);
    this.project.settings = from.pop();
    this.dirtyState();
  },
  update() {
    const settings = this.project.settings;
    if (settings.mode === "animated" && this.backgroundSlot === "function") this.backgroundSlot = "home";
    $("project-title").textContent = settings.name;
    $("undo").disabled = !this.history.length;
    $("redo").disabled = !this.future.length;
    const b = this.rafBudget();
    const report = this.result?.report;
    const actual = report?.revision === this.project.revision && !this.dirty ? report.raf?.ddsBytes : null;
    $("raf-summary").replaceChildren(
      node("span", { class: `badge ${b.invalid ? "error" : b.near ? "warning" : ""}` },
        settings.mode === "static" ? "Estático" : b.invalid ? "Inválido" : b.near ? "Próximo do limite" : "Seguro"),
      ...(settings.mode === "animated" ? [
        node("span", {}, `${b.preset.width} × ${b.preset.height}`),
        node("span", {}, `${b.frames}/48 quadros · ${number(b.fps)} FPS · ${seconds(b.duration)}`),
        node("span", {}, `DDS ${bytes(b.total)} estimados · ${number(b.percent, 1)}% de 6 MiB`),
        node("span", {}, actual ? `${bytes(actual)} reais` : "DDS reais após geração")
      ] : []));
    const used = Object.values(this.project.assets).reduce((sum, item) => sum + item.bytes, 0);
    $("quota").textContent = `${bytes(used)} / ${bytes(this.setup.projectQuota)}`;
    this.renderPanel();
    this.renderTimeline();
    drawOverlay(this);
    this.draw();
  },
  async showProjects() {
    if (this.busy) return toast("Finalize ou cancele a tarefa antes de trocar de projeto.", true);
    if (!(await this.save())) return;
    this.stopPlayback();
    this.stopSound();
    this.project = null;
    this.result = null;
    localStorage.removeItem(memory);
    $("launcher").hidden = false;
    $("editor").hidden = true;
    $("project-heading").hidden = true;
    $("header-actions").hidden = true;
    await this.refreshRecent();
  },
  async refreshRecent() {
    const recent = await request("/projects");
    $("recents").replaceChildren(...(recent.length ? recent.map((project) => button([
      project.thumbnail ? node("img", { src: this.assetUrl(project.thumbnail, "preview", project.id), alt: "" }) : symbol("folder"),
      node("span", {}, project.name, node("small", {}, dateLabel(project.updatedAt)))], () => this.open(project.id), { class: "recent-card" }))
      : [hint("Os temas que você criar ou importar aparecem aqui.")]));
  },
  async open(ident) {
    try {
      if (!(await this.save())) return;
      this.activate(await request(this.base(ident)));
    } catch (error) { toast(error.message, true); }
  },
  activate(project) {
    this.stopPlayback();
    this.stopSound();
    this.project = project;
    this.result = null; this.dirty = false; this.history = []; this.future = [];
    this.backgroundSlot = "home"; this.frameIndex = 0; this.manualIndex = 0; this.layerIndex = 0;
    this.time = project.settings.animation.start; this.layerTime = 0;
    this.panel = project.settings.mode === "animated" ? "animation" : "background";
    this.overlay = false;
    $("preview-mode").setAttribute("aria-pressed", "false");
    $("inspector").classList.remove("open");
    localStorage.setItem(memory, project.id);
    $("launcher").hidden = true;
    $("editor").hidden = false;
    $("project-heading").hidden = false;
    $("header-actions").hidden = false;
    $("save-state").textContent = "Salvo";
    this.renderNavigation();
    this.setView("home");
    this.update();
  },
  nameDialog(title, value, submit, chooseMode = false) {
    const input = node("input", { required: true, value, maxLength: 127, placeholder: "Nome do tema" });
    const select = node("select", {}, node("option", { value: "static" }, "Estático"),
      node("option", { value: "animated" }, "Animado (RAF)"));
    const form = node("form", {}, node("label", { class: "field" }, node("span", {}, "Nome do projeto"), input),
      chooseMode ? node("label", { class: "field" }, node("span", {}, "Tipo de fundo"), select) : null,
      node("button", { class: "primary", type: "submit" }, "Confirmar"));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!input.value.trim()) return toast("Informe o nome do tema.", true);
      const action = form.querySelector("button");
      action.disabled = true;
      try {
        await submit(input.value.trim(), select.value);
        $("dialog").close();
      } catch (error) {
        form.append(hint(error.message));
        action.disabled = false;
      }
    });
    dialog(title, form);
    input.focus();
  },
  createProject() {
    this.nameDialog("Novo tema", "", async (name, mode) => this.activate(
      await request("/projects", { method: "POST", json: { name, mode } })), true);
  },
  copyProject(duplicate) {
    if (this.busy) return toast("Aguarde a tarefa atual.", true);
    this.nameDialog(duplicate ? "Duplicar tema" : "Salvar como", this.project.settings.name + " — cópia", async (name) => {
      if (!(await this.save())) throw new Error("Salve as alterações antes de copiar.");
      const copy = await request(this.base() + "/duplicate", { method: "POST", json: { name } });
      this.activate(copy);
    });
  },
  deleteProject() {
    dialog("Excluir projeto", stack(hint(`Excluir “${this.project.settings.name}” e seus arquivos deste servidor?`),
      button("Excluir projeto", async () => {
        try {
          clearTimeout(this.timer);
          if (this.saving) await this.saving;
          await request(this.base(), { method: "DELETE" });
          this.dirty = false;
          $("dialog").close();
          await this.showProjects();
        } catch (error) { toast(error.message, true); }
      }, { class: "danger" })));
  },
  pickMedia(handler = null) {
    this.mediaAction = handler;
    $("media-picker").click();
  },
  async importMedia(files) {
    const current = this.project;
    if (!current || !files.length) return;
    const onChoose = this.mediaAction;
    const list = stack();
    dialog("Importar mídia", list);
    let first = null;
    let active;
    let stopped = false;
    const cancel = button("Pausar importação", () => { stopped = true; active?.cancel(); cancel.disabled = true; });
    list.append(cancel);
    for (const file of files) {
      if (stopped || this.project !== current) break;
      const meter = node("progress", { max: 100, value: 0, "aria-label": `Envio de ${file.name}` });
      const status = hint("Enviando…");
      list.insertBefore(stack(node("strong", {}, file.name), meter, status), cancel);
      try {
        active = upload(this.base() + "/assets", file, (fraction) => {
          meter.value = fraction * 100;
          if (fraction === 1) status.textContent = "Verificando a mídia…";
        });
        const asset = await active.promise;
        current.assets[asset.id] = asset;
        first ||= asset;
        meter.value = 100;
        status.textContent = "Importado";
      } catch (error) { status.textContent = error.message; status.classList.add("error"); }
    }
    cancel.replaceWith(button("Concluir", () => $("dialog").close(), { class: "primary" }));
    if (this.project !== current) return;
    this.update();
    if (first) {
      if (onChoose) { onChoose(first); $("dialog").close(); }
      else if (files.length === 1 && ["video", "animation"].includes(first.kind)) {
        this.assignAnimation(first);
        $("dialog").close();
      }
    }
  },
  chooseAsset(kinds, action) {
    const assets = Object.values(this.project.assets).filter((file) => kinds.includes(file.kind));
    dialog("Escolher mídia", stack(assetList(this, assets, (asset) => { $("dialog").close(); action(asset); }),
      button("Importar mídia", () => { $("dialog").close(); this.pickMedia(action); }, { icon: "upload" })));
  },
  openAsset(asset) {
    const image = ["image", "animation"].includes(asset.kind);
    const animate = ["animation", "video"].includes(asset.kind);
    dialog(asset.name, stack(hint(`${asset.codec} · ${bytes(asset.bytes)}`),
      image ? button("Usar no fundo inicial", () => {
        this.assignVisual("background", "home", asset); $("dialog").close(); this.openPanel("background");
      }) : null,
      animate ? button("Usar na animação RAF", () => { this.assignAnimation(asset); $("dialog").close(); }) : null,
      !image && !animate ? hint("Escolha o arquivo no slot desejado do painel Áudio.") : null));
  },
  assignVisual(group, slot, asset) {
    if (!["image", "animation"].includes(asset.kind)) return toast("Este slot aceita imagens.", true);
    this.apply((settings) => { settings[group][slot] = visual(asset.id); });
    if (group === "background" && slot === "home" && this.project.settings.mode === "animated") this.setView("apply");
  },
  assignAnimation(asset) {
    if (!["video", "animation"].includes(asset.kind)) return toast("Escolha um GIF animado ou vídeo.", true);
    this.stopPlayback();
    this.time = 0; this.layerTime = 0; this.frameIndex = 0;
    this.apply((s) => {
      Object.assign(s.animation, { source: visual(asset.id), start: 0, end: null, timeline: [], sampling: "uniform", first: 0 });
      s.mode = "animated";
    });
    this.setView("home");
    this.openPanel("animation");
  },
  async importProject(file) {
    if (!file) return;
    const meter = node("progress", { max: 100, value: 0, "aria-label": "Progresso da importação" });
    const status = hint("Enviando projeto…");
    const pending = upload("/projects/import", file, (fraction) => {
      meter.value = fraction * 100;
      if (fraction === 1) status.textContent = "Validando ZIP e mídias…";
    });
    dialog("Importar projeto", stack(status, meter, button("Cancelar", () => pending.cancel())));
    try {
      this.activate(await pending.promise);
      $("dialog").close();
    } catch (error) { status.textContent = error.message; }
  },
  moveLayer(offset) {
    const i = this.layerIndex, j = i + offset;
    this.layerIndex = j;
    this.apply((s) => { [s.layers[i], s.layers[j]] = [s.layers[j], s.layers[i]]; });
  },
  scrub(time, position = 0) {
    this.time = time;
    this.layerTime = position;
    $("timeline-time").textContent = timestamp(time);
    const input = $("playhead");
    if (input) input.value = time;
    return this.draw();
  },
  renderTimeline() {
    const body = $("timeline-body");
    const asset = this.project.settings.mode === "animated" ? this.source() : null;
    const a = this.project.settings.animation;
    const b = this.rafBudget();
    $("play").disabled = !asset;
    $("loop-tool").disabled = !asset;
    if (!asset) {
      body.replaceChildren(hint(this.project.settings.mode === "static" ? "Fundo estático. Adicione uma animação para abrir a timeline." :
        "Importe um GIF animado ou vídeo para escolher os quadros."), ...this.audioLane());
      return;
    }
    const selectedFrames = orderedFrames(a, asset.duration);
    const end = a.end ?? asset.duration;
    const region = node("div", { class: "selected-trim", "aria-hidden": "true" });
    region.style.setProperty("--start", `${100 * a.start / asset.duration}%`);
    region.style.setProperty("--end", `${100 * end / asset.duration}%`);
    const ruler = node("div", { class: "ruler", "aria-hidden": "true" },
      Array.from({ length: 5 }, (_, i) => node("span", {}, seconds(i * asset.duration / 4))));
    const playhead = node("input", { type: "range", id: "playhead", min: 0, max: asset.duration, step: .001,
      value: clamp(this.time, 0, asset.duration), "aria-label": "Playhead em segundos" });
    let scrubTimer;
    playhead.addEventListener("input", () => {
      this.stopPlayback();
      const t = playhead.valueAsNumber;
      this.time = t;
      $("timeline-time").textContent = timestamp(t);
      clearTimeout(scrubTimer);
      scrubTimer = setTimeout(() => this.scrub(t), 55);
    });
    const strip = node("div", { class: "strip", "aria-label": "Quadros selecionados" }, selectedFrames.map(({time, index}, i) =>
      button([node("img", { loading: "lazy", src: this.frameUrl(asset, time), alt: "" }),
        node("span", {}, String(i + 1).padStart(2, "0"))], () => {
        this.stopPlayback(); this.frameIndex = i;
        this.scrub(time, i * b.wait);
        if (a.sampling === "manual") this.manualIndex = index;
        this.renderTimeline();
      }, { class: "frame", "aria-pressed": String(i === this.frameIndex),
        "aria-label": `Quadro ${i + 1}, ${seconds(time)}` })));
    const startEnd = row(hint(`Corte ${seconds(a.start)} – ${seconds(end)}`),
      button("Marcar início", () => this.markTrim("start")),
      button("Marcar fim", () => this.markTrim("end")));
    const advanced = a.sampling === "manual" ? this.manualControls() :
      this.advanced && selectedFrames.length ? button("Definir quadro inicial", () => this.chooseFirst()) : null;
    const scroll = body.querySelector(".strip")?.scrollLeft ?? 0;
    body.replaceChildren(node("div", { class: "timeline-rail" }, region, playhead, ruler), strip, startEnd, advanced, ...this.audioLane());
    strip.scrollLeft = scroll;
    $("timeline-time").textContent = timestamp(this.time);
  },
  audioLane() {
    const slots = Object.keys(this.project.settings.audio);
    if (!slots.length) return [];
    return [node("div", { class: "row audio-lane", "aria-label": "Faixas de áudio" },
      slots.map((slot) => button(slotName(slot, words), () => { this.audioSlot = slot; this.openPanel("audio"); },
        { icon: "audio" })))];
  },
  markTrim(key) {
    const a = this.project.settings.animation, end = a.end ?? this.source().duration;
    if ((key === "start" && this.time >= end) || (key === "end" && this.time <= a.start)) {
      return toast("O trecho precisa ter início antes do fim.", true);
    }
    this.apply((s) => { s.animation[key] = this.time; });
  },
  chooseFirst() {
    const a = this.project.settings.animation, count = this.times().length;
    if (count) this.apply((s) => { s.animation.first = (a.first + this.frameIndex) % count; this.frameIndex = 0; });
  },
  manualControls() {
    const a = this.project.settings.animation, n = a.timeline.length;
    const i = this.manualIndex;
    const edit = (op) => this.apply((s) => { op(s.animation.timeline); });
    return row(button("Adicionar quadro", () => this.manualAdd()),
      button("Duplicar quadro", () => this.manualAdd(a.timeline[i]), { disabled: i < 0 || i >= n }),
      button("Remover quadro", () => edit((frames) => { frames.splice(i, 1); this.manualIndex = Math.max(0, i - 1); }), { disabled: i < 0 || i >= n }),
      button("Mover antes", () => edit((frames) => { [frames[i - 1], frames[i]] = [frames[i], frames[i - 1]]; this.manualIndex--; }), { disabled: i <= 0 || i >= n }),
      button("Mover depois", () => edit((frames) => { [frames[i + 1], frames[i]] = [frames[i], frames[i + 1]]; this.manualIndex++; }), { disabled: i < 0 || i + 1 >= n }),
      button("Definir quadro inicial", () => this.chooseFirst(), { disabled: !n }));
  },
  manualAdd(time = this.time) {
    const a = this.project.settings.animation;
    const b = this.rafBudget();
    const count = a.timeline.length + 1;
    const cap = a.pingPong ? Math.floor((b.preset.max + 2) / 2) : b.preset.max;
    if (count > cap) return toast("Limite RAF para esta resolução atingido.", true);
    if (time < a.start || time >= (a.end ?? this.source().duration)) return toast("Escolha um quadro dentro do corte.", true);
    this.manualIndex = a.timeline.length;
    this.apply((s) => { s.animation.timeline.push(time); });
  },
  stopPlayback() {
    this.playing = false;
    this.playToken = (this.playToken || 0) + 1;
    $("play").replaceChildren(symbol("play"));
    $("play").setAttribute("aria-label", "Reproduzir");
  },
  async togglePlayback() {
    if (this.playing) return this.stopPlayback();
    const times = this.times();
    if (!times.length) return toast("Selecione um trecho e os quadros do loop.", true);
    this.playing = true;
    const token = ++this.playToken;
    $("play").replaceChildren(symbol("pause"));
    $("play").setAttribute("aria-label", "Pausar");
    const { wait: interval } = this.rafBudget();
    let index = this.frameIndex % times.length;
    do {
      const start = performance.now();
      this.frameIndex = index;
      await this.scrub(times[index], index * interval);
      if (token !== this.playToken) return;
      $("timeline-body").querySelectorAll(".frame").forEach((frame, i) => frame.setAttribute("aria-pressed", i === index));
      await wait(Math.max(12, interval * 1000 - (performance.now() - start)));
      index = (index + 1) % times.length;
    } while (this.playing && token === this.playToken && (this.loop || index !== 0));
    if (token === this.playToken) this.stopPlayback();
  },
  openLoop() {
    const times = this.times();
    if (times.length < 2) return toast("Escolha pelo menos dois quadros para comparar.", true);
    const compare = node("div");
    const update = (pair) => {
      const [first, last, overlay, metric] = loopComparison(this, times, pair);
      compare.replaceChildren(node("div", { class: "loop-comparison" }, stack(hint("Primeiro quadro"), first),
        stack(hint("Último quadro"), last), stack(hint("Sobreposição"), overlay)), metric);
    };
    const last = times.at(-1);
    const slider = field("Comparar com outro instante", { type: "range", value: last, min: 0,
      max: this.source().duration, step: .01 }, (v) => update([times[0], v]));
    update();
    const suggestion = stack();
    const suggest = button("Buscar final semelhante", async () => {
      suggest.disabled = true;
      suggestion.replaceChildren(hint("Comparando pontos perto do final…"));
      try {
        const candidate = await findLoop(this, times);
        if (!$("dialog").open || !body.contains(suggestion)) return;
        update([times[0], candidate]);
        suggestion.replaceChildren(hint(`Quadro sugerido em ${seconds(candidate)}.`),
          button("Marcar esse fim", () => {
            const animation = this.project.settings.animation;
            const asset = this.source();
            const offset = asset.fps > 0 ? 1 / asset.fps : .04;
            const end = Math.min(asset.duration, candidate + offset);
            if (end > animation.start) {
              this.apply((s) => { s.animation.end = end; });
              this.scrub(candidate);
              $("dialog").close();
            }
          }));
      } catch (error) { suggestion.replaceChildren(hint("Não foi possível comparar esses instantes.")); }
      finally { suggest.disabled = false; }
    });
    dialog("Criar loop", stack(compare, slider, suggest, suggestion,
      row(button("Testar loop", () => { $("dialog").close(); this.stopPlayback(); this.togglePlayback(); }),
        button("Alternar pingue-pongue", () => {
          this.apply((s) => { s.animation.pingPong = !s.animation.pingPong; }); this.openLoop();
        }))));
  },
  optimizer() {
    const a = this.project.settings.animation;
    const result = node("p");
    let preference = this.setup.presets.findIndex((p) => p.width === a.width);
    const duration = node("input", { type: "number", inputMode: "decimal", min: .04, max: 240, step: "any",
      value: px(this.rafBudget().duration), required: true });
    const model = () => {
      const chosen = this.setup.presets[preference];
      const length = clamp(Number(duration.value) || 1, .04, 240);
      const count = Math.min(chosen.max, Math.max(2, Math.round(length * 24)));
      const interval = clamp(length / count, .02, 5);
      const b = budget({ ...a, width: chosen.width, frames: count, wait: interval, speed: 1, sampling: "uniform" }, this.setup);
      result.textContent = `${chosen.width} × ${chosen.height} · ${count} quadros · ${number(b.fps)} FPS · ${seconds(b.duration)} · ${bytes(b.total)}`;
      return { width: chosen.width, frames: count, wait: interval, speed: 1, sampling: "uniform" };
    };
    duration.addEventListener("input", model);
    const preferenceField = field("Qualidade ←→ Movimento", { type: "range", min: 0, max: 2, step: 1, value: preference },
      (value) => { preference = value; model(); }, (value) => { preference = value; model(); });
    const form = stack(node("label", { class: "field" }, node("span", {}, "Duração desejada (s)"), duration),
      preferenceField, result, button("Aplicar configuração", () => {
        if (!duration.reportValidity()) return;
        this.apply((s) => Object.assign(s.animation, model()));
        $("dialog").close();
      }, { class: "primary" }));
    model();
    dialog("Otimizar RAF", form);
  },
  async openIcon() {
    const key = this.iconSlot, icon = this.project.settings.icons[key];
    const title = slotName(key, words);
    const prep = node("canvas", { width: 128, height: 128, "aria-label": "Preview normal" });
    const glow = node("canvas", { width: 152, height: 152, "aria-label": "Preview glow" });
    const modal = node("div", { class: "loop-comparison icon-previews" }, stack(hint("Normal"), prep), stack(hint("Glow"), glow));
    const light = field("Fundo claro", { type: "checkbox" }, (v) => modal.classList.toggle("light", v));
    dialog(title, stack(modal, light));
    const img = new Image();
    img.src = this.assetUrl(icon.asset, "preview");
    try { await img.decode(); } catch { return; }
    const edit = icon.transform, ctx = prep.getContext("2d");
    const [l, t, r, b] = edit.crop, w = (r - l) * img.width, h = (b - t) * img.height;
    const scale = (edit.fit === "cover" ? Math.max : Math.min)(128 / w, 128 / h) * edit.zoom;
    ctx.translate(128 * (.5 + edit.x), 128 * (.5 + edit.y));
    ctx.rotate(edit.rotation * Math.PI / 180);
    ctx.scale(edit.flipX ? -1 : 1, edit.flipY ? -1 : 1);
    ctx.globalAlpha = edit.opacity;
    ctx.filter = `brightness(${edit.brightness}) contrast(${edit.contrast}) saturate(${edit.saturation})`;
    ctx.drawImage(img, l * img.width, t * img.height, w, h, -w * scale / 2, -h * scale / 2, w * scale, h * scale);
    const glowCtx = glow.getContext("2d");
    if (this.setup.slots[key].glow) { glowCtx.shadowColor = "white"; glowCtx.shadowBlur = icon.glow * 21; }
    glowCtx.drawImage(prep, 12, 12);
  },
  stopSound(channel = null) {
    const target = channel ? [this.audioChannels[channel]] : Object.values(this.audioChannels);
    for (const current of target) {
      current.serial++;
      if (current.sound) {
        current.sound.pause();
        URL.revokeObjectURL(current.sound.src);
        current.sound = null;
      }
    }
  },
  async playSound(slot, instruct = false) {
    const track = this.project.settings.audio[slot];
    if (!track) { if (instruct) toast("Adicione uma faixa de áudio a este slot."); return; }
    const key = slot === "bgm" ? "bgm" : "sfx";
    const channel = this.audioChannels[key];
    this.stopSound(key);
    const generation = channel.serial;
    try {
      const data = await request(this.base() + "/audio-preview/" + slot, { method: "POST", json: track, blob: true });
      if (generation !== channel.serial) return;
      const sound = new Audio(URL.createObjectURL(data));
      channel.sound = sound;
      sound.loop = key === "bgm" && this.audioLoop;
      sound.addEventListener("ended", () => { if (channel.sound === sound) this.stopSound(key); }, { once: true });
      await sound.play();
    } catch (error) {
      if (generation === channel.serial) { this.stopSound(key); toast(error.message, true); }
    }
  },
  async startJob(kind) {
    if (this.busy || this.starting) return;
    this.starting = true;
    try {
      if (!(await this.save())) return;
      const path = this.base() + "/jobs";
      this.result = null;
      const created = await request(path, { method: "POST", json: { kind } });
      this.busy = created;
      this.openPanel(kind === "validate" ? "validate" : "export");
      dialog(kind === "validate" ? "Validar tema" : "Exportar tema", stack(hint("Tarefa iniciada."),
        button("Continuar editando", () => $("dialog").close())));
      const content = body.firstElementChild;
      let failures = 0;
      while (this.busy) {
        await wait(350);
        try {
          const data = await request(path + "/" + created.id);
          failures = 0;
          if (["done", "failed", "cancelled"].includes(data.state)) {
            this.busy = null;
            this.result = data;
            const latest = await request(this.base());
            this.project.exports = latest.exports;
            this.update();
            if ($("dialog").open && body.contains(content)) {
              content.replaceChildren(hint(data.error?.message || data.stage),
                data.download ? node("a", { class: "button primary", href: this.jobUrl(data.id) + "/download" }, "Baixar ZIP") : "",
                data.report ? reportView(this) : "");
            } else toast(data.error?.message || data.stage, data.state === "failed");
            return;
          }
          this.busy = data;
          if ($("dialog").open && body.contains(content)) {
            content.replaceChildren(hint(data.stage), node("progress", { max: 100, value: data.progress,
              "aria-label": "Progresso da geração" }), row(button("Cancelar tarefa", () => this.cancelJob()),
              button("Continuar editando", () => $("dialog").close())));
          }
        } catch (error) {
          if (++failures >= 4) throw error;
        }
      }
    } catch (error) {
      this.busy = null;
      this.renderPanel();
      toast(error.message, true);
    } finally { this.starting = false; }
  },
  async cancelJob() {
    if (!this.busy) return;
    try { await request(this.base() + "/jobs/" + this.busy.id, { method: "DELETE" }); }
    catch (error) { toast(error.message, true); }
  },
  consoleTest() {
    const test = { console: "PS4 Slim", firmware: "12.52", buildId: this.result?.id ?? "", error: "", notes: "" };
    const flags = { installed: "Instalado", listed: "Aparece em Temas", applied: "Aplicado", animation: "RAF",
      music: "Música", sfx: "Efeitos sonoros", icons: "Ícones", reboot: "Reinicialização OK" };
    const form = node("form", {}, ...Object.entries({ console: "Console", firmware: "Firmware", buildId: "Build ID", error: "Código CE" })
      .map(([key, label]) => node("label", { class: "field" }, node("span", {}, label), node("input",
        { value: test[key], name: key, maxLength: { console: 50, firmware: 20, buildId: 64, error: 50 }[key], required: key !== "error" }))),
      ...Object.entries(flags).map(([key, label]) => node("label", { class: "field check" },
        node("input", { type: "checkbox", name: key }), node("span", {}, label))),
      node("label", { class: "field" }, node("span", {}, "Observações"), node("textarea", { name: "notes", maxLength: 2000 })),
      node("button", { type: "submit", class: "primary" }, "Salvar registro"));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (this.project.settings.consoleTests.length >= 50) return toast("Limite de registros atingido.", true);
      Object.keys(test).forEach((key) => { test[key] = form.elements.namedItem(key).value; });
      Object.keys(flags).forEach((key) => { test[key] = form.elements.namedItem(key).checked; });
      this.apply((s) => s.consoleTests.push(test));
      $("dialog").close();
    });
    dialog("Registrar teste físico", form);
  },
  async licenses() {
    try {
      const data = await request("/components");
      dialog("Licenças e componentes", stack(
        node("strong", {}, "Czar PS4 Theme Studio · MIT"),
        hint("Base original PS4 Ultimate Theme Creator, por Imxnxl."),
        node("a", { href: "https://github.com/Imxnxl/PS4-Ultimate-Theme-Creator/blob/main/LICENSE" }, "Licença do projeto de origem"),
        hint("FFmpeg detectado · a licença depende da configuração desta compilação."),
        ...((data.ffmpeg || ["Indisponível"]).map((line) => hint(line))),
        hint("Pillow (HPND), NumPy (BSD-3-Clause), FastAPI e Pydantic (MIT), Uvicorn (BSD-3-Clause), python-multipart (Apache-2.0)."),
        hint("O editor usa ferramentas externas apenas no estágio de empacotamento ou conversão ATRAC9, quando disponíveis ao proprietário.")));
    } catch (error) { toast(error.message, true); }
  }
};

for (const target of document.querySelectorAll("[data-icon]")) target.prepend(symbol(target.dataset.icon));
for (const [panel, icon] of Object.entries(tools)) $("tools").append(button(words.panels[panel], () => studio.openPanel(panel),
  { icon, class: "plain", data: { panel } }));
setupDrag(studio);
$("show-projects").onclick = () => studio.showProjects().catch((error) => toast(error.message, true));
$("create").onclick = () => studio.createProject();
$("open-recent").onclick = () => { $("recent-section").scrollIntoView({ block: "nearest", behavior: "smooth" }); $("recents").querySelector("button")?.focus(); };
$("import-project").onclick = () => $("project-picker").click();
$("project-picker").onchange = (event) => { studio.importProject(event.target.files[0]); event.target.value = ""; };
$("media-picker").onchange = (event) => { studio.importMedia([...event.target.files]); event.target.value = ""; };
$("canvas-import").onclick = () => studio.pickMedia((asset) => {
  if (["video", "animation"].includes(asset.kind)) studio.assignAnimation(asset);
  else studio.assignVisual("background", "home", asset);
});
$("save").onclick = () => studio.save();
$("undo").onclick = () => studio.undo();
$("redo").onclick = () => studio.undo(true);
$("preview-button").onclick = () => studio.openPanel("preview");
$("validate-button").onclick = () => studio.openPanel("validate");
$("export-button").onclick = () => studio.openPanel("export");
$("raf-shortcut").onclick = () => studio.openPanel("animation");
$("close-panel").onclick = () => $("inspector").classList.remove("open");
$("close-dialog").onclick = () => $("dialog").close();
$("preview-mode").onclick = () => studio.toggleOverlay();
$("guide-button").onclick = (event) => event.currentTarget.setAttribute("aria-pressed", $("guides").classList.toggle("on"));
$("play").onclick = () => studio.togglePlayback();
$("loop-play").onclick = (event) => { studio.loop = !studio.loop; event.currentTarget.setAttribute("aria-pressed", studio.loop); };
$("loop-tool").onclick = () => studio.openLoop();
for (const item of document.querySelectorAll("[data-license]")) item.onclick = () => studio.licenses();
for (const item of document.querySelectorAll("[data-view]")) item.onclick = () => studio.setView(item.dataset.view);
window.addEventListener("beforeunload", (event) => {
  if (studio.dirty || studio.saving) { event.preventDefault(); event.returnValue = ""; }
});

try {
  for (const element of ["create", "import-project", "open-recent"]) $(element).disabled = true;
  studio.setup = await connect();
  const previous = localStorage.getItem(memory);
  if (previous) {
    try { studio.activate(await request(studio.base(previous))); }
    catch { localStorage.removeItem(memory); await studio.refreshRecent(); }
  } else await studio.refreshRecent();
  for (const element of ["create", "import-project", "open-recent"]) $(element).disabled = false;
} catch (error) {
  toast(error.message + " Recarregue a página para tentar novamente.", true);
}
