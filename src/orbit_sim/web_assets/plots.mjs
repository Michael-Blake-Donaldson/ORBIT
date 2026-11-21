import {elapsed, measure, metrics, nearestSample} from "./mission.mjs";

const grid = "#26384e";
const muted = "#a4b5cc";
const mint = "#76ebd3";
const amber = "#ffbe75";
const earthRadius = 6378.137;
const burnKinds = new Set(["post_burn", "burn_start", "burn_end"]);
const planes = {XY: [0, 1], XZ: [0, 2], YZ: [1, 2]};
const plotFrame = {left: 68, right: 41, top: 17, bottom: 29};

function canvasContext(canvas) {
  const {width, height} = canvas.getBoundingClientRect();
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== Math.round(width * pixelRatio) || canvas.height !== Math.round(height * pixelRatio)) {
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.font = "12px Consolas, monospace";
  ctx.lineWidth = 1;
  ctx.fillStyle = muted;
  return {ctx, width, height};
}

function segment(ctx, x1, y1, x2, y2) {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
}

function dot(ctx, x, y, radius, fill) {
  ctx.fillStyle = fill;
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, 2 * Math.PI);
  ctx.fill();
}

function empty(ctx, width, height, label) {
  ctx.textAlign = "center";
  ctx.fillText(label, width / 2, height / 2);
}

function gridStep(extent) {
  const base = 10 ** Math.floor(Math.log10(extent));
  return ([1, 2, 5, 10].find((factor) => factor * base >= extent) ?? 10) * base;
}

export function drawOrbit(canvas, rows, selected, plane) {
  const {ctx, width, height} = canvasContext(canvas);
  if (!rows.length) return empty(ctx, width, height, "Trajectory appears after a run");
  const [a, b] = planes[plane];
  const extent = rows.reduce((highest, row) =>
    Math.max(highest, Math.abs(row.position[a]), Math.abs(row.position[b])), earthRadius);
  const originX = width / 2;
  const originY = height / 2;
  const scale = .43 * Math.min(width, height) / extent;
  const spacing = scale * gridStep(62 / scale);
  const project = (row) => [originX + row.position[a] * scale, originY - row.position[b] * scale];
  ctx.strokeStyle = grid;
  for (let x = originX % spacing; x < width; x += spacing) segment(ctx, x, 0, x, height);
  for (let y = originY % spacing; y < height; y += spacing) segment(ctx, 0, y, width, y);
  ctx.strokeStyle = "#3a546b";
  segment(ctx, originX, 0, originX, height);
  segment(ctx, 0, originY, width, originY);
  const earth = ctx.createRadialGradient(originX - 12, originY - 15, 0, originX, originY, earthRadius * scale + 18);
  earth.addColorStop(0, "#306486");
  earth.addColorStop(1, "#152943");
  dot(ctx, originX, originY, earthRadius * scale, earth);
  ctx.strokeStyle = "#5283a2";
  ctx.stroke();
  ctx.fillStyle = muted;
  ctx.textAlign = "center";
  if (earthRadius * scale >= 24) ctx.fillText("EARTH", originX, originY + 4);
  ctx.textAlign = "left";
  ctx.fillText(`${plane[0]} / ${plane[1]}`, 13, 21);
  ctx.textAlign = "right";
  ctx.fillText(`Grid ${measure(spacing / scale, "km", 0)}`, width - 13, height - 12);
  ctx.strokeStyle = mint;
  ctx.lineWidth = 2;
  ctx.beginPath();
  rows.forEach((row, index) => {
    const [x, y] = project(row);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  for (const row of rows) {
    if (burnKinds.has(row.kind)) dot(ctx, ...project(row), 3.7, amber);
  }
  dot(ctx, ...project(rows[selected]), 6, "#f7fbff");
  dot(ctx, ...project(rows[selected]), 3, "#162839");
}

export function drawTelemetry(canvas, rows, selected, metric) {
  const {ctx, width, height} = canvasContext(canvas);
  if (!rows.length) return empty(ctx, width, height, "Telemetry appears after a run");
  const values = rows.map((row) => row[metric]).filter((value) => value !== null);
  if (!values.length) return empty(ctx, width, height, `No ${metrics[metric].label.toLowerCase()} for this run`);
  let min = Math.min(...values);
  let max = Math.max(...values);
  const padding = Math.max((max - min) * .11, Math.abs(max) * .00001, .00001);
  min -= padding;
  max += padding;
  const {left, right, top, bottom} = plotFrame;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const duration = rows.at(-1).time;
  const x = (time) => left + (duration > 0 ? time / duration * plotWidth : 0);
  const y = (value) => top + (max - value) / (max - min) * plotHeight;
  ctx.strokeStyle = grid;
  ctx.textAlign = "right";
  for (let index = 0; index <= 4; index++) {
    const value = min + (max - min) * (index / 4);
    segment(ctx, left, y(value), width - right, y(value));
    ctx.fillText(measure(value, "", 3), left - 7, y(value) + 4);
  }
  ctx.textAlign = "center";
  const divisions = width < 420 ? 2 : 4;
  for (let index = 0; index <= (duration > 0 ? divisions : 0); index++) {
    const time = duration * index / divisions;
    segment(ctx, x(time), top, x(time), height - bottom);
    ctx.fillText(elapsed(time).slice(0, duration < 5 ? 12 : 8), x(time), height - 10);
  }
  ctx.strokeStyle = mint;
  ctx.lineWidth = 1.8;
  ctx.beginPath();
  let drawing = false;
  for (const row of rows) {
    if (row[metric] === null) {
      drawing = false;
      continue;
    }
    if (drawing) ctx.lineTo(x(row.time), y(row[metric]));
    else ctx.moveTo(x(row.time), y(row[metric]));
    drawing = true;
  }
  ctx.stroke();
  const active = rows[selected];
  ctx.strokeStyle = "#b4c9de";
  ctx.lineWidth = 1;
  segment(ctx, x(active.time), top, x(active.time), height - bottom);
  if (active[metric] !== null) dot(ctx, x(active.time), y(active[metric]), 4, "#f7fbff");
}

export function sampleAtPointer(canvas, rows, clientX) {
  const box = canvas.getBoundingClientRect();
  const {left, right} = plotFrame;
  const fraction = Math.max(0, Math.min(1, (clientX - box.left - left) / Math.max(1, box.width - left - right)));
  return nearestSample(rows, fraction * rows.at(-1).time);
}
