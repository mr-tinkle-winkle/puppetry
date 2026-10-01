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
  motion: [], clicks: [], wheels: [], views: {},
  offset: 0, dirty: true, connected: false,
  onconfig: null,
};
const MOVE_GAP = 0.15, HISTORY_S = 6.0;
const MOUSE_BTNS = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA"];

function nowS() { return performance.now() / 1000; }
function toClient(t) { return t - P.offset; }

function applyEvent(ev) {
  const t = toClient(ev.t);
  if (ev.e === "kd" || ev.e === "ku") {
    const d = ev.s === "r" ? P.held : P.out;
    if (ev.e === "kd" && MOUSE_BTNS.includes(ev.k) && !(ev.k in d)) {
      P.clicks.push([t, ev.k, ev.s]); if (P.clicks.length > 64) P.clicks.splice(0, 32);
    }
    if (ev.e === "kd") { if (!(ev.k in d)) d[ev.k] = t; delete P.released[ev.k]; }
    else if (ev.k in d) { delete d[ev.k]; if (ev.s === "r") P.released[ev.k] = t; }
  } else if (ev.e === "ax") {
    const d = ev.s === "r" ? P.axes : P.outAxes;
    if (ev.v) d[ev.a] = ev.v; else delete d[ev.a];
  } else if (ev.e === "mv") {
    if (!P.burst.length || t - P.lastMoveT > MOVE_GAP || ev.s !== P.burstSrc) { P.burst = [[t, 0, 0]]; P.burstSrc = ev.s; }
    const last = P.burst[P.burst.length - 1];
    P.burst.push([t, last[1] + ev.dx, last[2] + ev.dy]);
    addMotion(t, ev.dx, ev.dy, ev.s);
    if (P.burst.length > 4000) P.burst = [P.burst[0]].concat(P.burst.slice(1).filter((_, i) => i % 2 === 0));
    P.lastMoveT = t;
  } else if (ev.e === "wh" && ev.n) {
    P.wheelDir = ev.n > 0 ? 1 : -1; P.wheelT = t; P.wheelSrc = ev.s;
    P.wheels.push([t, P.wheelDir, ev.s]); if (P.wheels.length > 64) P.wheels.splice(0, 32);
  }
  P.dirty = true;
}

// same rules as kbm_layout.KbmState.move
function addMotion(t, dx, dy, src) {
  const h = P.motion;
  const last = h.length ? h[h.length - 1] : null;
  const mx = last ? last[1] : 0, my = last ? last[2] : 0;
  if (!last || t - last[0] > MOVE_GAP) h.push([t - 1e-4, mx, my, src]);
  h.push([t, mx + dx, my + dy, src]);
  if (h.length > 3000 || (h[0][0] < t - HISTORY_S && h.length > 2)) {
    let cut = 0;
    while (cut < h.length - 2 && h[cut][0] < t - HISTORY_S) cut++;
    h.splice(0, Math.max(cut, h.length - 3000));
  }
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
  P.motion = (s.motion || []).map(([rel, x, y, src]) => [n + rel, x, y, src]);
  P.clicks = (s.clicks || []).map(([rel, b, src]) => [n + rel, b, src]);
  P.wheels = (s.wheels || []).map(([rel, d, src]) => [n + rel, d, src]);
  P.dirty = true;
}

// A CSS font-family list for a font name (font_catalog.css_stack's twin): quoted, so names
// like "Press Start 2P" parse, with a generic fallback.
const GENERIC_FONTS = new Set(["sans-serif", "serif", "monospace", "cursive", "fantasy", "system-ui"]);
function fontStack(family) {
  family = (family || "sans-serif").trim();
  return GENERIC_FONTS.has(family) ? family : `"${family.replace(/"/g, "")}", sans-serif`;
}
// bundled fonts (/fonts.css) load on first use: redraw once they arrive
if (typeof document !== "undefined" && document.fonts) {
  document.fonts.addEventListener("loadingdone", () => { P.dirty = true; if (P.onfonts) P.onfonts(); });
}
function loadFont(st) {
  if (typeof document === "undefined" || !document.fonts || !st) return;
  for (const b of ["", "bold "]) document.fonts.load(`${b}16px ${fontStack(st.font_family)}`).then(() => { P.dirty = true; if (P.onfonts) P.onfonts(); }, () => {});
}

function connect() {
  const m = location.pathname.match(/^\/el\/([^/]+)/);       // an element page: /el/<id>
  const es = new EventSource("/events" + (m ? "?el=" + encodeURIComponent(decodeURIComponent(m[1])) : ""));
  es.addEventListener("config", (m) => {
    const c = JSON.parse(m.data);
    P.style = c.style || {}; P.layout = c.layout || null; P.labels = c.labels || {};
    P.padLabels = c.pad_labels || {};
    loadFont(P.style);
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
  return drawMotion(ctx, bx, by, bw, bh, style, now, "comet", "arrow", "head");
}

function drawArrowOld(ctx, bx, by, bw, bh, style, now) {
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
    ctx.font = `${Math.max(9, bh * 0.12)}px ${fontStack(style.font_family)}`;
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
  return motionAnimating(style, now);
}

function arrowAnimatingOld(style, now) {
  const fade = style.arrow_fade_ms || 0;
  return fade && (now - P.lastMoveT) * 1000 <= fade + 50;
}


// ===========================================================================
// Movement views -- a line-for-line port of kbm_layout.motion_frame (a test
// runs both on the same input and compares).
// ===========================================================================
const RING_S = 0.35, CHEVRON_S = 0.5, ARC_SPAN = 60.0;

function interpAt(h, t) {
  if (!h.length) return [0, 0, "r"];
  if (t <= h[0][0]) return [h[0][1], h[0][2], h[0][3]];
  const L = h[h.length - 1];
  if (t >= L[0]) return [L[1], L[2], L[3]];
  let lo = 0, hi = h.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (h[mid][0] <= t) lo = mid; else hi = mid; }
  const [t0, x0, y0] = h[lo], [t1, x1, y1, s1] = h[hi];
  const f = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
  return [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, s1];
}

function windowPts(h, t0) {
  if (!h.length) return [];
  let pts = h.filter((p) => p[0] > t0);
  const [x, y, src] = interpAt(h, t0);
  if (h[0][0] < t0) pts = [[t0, x, y, src]].concat(pts);
  return pts.length ? pts : [h[h.length - 1]];
}

function easeTo(cur, target, dt, tau) { return dt <= 0 ? target : cur + (target - cur) * (1 - Math.exp(-dt / tau)); }

function motionFrame(state, kind, box, style, now, viewKey, center) {
  const h = state.motion;
  const view = state.views[viewKey || kind] || (state.views[viewKey || kind] = {});
  const dt = now - (view.t ?? now);
  view.t = now;
  const T = +(style.trail_seconds ?? 1.0);
  const wmax = box * (+(style.trail_width ?? 4.0)) / 100;
  const dotR = wmax * 1.5, half = box / 2 - dotR * 1.75 - 1;  // room for the held-button ring
  const span = (+(style.pad_fraction ?? 80)) / 100 * (+(style.screen_height ?? 1080));
  const scale0 = box / Math.max(span, 1);
  const auto = style.auto_zoom !== false;
  let pts = windowPts(h, now - T);
  let mapped = [];
  if (kind === "joystick") {
    const vmax = +(style.joystick_speed ?? 3000);
    for (let k = 12; k >= 0; k--) {
      const t = now - k * 0.02;
      const [x1, y1, src] = interpAt(h, t), [x0, y0] = interpAt(h, t - 0.05);
      const vx = (x1 - x0) / 0.05, vy = (y1 - y0) / 0.05, sp = Math.hypot(vx, vy);
      const r = sp > 0 ? Math.tanh(sp / vmax) * half : 0;
      const ux = sp > 0 ? vx / sp : 0, uy = sp > 0 ? vy / sp : 0;
      mapped.push([box / 2 + ux * r, box / 2 + uy * r, src === "m" ? 1 : 0]);
    }
  } else {
    if (!pts.length) pts = [[now, 0, 0, "r"]];
    let cx, cy;
    if (kind === "mousepad") {
      const ax = h.length ? h[h.length - 1][1] : 0, ay = h.length ? h[h.length - 1][2] : 0;
      let ox = view.ox ?? (h.length ? h[0][1] : ax), oy = view.oy ?? (h.length ? h[0][2] : ay);
      const idle = now - (h.length ? h[h.length - 1][0] : now);
      if (idle > +(style.recenter_s ?? 1.0)) { ox = easeTo(ox, ax, dt, 0.3); oy = easeTo(oy, ay, dt, 0.3); }
      const hs = half / scale0;
      if (!auto) {
        if (ax - ox > hs) ox = ax - hs;
        if (ox - ax > hs) ox = ax + hs;
        if (ay - oy > hs) oy = ay - hs;
        if (oy - ay > hs) oy = ay + hs;
      }
      let z = 1.0;
      if (auto) {
        // zoom out at once to keep the WHOLE trail in view (restarting the "zoom back in"
        // timer); once it runs out, pan to the trail's middle and zoom in as far as it allows
        const allp = pts.concat([[now, ax, ay, "r"]]);
        const extent = (cx_, cy_) => Math.max(...allp.map((p) => Math.max(Math.abs(p[1] - cx_), Math.abs(p[2] - cy_)))) * scale0;
        z = view.z ?? 1.0;
        let need = extent(ox, oy);
        let allowed = need > 1e-9 ? Math.min(1, half / need) : 1;
        if (allowed < z - 1e-9) { z = allowed; view.growT = now; }
        else if (z < 1 && now - (view.growT ?? now) >= +(style.unzoom_s ?? 0.6)) {
          const xs = allp.map((p) => p[1]), ys = allp.map((p) => p[2]);
          ox = easeTo(ox, (Math.min(...xs) + Math.max(...xs)) / 2, dt, 0.35);
          oy = easeTo(oy, (Math.min(...ys) + Math.max(...ys)) / 2, dt, 0.35);
          need = extent(ox, oy);
          allowed = need > 1e-9 ? Math.min(1, half / need) : 1;
          z = Math.min(allowed, easeTo(z, allowed, dt, 0.35));
          if (z > 0.999) z = 1;
        } else z = Math.min(z, allowed);
      }
      view.ox = ox; view.oy = oy; view.z = z;
      cx = ox; cy = oy;
      if (!auto) {                                  // fixed scale: drop what has left the pad
        let i = 0;
        while (i < pts.length - 1 && pts.slice(i).some((p) => Math.max(Math.abs(p[1] - ox), Math.abs(p[2] - oy)) * scale0 > half)) i++;
        pts = pts.slice(i);
      }
    } else {
      // comet: always at the true scale, so a trail's length shows speed; what would
      // leave the view is dropped, oldest first
      let i = 0;
      while (i < pts.length - 1) {
        const c = center !== "head" ? pts[i] : pts[pts.length - 1];
        if (pts.slice(i).every((p) => Math.max(Math.abs(p[1] - c[1]), Math.abs(p[2] - c[2])) * scale0 <= half)) break;
        i++;
      }
      pts = pts.slice(i);
      const c = center !== "head" ? pts[0] : pts[pts.length - 1];
      cx = c[1]; cy = c[2];
    }
const sc = scale0 * (auto && kind === "mousepad" ? (view.z ?? 1) : 1);
    mapped = pts.map((p) => [box / 2 + (p[1] - cx) * sc, box / 2 + (p[2] - cy) * sc, p[3] === "m" ? 1 : 0]);
  }
  const n = mapped.length, trail = [];
  for (let i = 0; i < n; i++) {
    const lo = Math.max(0, i - 2), hi = Math.min(n, i + 3);
    let share = 0;
    for (let j = lo; j < hi; j++) share += mapped[j][2];
    share /= (hi - lo);
    const f = n > 1 ? i / (n - 1) : 1;
    trail.push([mapped[i][0], mapped[i][1], wmax * (0.15 + 0.85 * f), n > 1 ? Math.min(1, f / 0.25) : 1, share]);
  }
  const dot = trail.length ? [trail[n - 1][0], trail[n - 1][1], dotR, trail[n - 1][4]] : [box / 2, box / 2, dotR, 0];
  const showMacro = style.show_macro_output !== false;
  let held = null;
  for (const b of MOUSE_BTNS) {
    if (b in state.held) { held = "r"; break; }
    if (b in state.out && showMacro) held = "m";
  }
  const rings = [], R = box * 0.18;
  for (const [t, b, src] of state.clicks) {
    const a = (now - t) / RING_S;
    if (!(a >= 0 && a <= 1) || (src === "m" && !showMacro)) continue;
    const out = { alpha: 1 - a, width: Math.max(1.5, wmax * 0.5), src, arcs: null };
    if (b === "BTN_RIGHT") out.r = dotR + (1 - a) * R;
    else {
      out.r = dotR + a * R;
      if (b === "BTN_MIDDLE") out.arcs = [[90, ARC_SPAN], [270, ARC_SPAN]];
      else if (b === "BTN_SIDE" || b === "BTN_EXTRA") {     // back -> left, forward -> right (or inverted)
        const left = (b === "BTN_SIDE") !== !!style.invert_side_rings;
        out.arcs = [[left ? 180 : 0, ARC_SPAN]];
      }
    }
    rings.push(out);
  }
  const recent = state.wheels.filter((w) => now - w[0] >= 0 && now - w[0] <= CHEVRON_S && (w[2] === "r" || showMacro)).slice(-6);
  const chevrons = recent.map(([t, d, src], i) => {
    const a = (now - t) / CHEVRON_S, size = wmax * 1.6;
    const dist = dotR + size * (0.8 + 0.9 * (recent.length - 1 - i)) + a * size;
    return { x: dot[0], y: d > 0 ? dot[1] - dist : dot[1] + dist, size, dir: d, alpha: 1 - a, src };
  });
  return { trail, dot, held, rings, chevrons };
}

function mixColor(h1, h2, f, alpha) {
  const a = rgbaOf(h1), b = rgbaOf(h2);
  f = Math.max(0, Math.min(1, f));
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * f));
  return `rgba(${c[0]},${c[1]},${c[2]},${(c[3] / 255) * Math.max(0, Math.min(1, alpha ?? 1))})`;
}

function drawMotion(ctx, bx, by, bw, bh, style, now, kind, viewKey, center) {
  const size = Math.min(bw, bh), ox = bx + (bw - size) / 2, oy = by + (bh - size) / 2;
  const fr = motionFrame(P, kind, size, style, now, viewKey || kind, center || "head");
  const real = style.arrow_color || "#e0955aff";
  const macro = style.show_macro_output === false ? real : (style.macro_color || "#5a9ee0ff");
  ctx.save();
  ctx.beginPath(); ctx.rect(ox, oy, size, size); ctx.clip();
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  const tr = fr.trail;
  for (let i = 1; i < tr.length; i++) {
    const [x0, y0, w0, a0, m0] = tr[i - 1], [x1, y1, w1, a1, m1] = tr[i];
    if (Math.abs(x1 - x0) < 0.01 && Math.abs(y1 - y0) < 0.01) continue;
    ctx.strokeStyle = mixColor(real, macro, (m0 + m1) / 2, (a0 + a1) / 2);
    ctx.lineWidth = (w0 + w1) / 2;
    ctx.beginPath(); ctx.moveTo(ox + x0, oy + y0); ctx.lineTo(ox + x1, oy + y1); ctx.stroke();
  }
  const [dx, dy, dr, dm] = fr.dot, cx = ox + dx, cy = oy + dy;
  ctx.fillStyle = mixColor(real, macro, dm, 1);
  ctx.beginPath(); ctx.arc(cx, cy, dr, 0, 2 * Math.PI); ctx.fill();
  if (fr.held) {
    ctx.strokeStyle = cssColor(fr.held === "r" ? real : macro); ctx.lineWidth = Math.max(1.5, dr * 0.3);
    ctx.beginPath(); ctx.arc(cx, cy, dr * 1.55, 0, 2 * Math.PI); ctx.stroke();
  }
  for (const ring of fr.rings) {
    ctx.strokeStyle = mixColor(real, macro, ring.src === "m" ? 1 : 0, ring.alpha); ctx.lineWidth = ring.width;
    if (!ring.arcs) { ctx.beginPath(); ctx.arc(cx, cy, ring.r, 0, 2 * Math.PI); ctx.stroke(); }
    else for (const [mid, span] of ring.arcs) {
      // math angles (0 = right, 90 = up) -> canvas (y down): negate
      const a0 = -(mid + span / 2) * Math.PI / 180, a1 = -(mid - span / 2) * Math.PI / 180;
      ctx.beginPath(); ctx.arc(cx, cy, ring.r, a0, a1); ctx.stroke();
    }
  }
  for (const ch of fr.chevrons) {
    ctx.strokeStyle = mixColor(real, macro, ch.src === "m" ? 1 : 0, ch.alpha);
    ctx.lineWidth = Math.max(1.5, ch.size * 0.22);
    const x = ox + ch.x, y = oy + ch.y, tip = ch.dir > 0 ? -ch.size * 0.35 : ch.size * 0.35;
    ctx.beginPath(); ctx.moveTo(x - ch.size * 0.5, y - tip); ctx.lineTo(x, y + tip); ctx.lineTo(x + ch.size * 0.5, y - tip); ctx.stroke();
  }
  ctx.restore();
}

function motionAnimating(style, now) {
  if (P.motion.length && now - P.motion[P.motion.length - 1][0] <= (+(style.trail_seconds ?? 1.0)) + 1.5) return true;
  return P.clicks.some((c) => now - c[0] <= RING_S) || P.wheels.some((w) => now - w[0] <= CHEVRON_S);
}
