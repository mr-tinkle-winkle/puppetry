// Shared by every overlay page: the live connection to puppetry-overlay
// (Server-Sent Events) and the input state, mirroring kbm_layout.KbmState.
// Times are client seconds (performance.now()/1000); server timestamps are
// mapped through the clock offset measured from each snapshot.
"use strict";

const P = {
  style: {}, layout: null, labels: {},
  held: {}, out: {}, released: {},
  wheelDir: 0, wheelT: -1e9, wheelSrc: "r",
  burst: [], burstSrc: "r", lastMoveT: -1e9,
  axes: {}, outAxes: {}, padLabels: {},
  offset: 0, dirty: true, connected: false,
  onconfig: null,
};
const MOVE_GAP = 0.15;

function nowS() { return performance.now() / 1000; }
function toClient(t) { return t - P.offset; }

function applyEvent(ev) {
  const t = toClient(ev.t);
  if (ev.e === "kd" || ev.e === "ku") {
    const d = ev.s === "r" ? P.held : P.out;
    if (ev.e === "kd") { if (!(ev.k in d)) d[ev.k] = t; delete P.released[ev.k]; }
    else if (ev.k in d) { delete d[ev.k]; if (ev.s === "r") P.released[ev.k] = t; }
  } else if (ev.e === "ax") {
    const d = ev.s === "r" ? P.axes : P.outAxes;
    if (ev.v) d[ev.a] = ev.v; else delete d[ev.a];
  } else if (ev.e === "mv") {
    if (!P.burst.length || t - P.lastMoveT > MOVE_GAP || ev.s !== P.burstSrc) { P.burst = [[t, 0, 0]]; P.burstSrc = ev.s; }
    const last = P.burst[P.burst.length - 1];
    P.burst.push([t, last[1] + ev.dx, last[2] + ev.dy]);
    if (P.burst.length > 4000) P.burst = [P.burst[0]].concat(P.burst.slice(1).filter((_, i) => i % 2 === 0));
    P.lastMoveT = t;
  } else if (ev.e === "wh" && ev.n) {
    P.wheelDir = ev.n > 0 ? 1 : -1; P.wheelT = t; P.wheelSrc = ev.s;
  }
  P.dirty = true;
}

function applySnapshot(s) {
  const n = nowS();
  P.offset = s.now - n;
  P.held = {}; P.out = {};
  for (const [k, age] of Object.entries(s.held || {})) P.held[k] = n - age;
  for (const [k, age] of Object.entries(s.out || {})) P.out[k] = n - age;
  P.burst = (s.burst || []).map(([rel, x, y]) => [n + rel, x, y]);
  P.lastMoveT = s.last_move_age == null ? -1e9 : n - s.last_move_age;
  P.burstSrc = s.burst_src || "r";
  P.axes = s.axes || {}; P.outAxes = s.out_axes || {};
  P.dirty = true;
}

function connect() {
  const es = new EventSource("/events");
  es.addEventListener("config", (m) => {
    const c = JSON.parse(m.data);
    P.style = c.style || {}; P.layout = c.layout || null; P.labels = c.labels || {};
    P.padLabels = c.pad_labels || {};
    P.dirty = true;
    if (P.onconfig) P.onconfig();
  });
  es.addEventListener("snapshot", (m) => applySnapshot(JSON.parse(m.data)));
  es.addEventListener("ev", (m) => applyEvent(JSON.parse(m.data)));
  es.onopen = () => { P.connected = true; };
  es.onerror = () => { P.connected = false; P.held = {}; P.out = {}; P.axes = {}; P.outAxes = {}; P.dirty = true; };
}

function cssColor(hex) {
  const s = (hex || "").replace("#", "");
  if (s.length !== 6 && s.length !== 8) return "rgba(0,0,0,0)";
  const r = parseInt(s.slice(0, 2), 16), g = parseInt(s.slice(2, 4), 16), b = parseInt(s.slice(4, 6), 16);
  const a = s.length === 8 ? parseInt(s.slice(6, 8), 16) / 255 : 1;
  return `rgba(${r},${g},${b},${a})`;
}

function rgbaOf(hex) {
  const s = (hex || "").replace("#", "");
  const a = s.length === 8 ? parseInt(s.slice(6, 8), 16) : 255;
  return [parseInt(s.slice(0, 2), 16) || 0, parseInt(s.slice(2, 4), 16) || 0, parseInt(s.slice(4, 6), 16) || 0, a];
}

function blend(h1, h2, f) {
  const a = rgbaOf(h1), b = rgbaOf(h2);
  f = Math.max(0, Math.min(1, f));
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * f));
  return `rgba(${c[0]},${c[1]},${c[2]},${c[3] / 255})`;
}

function formatHold(sec) {
  const ms = Math.max(0, Math.floor(sec * 1000 + 1e-6));
  if (ms < 60000) return `${Math.floor(ms / 1000)}.${String(ms % 1000).padStart(3, "0")}`;
  const m = Math.floor(ms / 60000), rest = ms % 60000;
  return `${m}:${String(Math.floor(rest / 1000)).padStart(2, "0")}.${String(rest % 1000).padStart(3, "0")}`;
}

// --- the curved movement arrow (same math as kbm_layout.arrow_curve / fit_arrow)
function posAt(pts, t) {
  if (t <= pts[0][0]) return [pts[0][1], pts[0][2]];
  for (let i = 1; i < pts.length; i++) {
    const [t1, x1, y1] = pts[i];
    if (t1 >= t) {
      const [t0, x0, y0] = pts[i - 1];
      const f = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
      return [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f];
    }
  }
  const l = pts[pts.length - 1];
  return [l[1], l[2]];
}

function arrowCurve(pts) {
  if (pts.length < 2) return null;
  const x3 = pts[pts.length - 1][1], y3 = pts[pts.length - 1][2];
  const mag = Math.hypot(x3, y3);
  if (mag < 2) return null;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
  let b1, b2;
  if (t1 - t0 <= 0) { b1 = [x3 / 3, y3 / 3]; b2 = [2 * x3 / 3, 2 * y3 / 3]; }
  else { b1 = posAt(pts, t0 + (t1 - t0) / 3); b2 = posAt(pts, t0 + 2 * (t1 - t0) / 3); }
  const c1 = [(18 * b1[0] - 9 * b2[0] + 2 * x3) / 6, (18 * b1[1] - 9 * b2[1] + 2 * y3) / 6];
  const c2 = [(-9 * b1[0] + 18 * b2[0] - 5 * x3) / 6, (-9 * b1[1] + 18 * b2[1] - 5 * y3) / 6];
  return { pts: [[0, 0], c1, c2, [x3, y3]], mag };
}

function fitArrow(curve, bw, bh, fullDist) {
  const avail = Math.min(bw, bh) * 0.85;
  const len = avail * Math.max(0.25, Math.min(1, Math.sqrt(curve.mag / Math.max(fullDist || 600, 1))));
  const s = len / curve.mag;
  let xs = curve.pts.map((p) => p[0] * s), ys = curve.pts.map((p) => p[1] * s);
  const ex = Math.max(Math.max(...xs) - Math.min(...xs), 1e-9), ey = Math.max(Math.max(...ys) - Math.min(...ys), 1e-9);
  const k = Math.min(1, bw * 0.9 / ex, bh * 0.9 / ey);
  xs = xs.map((x) => x * k); ys = ys.map((y) => y * k);
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  return xs.map((x, i) => [x - cx + bw / 2, ys[i] - cy + bh / 2]);
}

function drawArrow(ctx, bx, by, bw, bh, style, now) {
  const fade = style.arrow_fade_ms || 0;
  if (fade && (now - P.lastMoveT) * 1000 > fade) return;
  const curve = arrowCurve(P.burst);
  if (!curve) return;
  const p = fitArrow(curve, bw, bh, style.arrow_full_distance).map(([x, y]) => [bx + x, by + y]);
  const width = style.arrow_width || 3, head = Math.max(width * 3.2, 8);
  let dx = 1, dy = 0;
  for (const q of [p[2], p[1], p[0]]) {
    const vx = p[3][0] - q[0], vy = p[3][1] - q[1], n = Math.hypot(vx, vy);
    if (n > 1e-6) { dx = vx / n; dy = vy / n; break; }
  }
  if (P.burstSrc === "m" && style.show_macro_output === false) return;
  const color = cssColor(P.burstSrc === "m" ? (style.macro_color || "#5a9ee0ff") : style.arrow_color);
  const tip = p[3];
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineCap = "round"; ctx.lineJoin = "round";
  ctx.beginPath(); ctx.moveTo(p[0][0], p[0][1]);
  ctx.bezierCurveTo(p[1][0], p[1][1], p[2][0], p[2][1], tip[0] - dx * head * 0.7, tip[1] - dy * head * 0.7);
  ctx.stroke();
  const nx = -dy, ny = dx, bxh = tip[0] - dx * head, byh = tip[1] - dy * head;
  ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(tip[0], tip[1]);
  ctx.lineTo(bxh + nx * head * 0.55, byh + ny * head * 0.55);
  ctx.lineTo(bxh - nx * head * 0.55, byh - ny * head * 0.55); ctx.closePath(); ctx.fill();
  if (style.show_move_text) {
    ctx.fillStyle = cssColor(style.text_color || "#ffffffff");
    ctx.font = `${Math.max(9, bh * 0.12)}px ${style.font_family || "sans-serif"}`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(`${Math.round(curve.mag)} px`, bx + bw / 2, by + bh * 0.92);
  }
}

function axisValue(name, st) {
  const v = P.axes[name] || 0;
  if (v) return [v, "r"];
  if (st && st.show_macro_output === false) return [0, "m"];
  return [P.outAxes[name] || 0, "m"];
}

function arrowAnimating(style, now) {
  const fade = style.arrow_fade_ms || 0;
  return fade && (now - P.lastMoveT) * 1000 <= fade + 50;
}
