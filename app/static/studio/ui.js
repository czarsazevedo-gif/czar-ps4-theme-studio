const icons = {
 studio: '<path d="M3 6.5h18v11H3zM7 10h5m-2.5-2.5v5m6.5-2h.01m2.5 2h.01"/>',
 plus: '<path d="M12 5v14M5 12h14"/>', folder: '<path d="M3 6h7l2 2h9v11H3z"/>',
 upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
 download: '<path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/>',
 save: '<path d="M4 3h13l3 3v15H4zM8 3v6h8V3M8 21v-8h8v8"/>',
 undo: '<path d="m8 5-5 5 5 5M3 10h12a5 5 0 0 1 0 10"/>',
 redo: '<path d="m16 5 5 5-5 5m5-5H9a5 5 0 0 0 0 10"/>',
 eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="2.6"/>',
 check: '<path d="m5 12 4 4L20 5"/>', image: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m3 16 5-5 4 4 3-3 6 6"/><circle cx="16" cy="8" r="1"/>',
 film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4m10 0h4M3 15h4m10 0h4"/>',
 layers: '<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
 grid: '<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/>',
 audio: '<path d="M9 17V6l11-3v13M9 6l11-3M9 17c0 2-6 4-6 1s6-4 6-1Zm11-1c0 2-6 4-6 1s6-4 6-1Z"/>',
 colors: '<path d="M12 3a9 9 0 1 0 0 18h2a2 2 0 0 0 0-4 2 2 0 0 1 0-4h3a4 4 0 0 0 0-8Z"/><path d="M7 11h.01M10 7h.01m5 1h.01"/>',
 info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>',
 close: '<path d="m6 6 12 12M18 6 6 18"/>',
 play: '<path d="m8 4 13 8-13 8Z"/>', pause: '<path d="M8 5v14m8-14v14"/>',
 loop: '<path d="M3 12a5 5 0 0 1 5-5h12m-4-4 4 4-4 4m5 1a5 5 0 0 1-5 5H4m4-4-4 4 4 4"/>',
 compare: '<path d="M12 3v18M3 6h6v12H3zm12 0h6v12h-6z"/>',
};
export function symbol(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(key, value);
  svg.innerHTML = icons[name] || icons.grid;
  return svg;
}
export const $ = (id) => document.getElementById(id);
export function node(tag, props = {}, ...content) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "on") Object.entries(value).forEach(([event, listener]) => el.addEventListener(event, listener));
    else if (key === "icon") { if (value) el.append(symbol(value)); }
    else if (key === "class") el.className = value;
    else if (key === "data") Object.assign(el.dataset, value);
    else if (key in el && !key.startsWith("aria")) el[key] = value;
    else if (value != null) el.setAttribute(key, value);
  }
  el.append(...content.flat(Infinity).filter((part) => part != null));
  return el;
}
export const button = (label, handler, props = {}) => node("button", { type: "button", ...props, on: { click: handler } }, label);
export const row = (...content) => node("div", { class: "row" }, content);
export const stack = (...content) => node("div", { class: "stack" }, content);
export const hint = (content) => node("p", { class: "hint" }, content);
export const block = (title, ...content) => node("section", { class: "panel-block stack" }, node("h3", {}, title), content);
export const fields = (...content) => node("div", { class: "fields" }, content);
export const body = $("dialog-body");
export function dialog(title, content) {
  $("dialog-title").textContent = title;
  body.replaceChildren(content);
  if (!$("dialog").open) $("dialog").showModal();
}
export function toast(message, error = false) {
  const card = node("div", { class: error ? "notice error" : "notice" }, message);
  $("notifications").append(card);
  setTimeout(() => card.remove(), 5500);
}
let fieldNumber = 0;
export function field(title, props, onChange, onInput) {
  let input;
  const label = node("span", { id: `field-label-${++fieldNumber}` }, title);
  if (props.options) {
    input = node("select", { disabled: props.disabled ?? false }, props.options.map(([value, text]) => node("option", { value }, text)));
    input.value = props.value ?? "";
  } else if (props.type === "textarea") {
    input = node("textarea", { value: props.value, maxLength: props.maxLength || 2000 });
  } else {
    input = node("input", props);
    if (props.value != null) input.value = props.value;
  }
  input.setAttribute("aria-labelledby", label.id);
  const type = input.type;
  const read = () => type === "checkbox" ? input.checked :
    (type === "range" || type === "number") ? input.valueAsNumber : input.value;
  input.addEventListener("change", () => {
    const value = read();
    if (type !== "number" || (Number.isFinite(value) && input.checkValidity())) onChange(value);
    else toast("Informe um valor dentro do intervalo.", true);
  });
  if (type === "range") {
    const output = node("span", { class: "field-value", "aria-hidden": "true" }, String(props.value));
    label.append(output);
    input.addEventListener("input", () => { output.textContent = String(read()); if (onInput) onInput(read()); });
  }
  return node("label", { class: type === "checkbox" ? "field check" : "field" },
    type === "checkbox" ? [input, label] : [label, input]);
}
