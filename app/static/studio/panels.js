import { $, node, button, row, stack, block, field, fields, hint, symbol, toast } from "./ui.js";
import { blankTransform, visual, track, clamp, bytes, seconds, slotName } from "./model.js";
import { words, number } from "./i18n.js";

const numberField = (label, value, min, max, step, change) =>
  field(label, { type: "number", value, min, max, step, inputMode: step === "any" || step < 1 ? "decimal" : "numeric" }, change);
const checkbox = (label, checked, change) => field(label, { type: "checkbox", checked }, change);
const select = (label, value, options, change) => field(label, { value, options }, change);
const range = (label, value, min, max, step, change, preview) =>
  field(label, { type: "range", value, min, max, step }, change, preview);
const names = words.panels;

function mediaPicker(studio, kinds, onChoose, description = "Escolher mídia") {
  return row(button(description, () => studio.chooseAsset(kinds, onChoose), { icon: "folder" }),
    button("Importar", () => studio.pickMedia(onChoose), { icon: "upload" }));
}

export function assetList(studio, assets, selected) {
  if (!assets.length) return hint("Nenhuma mídia compatível neste projeto.");
  return node("div", { class: "asset-list" }, assets.map((asset) => {
    const visual = ["image", "video", "animation"].includes(asset.kind);
    const information = [visual ? `${asset.width} × ${asset.height}` : asset.codec,
      asset.duration ? seconds(asset.duration) : "", bytes(asset.bytes)].filter(Boolean).join(" · ");
    const card = button(node("span", {}, asset.name, node("br"), node("small", {}, information)), () => selected(asset),
      { class: "asset-card", icon: visual ? undefined : "audio" });
    if (visual) card.prepend(node("img", { src: studio.assetUrl(asset.id, "preview"), alt: "", loading: "lazy" }));
    return card;
  }));
}

function transforms(studio, get, { crop = true } = {}) {
  const edit = get(studio.project.settings);
  const modify = (key, value) => studio.apply((settings) => { get(settings)[key] = value; });
  const adjust = (label, key, min, max, step) => range(label, edit[key], min, max, step, (v) => modify(key, v), (v) => {
    const initial = edit[key];
    edit[key] = v;
    studio.draw();
    edit[key] = initial;
  });
  const cropFields = edit.crop.map((value, i) => {
    const min = i < 2 ? 0 : Math.ceil((edit.crop[i - 2] + .02) * 100 - .0001);
    const max = i < 2 ? Math.floor((edit.crop[i + 2] - .02) * 100 + .0001) : 100;
    return numberField(["Esquerda (%)", "Topo (%)", "Direita (%)", "Base (%)"][i],
      Math.round(value * 100), min, max, 1, (v) => {
        const corners = [...edit.crop]; corners[i] = v / 100; modify("crop", corners);
      });
  });
  return stack(select("Enquadramento", edit.fit, [["cover", "Preencher"], ["contain", "Conter"]], (v) => modify("fit", v)),
    adjust("Zoom", "zoom", .25, 4, .05), adjust("Posição horizontal", "x", -1, 1, .01),
    adjust("Posição vertical", "y", -1, 1, .01),
    row(button("Centralizar", () => studio.apply((settings) => { const target = get(settings); target.x = 0; target.y = 0; })),
      button("Restaurar", () => studio.apply((settings) => Object.assign(get(settings), blankTransform())))),
    node("details", { open: studio.advanced }, node("summary", {}, "Recorte e ajustes"), stack(
      crop ? node("fieldset", {}, node("legend", {}, "Recorte da origem"), fields(cropFields)) : null,
      adjust("Rotação", "rotation", -180, 180, 1), adjust("Opacidade", "opacity", 0, 1, .01),
      fields(checkbox("Espelhar X", edit.flipX, (v) => modify("flipX", v)),
        checkbox("Espelhar Y", edit.flipY, (v) => modify("flipY", v))),
      adjust("Brilho", "brightness", 0, 2, .02), adjust("Contraste", "contrast", 0, 2, .02),
      adjust("Saturação", "saturation", 0, 2, .02))));
}

function project(studio) {
  const { settings } = studio.project;
  const all = Object.values(studio.project.assets);
  return stack(
    block("Dados do projeto", field("Nome", { value: settings.name, maxLength: 127 }, (v) => {
      if (v.trim()) studio.apply((s) => { s.name = v.trim(); });
      else toast("Informe o nome do tema.", true);
    }), row(button("Salvar como", () => studio.copyProject(false)), button("Duplicar", () => studio.copyProject(true))),
    button("Excluir projeto", () => studio.deleteProject(), { class: "danger" })),
    block("Modo de edição", select("Controles", studio.advanced ? "advanced" : "simple",
      [["simple", "Simples"], ["advanced", "Avançado"]], (v) => { studio.advanced = v === "advanced"; studio.renderPanel(); }),
    studio.advanced ? stack(field("Title ID", { value: settings.titleId, maxLength: 9 }, (v) => {
      const title = v.toUpperCase();
      if (!/^[A-Z]{4}[0-9]{5}$/.test(title)) return toast("Use um Title ID como CUSA00000.", true);
      studio.apply((s) => { s.titleId = title; });
    }), field("Content ID (opcional)", { value: settings.contentId, maxLength: 36 }, (v) => {
      const id = v.trim().toUpperCase();
      if (id && !/^[A-Z0-9]{6}-[A-Z]{4}[0-9]{5}_00-[A-Z0-9]{16}$/.test(id)) return toast("Formato de Content ID inválido.", true);
      studio.apply((s) => { s.contentId = id; });
    })) : null),
    block("Biblioteca de mídia", button("Importar mídia", () => studio.pickMedia(), { icon: "upload", class: "primary" }),
      assetList(studio, all, (asset) => studio.openAsset(asset))),
    button("Licenças e componentes", () => studio.licenses(), { icon: "info", class: "plain" }));
}

function background(studio) {
  const s = studio.project.settings;
  const key = studio.backgroundSlot;
  const item = s.background[key];
  const slots = ["home", "function", "preview", "icon0"].filter((k) => s.mode !== "animated" || k !== "function");
  const choose = (asset) => studio.assignVisual("background", key, asset);
  return stack(
    block("Modo de fundo", select("Tipo", s.mode, [["static", "Estático"], ["animated", "Animado (RAF)"]],
      (v) => studio.apply((settings) => { settings.mode = v; })),
    s.mode === "animated" ? hint("A cena RAF cobre a tela inicial e o menu superior. A imagem estática serve de capa e miniatura.") : null),
    block("Imagem do tema", select("Slot de imagem", slots.includes(key) ? key : "home",
      slots.map((slot) => [slot, slot === "home" && s.mode === "animated" ? "Capa do tema" : slotName(slot, words)]),
      (v) => {
        studio.backgroundSlot = v;
        studio.setView(v === "function" ? "function" : v === "preview" ? "apply" : "home");
        studio.renderPanel();
      }),
    mediaPicker(studio, ["image", "animation"], choose),
    item ? stack(hint(studio.project.assets[item.asset].name),
      transforms(studio, (s) => s.background[key].transform),
      button("Remover imagem", () => studio.apply((s) => { delete s.background[key]; }))) : null));
}

function animation(studio) {
  const a = studio.project.settings.animation;
  const b = studio.rafBudget();
  const asset = a.source && studio.project.assets[a.source.asset];
  const set = (key, val) => studio.apply((s) => { s.animation[key] = val; });
  const sample = (mode) => studio.apply((s) => {
    const animation = s.animation;
    if (mode === "manual" && !animation.timeline.length) {
      const count = Math.min(animation.frames, b.preset.max);
      const baseCount = animation.pingPong ? Math.floor((count + 2) / 2) : count;
      animation.timeline = Array.from({length: baseCount}, (_, i) =>
        animation.start + ((animation.end ?? asset.duration) - animation.start) * i / baseCount);
    }
    animation.sampling = mode;
  });
  const adjustTiming = (duration, fps) => {
    const count = Math.max(2, Math.round(duration * fps));
    const choices = [];
    const choose = (frames, rate, width = a.width) => studio.apply((s) => {
      Object.assign(s.animation, { frames, wait: clamp(1 / rate, .02, 5), speed: 1, width, sampling: "uniform" });
    });
    if (count <= b.preset.max) return choose(count, count / duration);
    choices.push(button(`${seconds(b.preset.max / fps)} · ${number(fps)} FPS`, () => {
      choose(b.preset.max, fps); $("dialog").close();
    }), button(`${seconds(duration)} · ${number(b.preset.max / duration)} FPS`, () => {
      choose(b.preset.max, b.preset.max / duration); $("dialog").close();
    }));
    const lower = studio.setup.presets.find((p) => p.max >= count);
    if (lower) choices.push(button(`${lower.width} × ${lower.height} · ${seconds(duration)} · ${number(count / duration)} FPS`, () => {
      choose(count, count / duration, lower.width); $("dialog").close();
    }));
    studio.dialog("Ajustar duração e fluidez", stack(hint(`${count} quadros solicitados. O limite em ${a.width} px é ${b.preset.max}.`), choices));
  };
  const preset = (width) => studio.apply((s) => {
    const max = studio.setup.presets.find((p) => p.width === width).max;
    s.animation.width = width;
    s.animation.frames = Math.min(s.animation.frames, max);
    if (s.animation.sampling === "manual") {
      const cap = s.animation.pingPong ? Math.floor((max + 2) / 2) : max;
      s.animation.timeline = s.animation.timeline.slice(0, cap);
    }
  });
  const options = studio.setup.presets.map((p, i) => [p.width, `${words.presets[i]} · ${p.width} × ${p.height}`]);
  return stack(
    block("Fonte da animação", mediaPicker(studio, ["video", "animation"], (newAsset) => studio.assignAnimation(newAsset), "Escolher GIF ou vídeo"),
      asset ? hint(`${asset.name} · ${seconds(asset.duration)} · ${asset.fps ? number(asset.fps) + " FPS de origem" : "FPS variável"}`) : null,
      hint("O tema usa quadros amostrados. A velocidade do RAF depende do tempo entre esses quadros.")),
    block("Orçamento RAF", select("Preset", a.width, options, preset),
      node("progress", { max: 100, value: Math.min(100, b.percent), "aria-label": "Percentual do orçamento DDS" }),
      hint(`${b.frames} quadros de ${b.preset.max} para este tamanho · ${bytes(b.total)} de 6 MiB`),
      a.sampling === "manual" && b.frames > b.preset.max ? button("Ajustar ao limite", () => preset(a.width)) : null,
      asset ? button("Otimizar RAF", () => studio.optimizer(), { icon: "layers" }) : null),
    asset ? block("Trecho e reprodução",
      fields(numberField("Início (s)", a.start, 0, Math.max(0, asset.duration - .001), "any", (v) => {
        if (v >= (a.end ?? asset.duration)) return toast("O início precisa ficar antes do fim.", true);
        set("start", v); studio.scrub(v);
      }), numberField("Fim (s)", a.end ?? asset.duration, 0, asset.duration + .0001, "any", (v) => {
        if (v <= a.start) return toast("O fim precisa ficar depois do início.", true);
        set("end", v);
      })),
      studio.advanced ? stack(
        range("Marcador de início", a.start, 0, Math.max(.01, (a.end ?? asset.duration) - .01), .01,
          (v) => { set("start", v); studio.scrub(v); }),
        range("Marcador de fim", a.end ?? asset.duration, Math.min(asset.duration, a.start + .01), asset.duration, .01,
          (v) => set("end", v))) : null,
      fields(numberField("Loop desejado (s)", Number(b.duration.toFixed(3)), .04, 240, "any", (v) => adjustTiming(v, b.fps)),
        numberField("FPS RAF", Number(b.fps.toFixed(3)), .2, 50, "any", (v) => adjustTiming(b.duration, v))),
      checkbox("Reverso", a.reverse, (v) => set("reverse", v)),
      checkbox("Pingue-pongue", a.pingPong, (v) => set("pingPong", v)),
      button("Criar loop", () => studio.openLoop(), { icon: "compare" }),
      studio.advanced ? stack(
        select("Amostragem", a.sampling, [["uniform", "Uniforme"], ["manual", "Manual"]], sample),
        a.sampling === "uniform" ? range("Quadros", a.frames, 2, b.preset.max, 1, (v) => set("frames", v)) :
          button("Adicionar quadro no playhead", () => studio.manualAdd()),
        numberField("Intervalo base (s)", a.wait, .02, 5, "any", (v) => set("wait", v)),
        range("Multiplicador de velocidade", a.speed, .25, 4, .05, (v) => set("speed", v)),
        transforms(studio, (s) => s.animation.source.transform)) : null) : null);
}

function layers(studio) {
  const layers = studio.project.settings.layers, i = studio.layerIndex, current = layers[i];
  const set = (key, v) => studio.apply((s) => { s.layers[i][key] = v; });
  const add = (item) => {
    if (layers.length >= 8) return toast("O projeto aceita até 8 camadas.", true);
    studio.layerIndex = layers.length;
    studio.apply((s) => s.layers.push({ name: `Camada ${layers.length + 1}`, color: "#FFFFFF",
      visual: item ? visual(item.id) : null, transform: blankTransform(), visible: true, locked: false, start: 0, end: null }));
  };
  return stack(
    block("Composição", hint("As camadas são combinadas antes de gerar a cena. GIFs usados como camada entram pelo primeiro quadro."),
      row(button("Cor", () => add(null), { icon: "plus" }), button("Imagem", () => studio.chooseAsset(["image", "animation"], add), { icon: "image" })),
      layers.map((layer, index) => button(layer.name, () => { studio.layerIndex = index; studio.renderPanel(); },
        { "aria-pressed": String(i === index), icon: "layers" }))),
    current ? block(current.name,
      field("Nome da camada", { value: current.name, maxLength: 127 }, (v) => { if (v.trim()) set("name", v.trim()); }),
      checkbox("Visível", current.visible, (v) => set("visible", v)), checkbox("Bloquear edição", current.locked, (v) => set("locked", v)),
      current.locked ? null : stack(
        row(button("Subir", () => studio.moveLayer(1), { disabled: i + 1 >= layers.length }),
          button("Descer", () => studio.moveLayer(-1), { disabled: i === 0 }),
          button("Remover", () => { studio.layerIndex = Math.max(0, i - 1); studio.apply((s) => s.layers.splice(i, 1)); })),
        !current.visual ? field("Cor da camada", { type: "color", value: current.color }, (v) => set("color", v)) : null,
        studio.advanced ? fields(numberField("Entrada (s)", current.start, 0, current.end ?? 600, "any", (v) => set("start", v)),
          numberField("Saída (s), 0 = até o fim", current.end ?? 0, 0, 600, "any", (v) => set("end", v || null))) : null,
        transforms(studio, (s) => s.layers[i].transform, { crop: Boolean(current.visual) }))) : null);
}

function icons(studio) {
  const settings = studio.project.settings;
  const chosen = studio.iconSlot;
  const item = settings.icons[chosen];
  const specs = Object.entries(studio.setup.slots).filter(([, spec]) => spec.group !== "background");
  const spec = studio.setup.slots[chosen];
  const grid = (list) => node("div", { class: "icon-grid" }, list.map(([key, slot]) => {
    const assigned = settings.icons[key];
    const title = slotName(key, words);
    const card = button(node("span", {}, title), () => { studio.iconSlot = key; studio.renderPanel(); }, {
      class: "icon-card", "aria-pressed": String(key === chosen), title: `${title} · ${slot.size.join(" × ")}`
    });
    card.prepend(assigned ? node("img", { src: studio.assetUrl(assigned.asset, "preview"), alt: "" }) : symbol("grid"));
    return card;
  }));
  return stack(
    block(slotName(chosen, words), hint(`${spec.size.join(" × ")}${spec.glow ? " · glow 152 × 152" : ""}`),
      mediaPicker(studio, ["image", "animation"], (asset) => studio.assignVisual("icons", chosen, asset), "Substituir"),
      item ? stack(button("Preview normal e glow", () => studio.openIcon()), transforms(studio, (s) => s.icons[chosen].transform),
        spec.glow ? range("Intensidade do glow", item.glow, 0, 1, .05, (v) => studio.apply((s) => { s.icons[chosen].glow = v; })) : null,
        button("Restaurar ícone do sistema", () => studio.apply((s) => { delete s.icons[chosen]; }))) : hint("O PS4 manterá o ícone do sistema.")),
    block("Menu superior", grid(specs.filter(([, x]) => x.group === "function_icon"))),
    block("Conteúdo", grid(specs.filter(([, x]) => x.group === "content_icon"))));
}

function audio(studio) {
  const slot = studio.audioSlot;
  const current = studio.project.settings.audio[slot];
  const asset = current && studio.project.assets[current.asset];
  const set = (key, v) => studio.apply((s) => { s.audio[slot][key] = v; });
  const compatible = slot === "bgm" ? ["audio", "at9"] : ["audio", "at9", "vag"];
  const add = (file) => {
    if (!compatible.includes(file.kind)) return toast("Escolha um arquivo de áudio compatível com o slot.", true);
    studio.apply((s) => { s.audio[slot] = track(file.id); });
  };
  const waveform = asset && node("div", { class: "waveform", role: "img",
    "aria-label": "Forma de onda. Pico estimado " + number(Math.max(0, ...asset.peak) * 100, 0) + "%" },
    asset.peak.map((p) => { const bar = node("i"); bar.style.height = `${Math.max(4, p * 100)}%`; return bar; }));
  return stack(block("Faixa", select("Slot", slot, studio.setup.audioSlots.map((key) => [key, slotName(key, words)]),
    (v) => { studio.audioSlot = v; studio.renderPanel(); }), mediaPicker(studio, compatible, add),
    slot === "bgm" ? hint("Use um AT9 pronto. Com outros formatos, será preparado um WAV para conversão externa.") :
      hint("O efeito é exportado em VAG a 48 kHz com até 5 segundos.")),
  current ? block(asset.name, waveform, hint(`${seconds(asset.duration)} · Pico estimado ${number(Math.max(0, ...asset.peak) * 100, 0)}%${asset.loudness != null ? " · " + number(asset.loudness, 1) + " LUFS da origem" : ""}`),
    row(button("Escutar", () => studio.playSound(slot, true), { icon: "play" }), button("Parar", () => studio.stopSound(), { icon: "pause" })),
    slot === "bgm" ? checkbox("Repetir preview", studio.audioLoop, (v) => { studio.audioLoop = v; }) : null,
    slot === "bgm" && asset.kind === "at9" ? hint("Para mudar ganho ou corte, prepare um WAV e converta novamente.") : stack(
      fields(numberField("Início (s)", current.start, 0, asset.duration, "any", (v) => set("start", v)),
        numberField("Fim (s)", current.end ?? asset.duration, 0, asset.duration, "any", (v) => set("end", v))),
      range("Ganho (dB)", current.gain, -24, 12, .5, (v) => set("gain", v)),
      fields(numberField("Fade-in (s)", current.fadeIn, 0, 30, "any", (v) => set("fadeIn", v)),
        numberField("Fade-out (s)", current.fadeOut, 0, 30, "any", (v) => set("fadeOut", v))),
      checkbox("Normalizar pico", current.normalize, (v) => set("normalize", v))),
    button("Remover faixa", () => studio.apply((s) => { delete s.audio[slot]; }))) : null);
}

function colors(studio) {
  const a = studio.project.settings.appearance;
  const set = (key, value) => studio.apply((s) => { s.appearance[key] = value; });
  return stack(block("Aparência", select("Cor do sistema", String(a.themecolor), words.colors.map((x, i) => [String(i), x]),
    (v) => set("themecolor", Number(v))),
  field("Cor do texto", { type: "color", value: "#" + a.fontcolor.slice(3) }, (v) => set("fontcolor", "#FF" + v.slice(1))),
  field("Cor da seleção", { type: "color", value: "#" + a.focuscolor.slice(3) }, (v) => set("focuscolor", "#FF" + v.slice(1))),
  field("Base da composição", { type: "color", value: a.background }, (v) => set("background", v))));
}

function preview(studio) {
  return stack(block("Simulação", hint("A simulação usa a disposição das três telas para avaliar legibilidade, foco e áudio."),
    button(studio.overlay ? "Ver preview limpo" : "Mostrar interface PS4", () => studio.toggleOverlay(), { icon: "eye" }),
    button("Testar legibilidade", () => studio.testContrast(), { icon: "check" }),
    row(button("Música", () => studio.playSound("bgm", true), { icon: "audio" }),
      button("Parar áudio", () => studio.stopSound())),
    row(...["se_cursor", "se_decide", "se_cancel"].map((key) =>
      button(slotName(key, words), () => studio.playSound(key, true))))));
}

export function reportView(studio) {
  const report = studio.result?.report;
  const current = report?.revision === studio.project.revision && !studio.dirty;
  if (!report) return hint("A validação gera os arquivos e verifica a estrutura antes do download.");
  return stack(current ? null : hint("O projeto foi alterado após este relatório."),
    hint(`Build ${report.buildId.slice(0, 10)} · ${bytes(report.totalBytes)}`),
    node("ul", { class: "report" }, report.checks.map(({ category, status, message }) =>
      node("li", {}, node("strong", { class: status }, `${category} · ${words.statuses[status]}`), hint(message)))));
}

function validate(studio) {
  const matrix = studio.result?.report?.testMatrix;
  const status = (v) => v === "passed" ? "Verificado" : v === "failed" ? "Falhou" : "Não verificado";
  const technical = studio.advanced && studio.result?.report ? node("details", {},
    node("summary", {}, "Detalhes técnicos"), node("pre", { class: "report-json" },
      JSON.stringify(studio.result.report, null, 2))) : null;
  return stack(block("Compatibilidade", button("Validar tema", () => studio.startJob("validate"), { icon: "check", class: "primary", disabled: studio.busy }),
    reportView(studio), technical), block("Matriz de testes",
    hint(`Validação estrutural: ${status(matrix?.structural)} · Parsers: ${status(matrix?.parser)} · PKG: ${status(matrix?.package)}`),
    hint("Registre um teste físico com o Build ID. Validação estrutural não indica instalação ou funcionamento no console."),
    button("Registrar teste físico", () => studio.consoleTest()), studio.project.settings.consoleTests.map((test) =>
      hint(`${test.console} · ${test.firmware} · ${test.buildId || "Sem Build ID"} · ${test.applied ? "Aplicado" : "Aplicação não confirmada"}`))));
}

function exports(studio) {
  const options = [
    ["project", "Projeto com originais"], ["assets", "Assets processados"], ["raf", "Cena RAF"],
    ["tree", "Árvore do tema"], ["bundle", "Bundle para empacotamento"], ["diagnostic", "Diagnóstico sem mídia original"]
  ];
  return stack(block("Exportar", options.map(([kind, name]) => button(name, () => studio.startJob(kind), {
    icon: "download", class: kind === "bundle" ? "primary" : "", disabled: studio.busy ||
      (kind === "raf" && studio.project.settings.mode !== "animated")
  }))),
  block("Empacotamento externo", hint("O servidor prepara os arquivos e o GP4. Use o bundle na ferramenta externa compatível para gerar e verificar o .pkg.")),
  studio.project.exports.length ? block("Exportações recentes", studio.project.exports.slice(-5).reverse().map((entry) =>
    entry.bytes ? node("a", { class: "button", href: studio.jobUrl(entry.id) + "/download" },
      `${options.find(([key]) => key === entry.kind)?.[1] || entry.kind} · ${bytes(entry.bytes)}`) : null)) : null);
}

export function renderPanel(studio) {
  const panels = { project, background, animation, layers, icons, audio, colors, preview, validate, export: exports };
  $("panel-title").textContent = names[studio.panel];
  const display = $("properties");
  const oldScroll = display.scrollTop;
  const active = document.activeElement;
  const details = active?.closest("details");
  const content = panels[studio.panel](studio);
  if (details?.open && content.querySelector("details")) content.querySelector("details").open = true;
  if (studio.busy || studio.result) {
    const job = studio.busy || studio.result;
    content.append(block("Processamento", hint(job.stage),
      studio.busy ? node("progress", { max: 100, value: job.progress, "aria-label": "Progresso da tarefa" }) : null,
      studio.busy ? button("Cancelar tarefa", () => studio.cancelJob()) : null,
      job.error ? hint(job.error.message) : null,
      job.download ? node("a", { class: "button primary", href: studio.jobUrl(job.id) + "/download" }, "Baixar ZIP") : null,
      job.report && studio.panel !== "validate" && job.report.errors.length ? reportView(studio) : null));
  }
  display.replaceChildren(content);
  display.scrollTop = oldScroll;
}
