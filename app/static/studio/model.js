export const blankTransform = () => ({
  fit: "cover", zoom: 1, x: 0, y: 0, rotation: 0, flipX: false, flipY: false, crop: [0, 0, 1, 1],
  opacity: 1, brightness: 1, contrast: 1, saturation: 1
});
export const visual = (asset) => ({ asset, transform: blankTransform(), glow: .65 });
export const track = (asset) => ({ asset, start: 0, end: null, gain: 0, fadeIn: 0, fadeOut: 0, normalize: false });
export const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
export const bytes = (value) => value < 1024 ? `${value} B` :
  value < 1048576 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1048576).toFixed(2)} MiB`;
export const seconds = (value) => `${Number(value).toFixed(2)} s`;
export const slotName = (slot, words) => words.slots[slot.split(":").at(-1)] || slot;

export function budget(animation, setup) {
  const preset = setup.presets.find(({ width }) => width === animation.width);
  const length = animation.timeline.length;
  const frames = animation.sampling === "manual" ?
    length + (animation.pingPong ? Math.max(0, length - 2) : 0) : animation.frames;
  const wait = Number(clamp(animation.wait / animation.speed, .02, 5).toPrecision(6));
  const size = Number(preset.width), height = preset.height;
  const total = frames * (128 + Math.ceil(size / 4) * Math.ceil(height / 4) * 8);
  return { preset, frames, wait, fps: 1 / wait, duration: frames * wait, total,
    percent: total / setup.rafLimit * 100, invalid: frames < 2 || frames > preset.max,
    near: frames / preset.max >= .9 };
}

export function orderedFrames(animation, duration) {
  const start = animation.start, end = animation.end ?? duration;
  if (!(duration > 0 && start < end && end <= duration + .001)) return [];
  let orders;
  if (animation.sampling === "manual") orders = animation.timeline.map((time, index) => ({ time, index }));
  else {
    const count = animation.pingPong ? Math.floor((animation.frames + 2) / 2) : animation.frames;
    orders = Array.from({ length: count }, (_, index) => ({ time: start + (end - start) * index / count, index }));
  }
  if (animation.reverse) orders.reverse();
  if (animation.pingPong) {
    orders.push(...orders.slice(1, -1).reverse());
    if (animation.sampling === "uniform" && orders.length < animation.frames) orders.push(orders[0]);
  }
  const first = orders.length ? animation.first % orders.length : 0;
  return [...orders.slice(first), ...orders.slice(0, first)];
}

export const schedule = (animation, duration) => orderedFrames(animation, duration).map(({ time }) => time);
