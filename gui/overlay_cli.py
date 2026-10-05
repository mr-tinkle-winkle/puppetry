"""
puppetry-overlay -- the overlay helper and its tools.

    puppetry-overlay serve                  (started by the daemon; OBS pages + replay-buffer file)
    puppetry-overlay status                 JSON: pages, replay-buffer file + length, OBS connection
    puppetry-overlay snapshot OUT           copy the replay-buffer file right now (freeze a clip's input)
    puppetry-overlay render --start T --end T OUT [--buffer F] [--mode M] [--fps N] [--scale S]
                            modes: full (the arrangement), el:<element id>, simple, movement
    puppetry-overlay composite --clip IN (--clip-end T | --start T) OUT [--buffer F] [--position P]
                               [--scale-frac F] [--margin PX] [--mode M] [--offset-ms MS] [--crf N]
    puppetry-overlay apply --clip IN --overlay OV OUT [--clip-end T --overlay-start T] [--position P]
                           [--scale-frac F] [--margin PX]
    puppetry-overlay align --overlay OV --overlay-start T --clip IN --clip-end T OUT
                                            cut a pre-rendered overlay to line up with a clip (stream copy)
    puppetry-overlay layer --clip IN --piece FILE:X:Y:W [--piece ...] OUT [--clip-end T --overlay-start T]
                                            burn several pieces on, each placed/sized (fractions of the clip)
    puppetry-overlay add-to-obs             create/update Browser Sources for the enabled pages
    puppetry-overlay obs-replay-length      what OBS reports as its replay buffer length
    puppetry-overlay screen show|hide|toggle|stop|status
                                            the input overlay on the real screen (click-through, on top)
    puppetry-overlay block toggle|on|off|status
                                            block the input visualizer: nothing shown, sent to OBS or
                                            recorded (ignored apps do the same automatically)
    puppetry-overlay share [toggle|show|hide|update|window|projector|status]
                                            the overlay for viewers only, in OBS scene "Puppetry: Share"
                                            (never in clips), placed with the on-screen overlay's settings
                                            (toggle = the keybind); `window` opens that scene as a window
                                            to share; `projector` opens OBS's plain program output

Every command prints one JSON object on stdout; on failure it prints
{"error": "..."} and exits 1. Times are Unix seconds.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys


def _print(obj) -> None:
    print(json.dumps(obj))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="puppetry-overlay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("status")
    sp = sub.add_parser("snapshot")
    sp.add_argument("out")
    sp.add_argument("--buffer")

    def common(p):
        p.add_argument("--buffer", help="input buffer file (default: the live one)")
        p.add_argument("--mode", default="full",
                       help="full (the whole arrangement), simple, movement, or el:<element id> (see status)")
        p.add_argument("--offset-ms", type=float, default=0.0,
                       help="shift inputs later (+) or earlier (-) relative to the video")

    rp = sub.add_parser("render")
    common(rp)
    rp.add_argument("--start", type=float, required=True)
    rp.add_argument("--end", type=float, required=True)
    rp.add_argument("--fps", type=float, default=60.0)
    rp.add_argument("--scale", type=float, default=1.0)
    rp.add_argument("out")

    cp = sub.add_parser("composite")
    common(cp)
    cp.add_argument("--clip", required=True)
    g = cp.add_mutually_exclusive_group(required=True)
    g.add_argument("--clip-end", type=float)
    g.add_argument("--start", type=float)
    cp.add_argument("--position", default="bottom-right")
    cp.add_argument("--scale-frac", type=float, default=0.35, help="overlay width as a fraction of the clip's")
    cp.add_argument("--margin", type=int, default=24)
    cp.add_argument("--crf", type=int, default=18)
    cp.add_argument("out")

    app = sub.add_parser("apply")
    app.add_argument("--clip", required=True)
    app.add_argument("--overlay", required=True)
    app.add_argument("--position", default="bottom-right")
    app.add_argument("--scale-frac", type=float, default=0.35)
    app.add_argument("--margin", type=int, default=24)
    app.add_argument("--crf", type=int, default=18)
    app.add_argument("--clip-end", type=float, help="Unix time of the clip's last frame (with --overlay-start)")
    app.add_argument("--overlay-start", type=float, help="the --start the overlay was rendered from")
    app.add_argument("out")

    alp = sub.add_parser("align")
    alp.add_argument("--overlay", required=True)
    alp.add_argument("--overlay-start", type=float, required=True, help="the --start it was rendered from")
    alp.add_argument("--clip", required=True, help="the clip it belongs to (for its length)")
    alp.add_argument("--clip-end", type=float, required=True, help="Unix time of the clip's last frame")
    alp.add_argument("out")

    lp = sub.add_parser("layer")
    lp.add_argument("--clip", required=True)
    lp.add_argument("--piece", action="append", required=True, metavar="FILE:X:Y:W",
                    help="an overlay file and where it goes: top-left x, y and width as fractions of the clip")
    lp.add_argument("--clip-end", type=float)
    lp.add_argument("--overlay-start", type=float)
    lp.add_argument("--crf", type=int, default=18)
    lp.add_argument("out")

    sub.add_parser("add-to-obs")
    sub.add_parser("obs-replay-length")
    scp = sub.add_parser("screen")
    scp.add_argument("action", choices=["show", "hide", "toggle", "stop", "status", "run"])
    scp.add_argument("--show", action="store_true", help="(run) start visible")
    bp = sub.add_parser("block")
    bp.add_argument("action", nargs="?", default="toggle", choices=["toggle", "on", "off", "status"])
    shp = sub.add_parser("share")
    shp.add_argument("action", nargs="?", default="toggle",
                     choices=["toggle", "show", "hide", "update", "window", "projector", "status"])
    a = ap.parse_args(argv)

    import overlay_config as oc
    try:
        if a.cmd == "serve":
            import overlay_server
            return overlay_server.serve()
        if a.cmd == "status":
            try:
                _print(json.loads(oc.status_file().read_text()))
            except (OSError, ValueError):
                _print({"error": "the overlay helper isn't running (nothing enabled, or the daemon is stopped)"})
                return 1
            return 0
        if a.cmd == "snapshot":
            src = a.buffer or str(oc.buffer_file())
            shutil.copyfile(src, a.out)
            _print({"out": a.out})
            return 0
        if a.cmd == "align":
            import overlay_render as orr
            dur = orr.probe(a.clip)["duration"]
            _print(orr.align(a.overlay, a.out, overlay_start=a.overlay_start, clip_start=a.clip_end - dur,
                             duration=dur))
            return 0
        if a.cmd == "layer":
            import overlay_render as orr
            pieces = []
            for spec in a.piece:
                f, x, y, w = spec.rsplit(":", 3)
                pieces.append({"file": f, "x": float(x), "y": float(y), "w": float(w)})
            _print(orr.layer(a.clip, pieces, a.out, crf=a.crf, clip_end=a.clip_end, overlay_start=a.overlay_start))
            return 0
        if a.cmd in ("render", "composite", "apply"):
            import overlay_render as orr
            buf = getattr(a, "buffer", None) or str(oc.buffer_file())
            if a.cmd == "render":
                _print(orr.render(buf, a.start, a.end, a.out, mode=a.mode, fps=a.fps, scale=a.scale,
                                  offset_ms=a.offset_ms))
            elif a.cmd == "composite":
                _print(orr.composite(a.clip, buf, a.out, clip_end=a.clip_end, start=a.start, mode=a.mode,
                                     position=a.position, scale_frac=a.scale_frac, margin=a.margin,
                                     offset_ms=a.offset_ms, crf=a.crf))
            else:
                _print(orr.apply(a.clip, a.overlay, a.out, position=a.position, scale_frac=a.scale_frac,
                                 margin=a.margin, crf=a.crf, clip_end=a.clip_end, overlay_start=a.overlay_start))
            return 0
        if a.cmd == "screen":
            import screen_overlay
            if a.action == "run":
                return screen_overlay.run(show=a.show)
            _print(screen_overlay.control(a.action))
            return 0
        if a.cmd == "block":
            state = {"on": "block", "off": "unblock"}.get(a.action, a.action)
            resp = daemon_request({"cmd": "VISUALIZER", "state": state})
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "the daemon refused"))
            resp.pop("ok", None)
            resp.update({k: v for k, v in oc.read_privacy().items() if k in ("app", "why", "watching")})
            _print(resp)
            return 0
        if a.cmd == "share":
            import viewers_share
            _print(viewers_share.share(a.action))
            return 0
        if a.cmd in ("add-to-obs", "obs-replay-length"):
            from obs_client import ObsClient
            cfg = oc.load()
            o = cfg["obs"]
            with ObsClient(o["host"], o["port"], o.get("password", "")) as c:
                if a.cmd == "obs-replay-length":
                    _print({"seconds": c.replay_buffer_seconds()})
                else:
                    _print({"sources": c.add_browser_sources(obs_sources(cfg))})
            return 0
    except Exception as e:  # noqa: BLE001 -- one JSON error line for the caller
        _print({"error": str(e)})
        return 1
    return 0


def daemon_request(payload: dict, timeout: float = 3.0) -> dict:
    """One JSON request on the daemon's control socket; its whole JSON reply."""
    import socket
    import overlay_config as oc
    path = oc.CONFIG_DIR / "control.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        return {"ok": False, "error": f"the Puppetry daemon isn't running ({e})"}
    finally:
        s.close()
    try:
        return json.loads(buf.decode() or "{}")
    except ValueError:
        return {"ok": False, "error": "no answer from the daemon"}


SOURCE_NAMES = {"full": "Puppetry Input Overlay", "simple": "Puppetry Input List",
                "movement": "Puppetry Mouse Movement"}


def obs_sources(cfg: dict) -> list:
    """Browser Source definitions for every enabled page (and, with "each
    element as its own source", every element), sized to it."""
    import kbm_layout as kl
    import overlay_config as oc
    out = []
    els = oc.element_urls(cfg)
    for page, url in oc.urls(cfg).items():
        if page == "full":
            if els:
                for el_id, el_url in els.items():
                    w, h = kl.layout_pixel_size(kl.build_scene(cfg["scene"], only=el_id), cfg["style"])
                    out.append({"name": f"Puppetry: {el_id}", "url": el_url, "width": w, "height": h})
                continue
            w, h = kl.layout_pixel_size(kl.build_scene(cfg["scene"]), cfg["style"])
        elif page == "simple":
            w, h = int(cfg["simple"]["width"]), int(max(20, cfg["simple_style"]["font_px"] * 1.6))
        else:
            w = h = int(cfg["movement_style"]["size"])
        out.append({"name": SOURCE_NAMES[page], "url": url, "width": w, "height": h})
    return out


if __name__ == "__main__":
    sys.exit(main())
