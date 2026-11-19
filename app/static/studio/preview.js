import { $, button, node, symbol } from "./ui.js";
import { blankTransform, clamp, slotName } from "./model.js";
import { words } from "./i18n.js";

const cache = new Map();
const FRAME_CACHE = 56;
const palette = ["#1e9bff", "#ff5fa2", "#e8413c", "#2c4a7c", "#6b7280", "#d9a441", "#a8c0d8", "#8b5cf6"];

function image(url) {
  if (!url) return Promise.resolve(null);
  if (!cache.has(url)) {
    const img = new Image();
    const pending = new Promise((resolve) => {
      img.onload = () => resolve(img);
      img.onerror = () => { cache.delete(url); resolve(null); };
      img.src = url;
    });
    cache.set(url, pending);
    if (cache.size > FRAME_CACHE) cache.delete(cache.keys().next().value);
  }
  return cache.get(url);
}

function draw(ctx, w, h, bitmap, edit) {
  if (!bitmap) return;
  const [a, b, c, d] = edit.crop;
  const dw = (c - a) * bitmap.width, dh = (d - b) * bitmap.height;
  const fit = edit.fit === "contain" ? Math.min : Math.max;
  const scale = fit(w / dw, h / dh) * edit.zoom;
  ctx.save();
  ctx.globalAlpha = edit.opacity;
  ctx.filter = `brightness(${edit.brightness}) contrast(${edit.contrast}) saturate(${edit.saturation})`;
  ctx.translate(w * (.5 + edit.x), h * (.5 + edit.y));
  ctx.rotate(edit.rotation * Math.PI / 180);
  ctx.scale(edit.flipX ? -1 : 1, edit.flipY ? -1 : 1);
  ctx.drawImage(bitmap, a * bitmap.width, b * bitmap.height, dw, dh,
    -dw * scale / 2, -dh * scale / 2, dw * scale, dh * scale);
  ctx.restore();
}

let serial = 0;

export async function drawStage(studio, canvas = $("canvas"), time = studio.time, target = null, framePosition = studio.layerTime) {
  const generation = target ? null : ++serial;
  const { settings } = studio.project;
  const { width, height } = canvas;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const animation = settings.animation;
  const animated = settings.mode === "animated" && (target || studio.view !== "apply");
  const visual = animated ? animation.source : settings.background[studio.view === "function" ? "function" : studio.view === "apply" ? "preview" : "home"]
    || settings.background.home;
  const displayFrame = animated || (!visual && settings.mode === "animated" && animation.source);
  const asset = (displayFrame ? animation.source : visual)?.asset;
  const edit = { ...((displayFrame ? animation.source : visual)?.transform || blankTransform()) };
  const source = asset && studio.project.assets[asset];
  const active = settings.layers.filter((layer) => layer.visible && framePosition >= layer.start && (layer.end === null || framePosition < layer.end))
    .map((layer) => ({ ...layer, transform: { ...layer.transform } }));
  const data = await Promise.all([
    image(source && (displayFrame && source.duration ? studio.frameUrl(source, time) : studio.assetUrl(source.id, "preview"))),
    ...active.map((layer) => layer.visual ? image(studio.assetUrl(layer.visual.asset, "preview")) : null)
  ]);
  if (generation !== null && generation !== serial) return;
  ctx.fillStyle = settings.appearance.background;
  ctx.fillRect(0, 0, width, height);
  draw(ctx, width, height, data[0], edit);
  const colorSource = document.createElement("canvas");
  colorSource.width = 16; colorSource.height = 9;
  const colorCtx = colorSource.getContext("2d");
  active.forEach((layer, i) => {
    if (!layer.visual) {
      colorCtx.fillStyle = layer.color;
      colorCtx.fillRect(0, 0, 16, 9);
    }
    draw(ctx, width, height, layer.visual ? data[i + 1] : colorSource, layer.transform);
  });
  if (!target) {
    $("canvas-empty").hidden = Boolean(source || active.length);
    $("canvas").classList.toggle("editable", Boolean(studio.editTarget()));
    if (studio.overlay) contrast(studio);
  }
  return ctx;
}

function luminance(r, g, b) {
  const [a, c, d] = [r, g, b].map((ch) => {
    const s = ch / 255;
    return s <= .04045 ? s / 12.92 : ((s + .055) / 1.055) ** 2.4;
  });
  return .2126 * a + .7152 * c + .0722 * d;
}

export function contrast(studio) {
  const canvas = $("canvas"), ctx = canvas.getContext("2d", { willReadFrequently: true });
  const rgb = studio.project.settings.appearance.fontcolor.slice(3).match(/../g).map((v) => parseInt(v, 16));
  const foreground = luminance(...rgb);
  const samples = [[.18, .62], [.32, .62], [.66, .09], [.82, .09], [.28, .32], [.55, .32]];
  const failures = samples.filter(([x, y]) => {
    const pixel = ctx.getImageData(x * canvas.width, y * canvas.height, 1, 1).data;
    const bg = luminance(pixel[0], pixel[1], pixel[2]);
    return (Math.max(bg, foreground) + .05) / (Math.min(bg, foreground) + .05) < 4.5;
  }).length;
  const note = $("contrast-note");
  note.classList.toggle("warning", failures > 0);
  note.textContent = failures ? "Contraste baixo em pontos da interface" : "Contraste legível nos pontos amostrados";
}

export function drawOverlay(studio) {
  const overlay = $("screen-overlay");
  overlay.hidden = !studio.overlay;
  overlay.replaceChildren();
  if (!studio.overlay) { $("contrast-note").textContent = ""; return; }
  const { settings } = studio.project;
  overlay.style.setProperty("--font-color", "#" + settings.appearance.fontcolor.slice(3));
  overlay.style.setProperty("--focus-color", "#" + settings.appearance.focuscolor.slice(3));
  overlay.style.setProperty("--system-color", palette[settings.appearance.themecolor]);
  overlay.classList.toggle("sim-settings", studio.view === "apply");
  if (studio.view === "apply") {
    const cover = settings.background.preview || settings.background.home || settings.animation.source;
    if (cover) overlay.append(node("img", { src: studio.assetUrl(cover.asset, "preview"), alt: "Miniatura do tema" }));
    overlay.append(node("strong", {}, settings.name), button("Aplicar (simulação)", () => studio.playSound("se_decide"), { class: "sim apply-control" }));
    return;
  }
  const group = Object.entries(studio.setup.slots);
  const functionSlots = group.filter(([, slot]) => slot.group === "function_icon");
  const contentSlots = group.filter(([, slot]) => slot.group === "content_icon").slice(0, 6);
  const makeRow = (slots, cls) => node("div", { class: `sim-icons ${cls}` }, slots.map(([key, slot]) => {
    const current = key === studio.selectedPreview;
    const spec = settings.icons[key];
    const item = button("", () => {
      studio.playSound(current ? "se_decide" : "se_cursor");
      studio.selectedPreview = key;
      drawOverlay(studio);
      overlay.querySelector(".is-current")?.focus({ preventScroll: true });
    }, { class: `sim${current ? " is-current" : ""}`, title: slotName(key, words), "aria-label": slotName(key, words),
      "aria-pressed": String(current) });
    if (spec) {
      const img = node("img", { src: studio.assetUrl(spec.asset, "preview"), alt: "" });
      if (current && slot.glow) img.style.filter = `drop-shadow(0 0 ${spec.glow * 12}px white)`;
      item.append(img);
    } else item.append(symbol(slot.group === "function_icon" ? "folder" : "grid"));
    return item;
  }));
  overlay.append(node("span", { class: "sim-user" }, settings.name));
  if (studio.view === "home") overlay.append(makeRow(functionSlots, ""), makeRow(contentSlots, "sim-content"));
  else overlay.append(makeRow(functionSlots, "sim-function"));
  const selected = studio.selectedPreview;
  if (selected) overlay.append(node("strong", { class: "sim-name" }, slotName(selected, words)));
}

export function setupDrag(studio) {
  const canvas = $("canvas");
  let start;
  canvas.addEventListener("pointerdown", (event) => {
    if (!studio.project || event.button !== 0) return;
    const target = studio.editTarget();
    if (!target) return;
    const edit = target(studio.project.settings);
    start = { pointer: event.pointerId, target, settings: structuredClone(studio.project.settings),
      x: event.clientX, y: event.clientY, left: edit.x, top: edit.y };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!start || start.pointer !== event.pointerId) return;
    const edit = start.target(studio.project.settings);
    edit.x = clamp(start.left + (event.clientX - start.x) / canvas.clientWidth, -1, 1);
    edit.y = clamp(start.top + (event.clientY - start.y) / canvas.clientHeight, -1, 1);
    studio.draw();
  });
  const stop = (event) => {
    if (!start || start.pointer !== event.pointerId) return;
    const previous = start.settings;
    start = null;
    studio.changed(previous);
  };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
}

function visualDifference(a, b, width, height) {
  const pixelsA = a.getImageData(0, 0, width, height).data;
  const pixelsB = b.getImageData(0, 0, width, height).data;
  let error = 0;
  for (let i = 0; i < pixelsA.length; i += 4) {
    error += Math.abs(pixelsA[i] - pixelsB[i]) + Math.abs(pixelsA[i + 1] - pixelsB[i + 1]) + Math.abs(pixelsA[i + 2] - pixelsB[i + 2]);
  }
  return 100 * error / (width * height * 3 * 255);
}

export async function findLoop(studio, times) {
  const width = 160, height = 90;
  const a = node("canvas", { width, height }), b = node("canvas", { width, height });
  const base = await drawStage(studio, a, times[0], true, 0);
  let best = { time: times.at(-1), difference: Infinity };
  const candidates = [...new Set(times.slice(Math.ceil(times.length / 2)).slice(-6))];
  for (const time of candidates) {
    const end = await drawStage(studio, b, time, true, (times.length - 1) * studio.rafBudget().wait);
    const difference = visualDifference(base, end, width, height);
    if (difference < best.difference) best = { time, difference };
  }
  return best.time;
}

export function loopComparison(studio, times, pair = [times[0], times.at(-1)]) {
  const first = node("canvas", { width: 240, height: 135 });
  const last = node("canvas", { width: 240, height: 135 });
  const output = node("canvas", { width: 240, height: 135 });
  const metric = node("p", { class: "hint" }, "Comparando os extremos…");
  if (pair.every(Number.isFinite)) {
    Promise.all([drawStage(studio, first, pair[0], true, 0), drawStage(studio, last, pair[1], true,
      (times.length - 1) * studio.rafBudget().wait)]).then(([a, b]) => {
      const difference = visualDifference(a, b, first.width, first.height);
      const ctx = output.getContext("2d");
      ctx.drawImage(first, 0, 0);
      ctx.globalAlpha = .5;
      ctx.drawImage(last, 0, 0);
      metric.textContent = `Diferença média de cor entre as extremidades: ${difference.toFixed(1)}%.`;
    });
  }
  return [first, last, output, metric];
}
