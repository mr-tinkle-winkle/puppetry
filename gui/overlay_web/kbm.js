// Draws the keyboard + mouse on a canvas -- the browser twin of
// kbm_paint.paint_kbm (same layout JSON, same style keys).
"use strict";

function roundRect(ctx, x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

function fitText(ctx, text, x, y, w, h, px, family, bold, color) {
  if (!text) return;
  const weight = bold ? "bold " : "";
  ctx.font = `${weight}${px}px ${family || "sans-serif"}`;
  const tw = ctx.measureText(text).width;
  if (tw > w * 0.9 && tw > 0) ctx.font = `${weight}${Math.max(4, px * w * 0.9 / tw)}px ${family || "sans-serif"}`;
  ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, x + w / 2, y + h / 2 + 0.5);
}

function contrast(hex) {
  const [r, g, b] = rgbaOf(hex);
  const light = (Math.max(r, g, b) + Math.min(r, g, b)) / 2;
  return light > 150 ? "#111111" : "#f2f2f2";
}

function drawKbm(ctx, layout, st, now, unit, ox, oy) {
  const u = unit || st.unit || 48, pad = st.padding || 0;
  ox += pad; oy += pad;
  const gap = u * (st.gap ?? 8) / 100, rad = u * (st.radius ?? 14) / 100;
  const fpx = u * (st.font_scale ?? 32) / 100, ow = st.outline_width ?? 1;
  const fade = st.release_fade_ms || 0, delay = (st.timer_delay_ms || 0) / 1000;
  const showMacro = st.show_macro_output !== false;
  const wheelOn = (now - P.wheelT) * 1000 <= (st.wheel_flash_ms ?? 250) && (P.wheelSrc === "r" || showMacro);
  const wheelC = cssColor(P.wheelSrc === "r" ? st.pressed_color : st.macro_color);
  const keyC = cssColor(st.key_color), edge = cssColor(st.key_outline);
  const pressedC = cssColor(st.pressed_color), macroC = cssColor(st.macro_color);
  const srcColor = (src) => (src === "r" ? pressedC : macroC);

  function fillFor(name) {
    if (name in P.held) return [cssColor(st.pressed_color), cssColor(st.pressed_text_color), true];
    if (showMacro && name in P.out) return [cssColor(st.macro_color), contrast(st.macro_color), true];
    const rel = P.released[name];
    if (rel !== undefined && fade && (now - rel) * 1000 < fade) {
      const f = (now - rel) * 1000 / fade;
      return [blend(st.pressed_color, st.key_color, f), blend(st.pressed_text_color, st.text_color, f), false];
    }
    return [keyC, cssColor(st.text_color), false];
  }

  function labelFor(it) {
    const n = it.name;
    if (st.show_timers !== false && n in P.held && now - P.held[n] >= delay) return formatHold(now - P.held[n]);
    if (st.show_timers !== false && showMacro && n in P.out && now - P.out[n] >= delay) return formatHold(now - P.out[n]);
    if (st.label_mode === "none") return "";
    if (st.label_mode === "name" && n) return n;
    return (n && n.startsWith("BTN_") && P.padLabels[n]) || it.label;
  }

  function glowAt(fn, fill) {
    if (!st.glow) return;
    ctx.save(); ctx.globalAlpha = 0.35; fn(Math.max(2, u * 0.08)); ctx.fillStyle = fill; ctx.fill(); ctx.restore();
  }
  function stroke() { if (ow > 0) { ctx.strokeStyle = edge; ctx.lineWidth = ow; ctx.stroke(); } }
  function ellipse(x, y, w, h) { ctx.beginPath(); ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, 2 * Math.PI); }
  function darker(hex, f) {
    const [r, g, b, a] = rgbaOf(hex);
    return `rgba(${Math.round(r / f)},${Math.round(g / f)},${Math.round(b / f)},${a / 255})`;
  }
  function lighter(hex, f) {
    const [r, g, b, a] = rgbaOf(hex);
    return `rgba(${Math.min(255, Math.round(r * f))},${Math.min(255, Math.round(g * f))},${Math.min(255, Math.round(b * f))},${a / 255})`;
  }
  const padLabel = (n) => P.padLabels[n] || "";

  function controllerItem(it, x, y, w, h) {
    const n = it.name;
    if (it.kind === "pad_body") {
      // body + two grips, filled as one shape (nonzero fill), outlined once
      ctx.beginPath();
      const rr = h * 0.3, bh = h * 0.7;
      ctx.moveTo(x + rr, y); ctx.arcTo(x + w, y, x + w, y + bh, rr); ctx.arcTo(x + w, y + bh, x, y + bh, rr);
      ctx.arcTo(x, y + bh, x, y, rr); ctx.arcTo(x, y, x + w, y, rr); ctx.closePath();
      for (const gx of [x + w * 0.02, x + w - w * 0.32]) {
        ctx.moveTo(gx + w * 0.3, y + h * 0.65);
        ctx.ellipse(gx + w * 0.15, y + h * 0.65, w * 0.15, h * 0.35, 0, 0, 2 * Math.PI);
      }
      ctx.fillStyle = darker(st.key_color, 1.35); ctx.fill("nonzero");
      return;
    }
    let [fill, tcol, pressed] = fillFor(n);
    if (it.kind === "trigger") {
      let [v, src] = axisValue(it.axis, st);
      if (n in P.held && v < 1) { v = 1; src = "r"; }
      const rr = h * 0.3;
      roundRect(ctx, x, y, w, h, rr); ctx.fillStyle = keyC; ctx.fill(); stroke();
      if (v > 0.001) {
        ctx.save(); roundRect(ctx, x, y, w, h, rr); ctx.clip();
        ctx.fillStyle = srcColor(src); ctx.fillRect(x, y + h * (1 - v), w, h * v); ctx.restore();
      }
      fitText(ctx, n in P.held ? labelFor(it) : padLabel(n), x, y, w, h, fpx, st.font_family, st.bold,
              v > 0.5 ? cssColor(st.pressed_text_color) : cssColor(st.text_color));
      return;
    }
    if (it.kind === "stick") {
      ellipse(x, y, w, h); ctx.fillStyle = darker(st.key_color, 1.2); ctx.fill(); stroke();
      const [vx, sx] = axisValue(it.ax, st), [vy, sy] = axisValue(it.ay, st);
      const k = w * 0.62, travel = (w - k) / 2;
      const kx = x + w / 2 - k / 2 + vx * travel, ky = y + h / 2 - k / 2 + vy * travel;
      const moved = Math.abs(vx) > 0.12 || Math.abs(vy) > 0.12;
      const kfill = pressed ? fill : lighter(st.key_color, 1.18);
      if (pressed) glowAt((g) => ellipse(kx - g, ky - g, k + 2 * g, k + 2 * g), kfill);
      ellipse(kx, ky, k, k); ctx.fillStyle = kfill; ctx.fill();
      if (moved) { ctx.strokeStyle = srcColor(Math.abs(vx) >= Math.abs(vy) ? sx : sy); ctx.lineWidth = Math.max(2, u * 0.1); ctx.stroke(); }
      else stroke();
      fitText(ctx, labelFor(it), kx, ky, k, k, fpx * 0.9, st.font_family, st.bold, pressed ? tcol : cssColor(st.text_color));
      return;
    }
    if (it.kind === "dpad") {
      const [ha, want] = it.hat;
      const [hv, hsrc] = axisValue(ha, st);
      if (!pressed && ((want < 0 && hv < -0.5) || (want > 0 && hv > 0.5))) {
        fill = srcColor(hsrc); tcol = cssColor(st.pressed_text_color); pressed = true;
      }
      if (pressed) glowAt((g) => roundRect(ctx, x - g, y - g, w + 2 * g, h + 2 * g, w * 0.18 + g), fill);
      roundRect(ctx, x, y, w, h, w * 0.18); ctx.fillStyle = fill; ctx.fill(); stroke();
      fitText(ctx, n in P.held ? labelFor(it) : padLabel(n), x, y, w, h, fpx * 0.9, st.font_family, st.bold, tcol);
      return;
    }
    if (it.kind === "pad_btn") {
      if (pressed) glowAt((g) => ellipse(x - g, y - g, w + 2 * g, h + 2 * g), fill);
      ellipse(x, y, w, h); ctx.fillStyle = fill; ctx.fill(); stroke();
      fitText(ctx, labelFor(it), x, y, w, h, fpx, st.font_family, st.bold, tcol);
      return;
    }
    const rr = Math.min(w, h) * 0.45;          // shoulder / small
    if (pressed) glowAt((g) => roundRect(ctx, x - g, y - g, w + 2 * g, h + 2 * g, rr + g), fill);
    roundRect(ctx, x, y, w, h, rr); ctx.fillStyle = fill; ctx.fill(); stroke();
    fitText(ctx, labelFor(it), x, y, w, h, fpx * (it.kind === "small" ? 0.8 : 1), st.font_family, st.bold, tcol);
  }
  const PAD_KINDS = new Set(["pad_body", "trigger", "stick", "dpad", "pad_btn", "shoulder", "small"]);

  for (const it of layout.items) {
    let x = ox + it.x * u, y = oy + it.y * u, w = it.w * u, h = it.h * u;
    if (PAD_KINDS.has(it.kind)) { controllerItem(it, x, y, w, h); continue; }
    if (it.kind === "arrow_box") { if (st.show_arrow !== false) drawArrow(ctx, x, y, w, h, st, now); continue; }
    if (it.kind === "mouse_body") {
      roundRect(ctx, x, y, w, h, w * 0.42); ctx.fillStyle = keyC; ctx.fill();
      if (ow > 0) { ctx.strokeStyle = edge; ctx.lineWidth = ow; ctx.stroke(); }
      continue;
    }
    if (it.kind === "key") { w -= gap; h -= gap; }
    let [fill, tcol, pressed] = fillFor(it.name);
    if (it.kind === "wheel" && wheelOn && P.wheelDir) { fill = wheelC; tcol = contrast(P.wheelSrc === "r" ? st.pressed_color : st.macro_color); pressed = true; }
    const rr = it.kind === "key" ? rad : Math.min(w, h) * 0.3;
    if (pressed && st.glow && it.kind !== "wheel") {
      const g = Math.max(2, u * 0.08);
      ctx.save(); ctx.globalAlpha = 0.35; roundRect(ctx, x - g, y - g, w + 2 * g, h + 2 * g, rr + g);
      ctx.fillStyle = fill; ctx.fill(); ctx.restore();
    }
    roundRect(ctx, x, y, w, h, rr);
    ctx.fillStyle = it.kind === "key" || pressed ? fill : keyC; ctx.fill();
    if (ow > 0) { ctx.strokeStyle = edge; ctx.lineWidth = ow; ctx.stroke(); }
    if (it.kind === "wheel") {
      if (wheelOn && P.wheelDir) fitText(ctx, P.wheelDir > 0 ? "▲" : "▼", x, y, w, h, w * 0.8, st.font_family, false, tcol);
      continue;
    }
    fitText(ctx, labelFor(it), x, y, w, h, fpx, st.font_family, st.bold, tcol);
  }
}

function kbmAnimating(st, now) {
  if (st.show_timers !== false && (Object.keys(P.held).length || (st.show_macro_output !== false && Object.keys(P.out).length))) return true;
  const fade = st.release_fade_ms || 0;
  if (fade && Object.values(P.released).some((t) => (now - t) * 1000 < fade)) return true;
  if ((now - P.wheelT) * 1000 <= (st.wheel_flash_ms ?? 250) + 50) return true;
  return arrowAnimating(st, now);
}
