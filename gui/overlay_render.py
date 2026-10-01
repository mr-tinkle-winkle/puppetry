"""
Turns recorded input (the layered replay-buffer file) into video: the same
keyboard/mouse picture as the OBS page, drawn frame by frame with the
shared painter (kbm_paint) and piped straight into ffmpeg.

    render     time range -> transparent overlay video (.mov qtrle / .webm vp9 / .mkv ffv1)
    apply      overlay video + clip -> clip with the overlay on top
    composite  clip + input buffer -> clip with the overlay on top, one pass

Times are Unix seconds (the buffer's own clock). For a replay-buffer clip,
the clip ends when OBS was told to save it, so start = clip_end - duration.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import kbm_layout as kl  # noqa: E402
import overlay_config as oc  # noqa: E402

POSITIONS = ("top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right", "center")


class RenderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Reading the buffer
# ---------------------------------------------------------------------------
def load_buffer(path) -> tuple:
    """-> (header, events). Skips a final line that's still being written
    and anything unparsable."""
    raw = Path(path).read_text()
    lines = raw.split("\n")
    if not raw.endswith("\n"):
        lines = lines[:-1]               # partial last line
    header, events = {}, []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if obj.get("format") == "puppetry-input-buffer":
            header = obj
        elif "t" in obj and "e" in obj:
            events.append(obj)
    events.sort(key=lambda e: e["t"])
    return header, events


class Timeline:
    """Replays events into a KbmState as frame times advance."""

    def __init__(self, header: dict, events: list, offset_s: float = 0.0):
        self.events = events
        self.offset = offset_s
        self.i = 0
        self.state = kl.KbmState()
        for k, t in (header.get("held_at_start") or {}).items():
            self.state.key(k, True, t + offset_s)
        start = header.get("start", 0.0) + offset_s
        for src in ("r", "m"):
            for a, v in ((header.get("axes_at_start") or {}).get(src) or {}).items():
                self.state.axis(a, v, start, src)

    def advance(self, t: float) -> None:
        ev, st = self.events, self.state
        while self.i < len(ev) and ev[self.i]["t"] + self.offset <= t:
            e = ev[self.i]
            et = e["t"] + self.offset
            src = e.get("s", "r")
            if e["e"] in ("kd", "ku"):
                st.key(e["k"], e["e"] == "kd", et, src)
            elif e["e"] == "mv":
                st.move(e["dx"], e["dy"], et, src)
            elif e["e"] == "wh":
                st.wheel(e["n"], et, src)
            elif e["e"] == "ax":
                st.axis(e["a"], e["v"], et, src)
            self.i += 1


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------
MODES = ("full", "simple", "movement")      # plus "el:<element id>" (one element of the scene)


def element_for(mode: str, cfg: dict) -> "str | None":
    """Mode -> scene element id: "el:<id>", a bare element id, or (older
    pieces) a type -- "keyboard", "mouse", "controller" -> the first element
    of that type. None for full / simple / movement."""
    if mode in ("full", "scene", "simple", "movement", "mouse_arrow"):
        return None
    els = cfg["scene"].get("elements", [])
    if mode.startswith("el:"):
        want = mode[3:]
        if not any(e.get("id") == want for e in els):
            raise RenderError(f"no element {want!r} in the scene ({', '.join(e.get('id', '?') for e in els)})")
        return want
    for e in els:
        if e.get("id") == mode:
            return mode
    for e in els:
        if e.get("type") == mode:
            return e["id"]
    raise RenderError(f"unknown overlay mode {mode!r} (full, simple, movement, el:<id>; elements: "
                      f"{', '.join(e.get('id', '?') for e in els)})")


class FrameMaker:
    """mode: "full" (the whole layout), "el:<id>" / an element id / an
    element type (one element; a type the layout lacks gets a default one),
    "simple" (text list) or "movement" (one movement view, per the movement
    page's style)."""

    def __init__(self, mode: str, cfg: dict, scale: float = 1.0):
        from PySide6.QtGui import QGuiApplication, QImage
        self._app = QGuiApplication.instance() or QGuiApplication([])
        self.mode, self.cfg, self.scale = mode, cfg, scale
        if mode == "mouse_arrow":           # (pre-Session-18 name for "movement")
            mode = self.mode = "movement"
        try:
            el = element_for(mode, cfg)
        except RenderError:
            if mode not in kl.ELEMENT_TYPES:
                raise
            # a piece of a type the layout doesn't show (e.g. "controller"
            # for a clip when the visualizer has none): draw a default one
            cfg = dict(cfg, scene={"elements": [kl.default_element({"elements": []}, mode)]})
            self.cfg, el = cfg, mode
        if mode in ("full", "scene") or el is not None:
            self.mode = "picture"
            self.style = dict(cfg["style"])
            self.layout = kl.build_scene(cfg["scene"], only=el)
            w, h = kl.layout_pixel_size(self.layout, self.style)
        elif mode == "simple":
            self.mode = "simple"
            self.style = dict(cfg["simple_style"])
            w, h = int(cfg["simple"].get("width", 900)), int(max(20, self.style["font_px"] * 1.6))
        elif mode == "movement":
            self.mode = "movement"
            self.style = dict(cfg["movement_style"])
            w = h = int(self.style["size"])
        else:
            raise RenderError(f"unknown overlay mode {mode!r}")
        self.w, self.h = max(2, int(w * scale)) // 2 * 2, max(2, int(h * scale)) // 2 * 2   # even for encoders
        self.img = QImage(self.w, self.h, QImage.Format_RGBA8888_Premultiplied)
        self._QImage = QImage
        self._last_key = None
        self._last_bytes = None

    def frame(self, state: "kl.KbmState", t: float) -> bytes:
        from kbm_paint import needs_animation
        style = self.style
        if self.mode == "picture":
            animating = needs_animation(state, style, t)
        elif self.mode == "movement":
            animating = kl.motion_animating(state, style, t)
        else:
            animating = bool(style.get("show_timers") and (state.held or state.out_held)) or (t - state.wheel_t) < 0.45 or bool(
                style.get("history"))
        key = state.revision
        if not animating and key == self._last_key and self._last_bytes is not None:
            return self._last_bytes
        self._paint(state, t)
        img = self.img.convertToFormat(self._QImage.Format_RGBA8888)
        data = bytes(img.constBits())[: self.w * self.h * 4]
        self._last_key, self._last_bytes = key, data
        return data

    def _paint(self, state, t) -> None:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QPainter
        from kbm_paint import paint_arrow, paint_kbm, qcolor
        self.img.fill(0)
        p = QPainter(self.img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.scale(self.scale, self.scale)
        w, h = self.w / self.scale, self.h / self.scale
        if self.mode == "picture":
            paint_kbm(p, self.layout, self.style, state, t)
        elif self.mode == "movement":
            bg = qcolor(self.style.get("background", "#00000000"))
            if bg.alpha():
                p.fillRect(QRectF(0, 0, w, h), bg)
            from kbm_paint import paint_motion
            paint_motion(p, QRectF(0, 0, w, h), state, self.style, t, self.style.get("motion", "comet"),
                         "movement", self.style.get("center", "head"))
        else:
            self._paint_simple(p, state, t, w, h)
        p.end()

    def _paint_simple(self, p, state, t, w, h) -> None:
        paint_simple(p, state, self.style, t, w, h)


def paint_simple(p, state, s: dict, t: float, w: float, h: float) -> None:
    """The simple page's text list, drawn with QPainter (renderer + preview):
    each part in its source's color (your input / a macro's output)."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath, QPen
    from kbm_paint import qcolor
    bg = qcolor(s.get("background", "#00000000"))
    if bg.alpha():
        p.fillRect(QRectF(0, 0, w, h), bg)
    parts = simple_parts(state, s, t)
    sep = s.get("separator", " + ")
    if not parts:
        empty = s.get("empty_text", "")
        if not empty:
            return
        parts = [(empty, "")]
    f = QFont(s.get("font_family") or "sans-serif")
    f.setPixelSize(int(s.get("font_px", 36)))
    f.setBold(bool(s.get("bold")))
    fm = QFontMetricsF(f)
    runs = []                                   # (text, color)
    by_src = s.get("color_by_source", True)
    for i, (txt, src) in enumerate(parts):
        if i:
            runs.append((sep, s["text_color"]))
        color = s["text_color"] if not by_src or not src else (s["real_color"] if src == "r" else s["macro_color"])
        runs.append((txt, color))
    tw = sum(fm.horizontalAdvance(r[0]) for r in runs)
    x = {"left": 8, "center": (w - tw) / 2, "right": w - tw - 8}.get(s.get("align", "left"), 8)
    y = (h + fm.ascent() - fm.descent()) / 2
    o = float(s.get("outline_px", 0))
    paths = []
    for txt, color in runs:
        path = QPainterPath()
        path.addText(QPointF(x, y), f, txt)
        paths.append((path, color))
        x += fm.horizontalAdvance(txt)
    if o:
        p.setPen(QPen(qcolor(s["outline_color"]), o * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        for path, _c in paths:
            p.drawPath(path)
    p.setPen(Qt.NoPen)
    for path, color in paths:
        p.setBrush(qcolor(color))
        p.drawPath(path)


MOUSE_SHORT = {"BTN_LEFT": "LMB", "BTN_RIGHT": "RMB", "BTN_MIDDLE": "MMB", "BTN_SIDE": "MB4", "BTN_EXTRA": "MB5"}


def simple_name(k: str, style: dict) -> str:
    if k in MOUSE_SHORT:
        return MOUSE_SHORT[k]
    if k in kl.PAD_BUTTONS and style.get("names", "label") != "name":
        return kl.PAD_LABELS.get(style.get("controller_labels", "xbox"), kl.PAD_LABELS["xbox"]).get(k, k)
    if style.get("names", "label") == "name":
        return k
    lab = kl.LABELS.get(k)
    if lab == "":
        return "Space" if k == "KEY_SPACE" else k.replace("KEY_", "")
    return lab or k.replace("KEY_", "").replace("BTN_", "")


def simple_parts(state: "kl.KbmState", style: dict, t: float) -> list:
    """[(text, src)] -- what the simple OBS page shows, oldest press first;
    src "r" = your input, "m" = a macro's output."""
    items = [(k, pt, "r") for k, pt in state.held.items()]
    if style.get("show_macro_output", True):
        items += [(k, pt, "m") for k, pt in state.out_held.items() if k not in state.held]
    parts = []
    for k, pt, src in sorted(items, key=lambda x: x[1]):
        if not style.get("show_mouse_buttons", True) and k in MOUSE_SHORT:
            continue
        if not style.get("show_controller", True) and k in kl.PAD_BUTTONS:
            continue
        txt = simple_name(k, style)
        if style.get("show_timers"):
            txt += " " + kl.format_hold(t - pt)
        parts.append((txt, src))
    hist = style.get("history", 0)
    if hist:
        for k, rt in sorted(state.released.items(), key=lambda kv: kv[1]):
            if (t - rt) * 1000 <= hist and k not in state.held:
                if not style.get("show_mouse_buttons", True) and k in MOUSE_SHORT:
                    continue
                parts.append((simple_name(k, style), "r"))
    if style.get("show_wheel", True) and state.wheel_dir and t - state.wheel_t < 0.4 and (
            state.wheel_src == "r" or style.get("show_macro_output", True)):
        parts.append(("Wheel \u25b2" if state.wheel_dir > 0 else "Wheel \u25bc", state.wheel_src))
    return parts


def simple_text(state: "kl.KbmState", style: dict, t: float) -> str:
    """The same text the simple OBS page shows."""
    parts = simple_parts(state, style, t)
    return style.get("separator", " + ").join(p for p, _s in parts) if parts else style.get("empty_text", "")


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
def ffmpeg_bin() -> str:
    exe = os.environ.get("PUPPETRY_FFMPEG") or shutil.which("ffmpeg")
    if not exe:
        raise RenderError("ffmpeg not found")
    return exe


def probe(clip) -> dict:
    """-> {"duration", "fps", "width", "height"} of a video file."""
    exe = shutil.which("ffprobe") or str(Path(ffmpeg_bin()).with_name("ffprobe"))
    out = subprocess.run([exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(clip)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RenderError(f"ffprobe couldn't read {clip}: {out.stderr.strip()}")
    d = json.loads(out.stdout)
    st = d["streams"][0]
    num, den = (st.get("r_frame_rate") or "60/1").split("/")
    return {"duration": float(d["format"]["duration"]), "fps": float(num) / float(den or 1),
            "width": int(st["width"]), "height": int(st["height"])}


def overlay_xy(position: str, margin: int) -> tuple:
    m = str(margin)
    return {"top-left": (m, m), "top-center": ("(W-w)/2", m), "top-right": (f"W-w-{m}", m),
            "bottom-left": (m, f"H-h-{m}"), "bottom-center": ("(W-w)/2", f"H-h-{m}"),
            "bottom-right": (f"W-w-{m}", f"H-h-{m}"), "center": ("(W-w)/2", "(H-h)/2")}[position]


def _encoder_args(out: Path) -> list:
    ext = out.suffix.lower()
    if ext == ".webm":
        return ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "32", "-row-mt", "1"]
    if ext == ".mkv":
        return ["-c:v", "ffv1", "-pix_fmt", "yuva420p"]
    if ext == ".mov":
        return ["-c:v", "qtrle", "-pix_fmt", "argb"]
    raise RenderError("overlay output must be .mov (qtrle), .webm (vp9) or .mkv (ffv1) -- formats that keep transparency")


def _frames(maker: FrameMaker, timeline: Timeline, start: float, end: float, fps: float):
    n = max(1, int(round((end - start) * fps)))
    for i in range(n):
        t = start + i / fps
        timeline.advance(t)
        yield maker.frame(timeline.state, t)


def _pump(cmd: list, frames, progress=None) -> None:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i, fr in enumerate(frames):
            proc.stdin.write(fr)
            if progress and i % 30 == 0:
                progress(i)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RenderError(f"ffmpeg failed: {err.strip()[-800:]}")


def render(buffer, start: float, end: float, out, *, mode="full", fps=60.0, scale=1.0, offset_ms=0.0,
           cfg: dict | None = None, progress=None) -> dict:
    """Transparent overlay video covering [start, end)."""
    cfg = cfg or oc.load()
    header, events = load_buffer(buffer)
    tl = Timeline(header, events, offset_ms / 1000.0)
    maker = FrameMaker(mode, cfg, scale)
    out = Path(out)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{maker.w}x{maker.h}",
           "-r", f"{fps:g}", "-i", "-", *_encoder_args(out), str(out)]
    t0 = time.time()
    _pump(cmd, _frames(maker, tl, start, end, fps), progress)
    return {"out": str(out), "width": maker.w, "height": maker.h, "frames": int(round((end - start) * fps)),
            "seconds": round(time.time() - t0, 2), "coverage": coverage(header, events, start)}


def coverage(header: dict, events: list, start: float) -> str:
    """"full" if the buffer reaches back to `start`, else "partial"."""
    first = header.get("start", events[0]["t"] if events else None)
    return "full" if first is not None and first <= start else "partial"


def _video_args(out: Path, crf: int) -> list:
    if out.suffix.lower() in (".mp4", ".mkv", ".mov"):
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    return []


def _filter(position: str, margin: int, size: tuple | None = None) -> str:
    x, y = overlay_xy(position, margin)
    pre = f"[1:v]scale={size[0]}:{size[1]}[ov];" if size else "[1:v]null[ov];"
    return f"{pre}[0:v][ov]overlay=x={x}:y={y}:format=auto:eof_action=pass[v]"


def _target_width(clip_w: int, scale_frac: float) -> int:
    return max(2, int(clip_w * scale_frac) // 2 * 2)


def composite(clip, buffer, out, *, clip_end: float | None = None, start: float | None = None, mode="full",
              position="bottom-right", scale_frac=0.35, margin=24, offset_ms=0.0, crf=18,
              cfg: dict | None = None, progress=None) -> dict:
    """clip + input buffer -> `out` with the overlay on top, in one ffmpeg pass.
    Give either clip_end (Unix time the clip's LAST frame was captured -- for a
    replay buffer, when the save was requested) or start (Unix time of its
    first frame)."""
    if position not in POSITIONS:
        raise RenderError(f"position must be one of {', '.join(POSITIONS)}")
    info = probe(clip)
    if start is None:
        if clip_end is None:
            raise RenderError("give --clip-end or --start")
        start = clip_end - info["duration"]
    end = start + info["duration"]
    cfg = cfg or oc.load()
    header, events = load_buffer(buffer)
    tl = Timeline(header, events, offset_ms / 1000.0)
    # drawn directly at its final size (no scaling filter, crisp text)
    base_w = FrameMaker(mode, cfg, 1.0).w
    maker = FrameMaker(mode, cfg, _target_width(info["width"], scale_frac) / base_w)
    out = Path(out)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", str(clip), "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{maker.w}x{maker.h}", "-r", f"{info['fps']:g}", "-i", "-",
           "-filter_complex", _filter(position, margin), "-map", "[v]", "-map", "0:a?",
           *_video_args(out, crf), "-c:a", "copy", str(out)]
    t0 = time.time()
    _pump(cmd, _frames(maker, tl, start, end, info["fps"]), progress)
    return {"out": str(out), "start": start, "end": end, "fps": info["fps"], "seconds": round(time.time() - t0, 2),
            "coverage": coverage(header, events, start)}


def apply(clip, overlay, out, *, position="bottom-right", scale_frac=0.35, margin=24, crf=18,
          clip_end: float | None = None, overlay_start: float | None = None) -> dict:
    """Put an already-rendered overlay video (from render()) on top of a clip.

    For the render-while-OBS-writes flow the overlay usually starts a bit
    before the clip: pass clip_end (Unix time of the clip's last frame) and
    overlay_start (the --start given to render) and the overlay is skipped
    forward so the two line up. Without them both are assumed to start
    together."""
    if position not in POSITIONS:
        raise RenderError(f"position must be one of {', '.join(POSITIONS)}")
    out = Path(out)
    ci, oi = probe(clip), probe(overlay)
    tw = _target_width(ci["width"], scale_frac)
    th = max(2, int(tw * oi["height"] / oi["width"]) // 2 * 2)
    skip = 0.0
    if clip_end is not None and overlay_start is not None:
        skip = (clip_end - ci["duration"]) - overlay_start
        if skip < -0.05:
            raise RenderError(f"the overlay starts {-skip:.2f} s after the clip does -- render it from an earlier --start")
        skip = max(0.0, skip)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", str(clip), *(["-ss", f"{skip:.6f}"] if skip else []),
           "-i", str(overlay), "-filter_complex", _filter(position, margin, (tw, th)), "-map", "[v]", "-map", "0:a?",
           *_video_args(out, crf), "-c:a", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RenderError(f"ffmpeg failed: {r.stderr.strip()[-800:]}")
    return {"out": str(out), "overlay_skip_s": round(skip, 6)}


def align(overlay, out, *, overlay_start: float, clip_start: float, duration: float) -> dict:
    """Cut a pre-rendered overlay so it starts exactly with the clip and
    lasts as long: a stream copy, no re-encode (qtrle / png / ffv1 are
    all-intra, so the cut is frame-exact). Result: a side file that lines
    up with the clip frame for frame -- what an editor/previewer wants."""
    skip = clip_start - overlay_start
    if skip < -0.05:
        raise RenderError(f"the overlay starts {-skip:.2f} s after the clip does -- render it from an earlier --start")
    skip = max(0.0, skip)
    out = Path(out)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{skip:.6f}", "-i", str(overlay), "-t", f"{duration:.6f}",
           "-map", "0:v", "-c", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RenderError(f"ffmpeg failed: {r.stderr.strip()[-800:]}")
    return {"out": str(out), "skip_s": round(skip, 6)}


def layer(clip, pieces: list, out, *, crf=18, clip_end: float | None = None,
          overlay_start: float | None = None) -> dict:
    """Burn several overlay pieces onto a clip, each at its own place and
    size. pieces: [{"file", "x", "y", "w"}] -- x, y = top-left corner and
    w = width, all as fractions of the clip (0..1); height keeps each
    piece's aspect ratio. clip_end + overlay_start line up pieces rendered
    ahead of time (see apply()); without them pieces start with the clip."""
    if not pieces:
        raise RenderError("no pieces to layer")
    ci = probe(clip)
    skip = 0.0
    if clip_end is not None and overlay_start is not None:
        skip = (clip_end - ci["duration"]) - overlay_start
        if skip < -0.05:
            raise RenderError(f"the overlays start {-skip:.2f} s after the clip does")
        skip = max(0.0, skip)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", str(clip)]
    chain, last = [], "0:v"
    for i, pc in enumerate(pieces, start=1):
        oi = probe(pc["file"])
        tw = max(2, int(ci["width"] * float(pc["w"])) // 2 * 2)
        th = max(2, int(tw * oi["height"] / oi["width"]) // 2 * 2)
        x, y = int(ci["width"] * float(pc["x"])), int(ci["height"] * float(pc["y"]))
        if skip:
            cmd += ["-ss", f"{skip:.6f}"]
        cmd += ["-i", str(pc["file"])]
        chain.append(f"[{i}:v]scale={tw}:{th}[p{i}]")
        chain.append(f"[{last}][p{i}]overlay=x={x}:y={y}:format=auto:eof_action=pass[v{i}]")
        last = f"v{i}"
    cmd += ["-filter_complex", ";".join(chain), "-map", f"[{last}]", "-map", "0:a?",
            *_video_args(Path(out), crf), "-c:a", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RenderError(f"ffmpeg failed: {r.stderr.strip()[-800:]}")
    return {"out": str(out), "pieces": len(pieces), "overlay_skip_s": round(skip, 6)}
