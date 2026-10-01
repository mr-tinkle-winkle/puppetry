"""
puppetry-overlay serve -- the overlay helper the daemon starts when
overlay.json turns something on. Standard library only (no Qt), so it's
light enough to run all the time.

    daemon events.sock --> EventSource --> Hub --+--> OBS pages (HTTP + Server-Sent Events)
                                                 +--> ReplayBuffer (input_buffer.jsonl)

Pages (each on its own localhost port, all optional):
    full    the keyboard + mouse picture           overlay_web/full.html
    simple  a text list of what's held right now   overlay_web/simple.html
    mouse   the movement arrow on its own          overlay_web/mouse.html
Every page is "/", with "/config" (style + layout JSON) and "/events" (SSE).

overlay.json is re-read whenever it changes: style edits reach open pages
live; turning pages on/off or changing ports restarts just those servers;
turning everything off exits the helper.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kbm_layout as kl
import overlay_config as oc

WEB_DIR = Path(__file__).resolve().parent / "overlay_web"
MOVE_BATCH_S = 1 / 60          # mouse motion is summed into ~60 Hz steps for the pages
BUFFER_MOVE_BATCH_S = 0.008    # ... and ~125 Hz steps in the replay-buffer file
STATIC = {"/kbm.js": "application/javascript", "/common.js": "application/javascript"}
PAGES = ("full", "simple", "movement")
PAGE_FILE = {"full": "full", "simple": "simple", "movement": "movement"}
AXIS_BATCH_S = 1 / 60


def load_code_names() -> dict:
    """evdev code -> preferred name (the layout's own names first, then the
    daemon's table, KEY_/BTN_ over aliases)."""
    names: dict = {}
    try:
        import puppetry_config as cfg
        for name, code in (cfg.name_tables().get("codes") or {}).items():
            prev = names.get(code)
            if prev is None or (not prev.startswith(("KEY_", "BTN_")) and name.startswith(("KEY_", "BTN_"))):
                names[code] = name
    except Exception:
        pass
    names.update(kl.CODE_TO_NAME)
    return names


# ---------------------------------------------------------------------------
# Events in
# ---------------------------------------------------------------------------
class EventSource(threading.Thread):
    """Reads the daemon's event stream, reconnecting forever. Calls
    hub.on_event(src, t_seconds, type, a, b) / hub.on_hello(t, codes)."""

    def __init__(self, hub, path: Path):
        super().__init__(daemon=True)
        self.hub, self.path = hub, path
        self.connected = False

    def run(self) -> None:
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(self.path))
            except OSError:
                self.connected = False
                time.sleep(1.0)
                continue
            self.connected = True
            buf = b""
            try:
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        self.handle(line)
            except OSError:
                pass
            finally:
                s.close()
                self.connected = False
                self.hub.on_disconnect(time.time())
            time.sleep(0.5)

    def handle(self, line: bytes) -> None:
        parts = line.split()
        try:
            if parts[0] == b"h":
                self.hub.on_hello(int(parts[1]) / 1e6, [int(c) for c in parts[2:]])
            elif len(parts) == 5:
                self.hub.on_event(parts[0].decode(), int(parts[1]) / 1e6, parts[2].decode(), int(parts[3]),
                                  int(parts[4]))
        except (ValueError, IndexError):
            pass


# ---------------------------------------------------------------------------
# The replay-buffer file
# ---------------------------------------------------------------------------
class ReplayBuffer:
    """Keeps the last `length` seconds of input in memory and mirrors it to
    input_buffer.jsonl in $XDG_RUNTIME_DIR (a tmpfs -- RAM, never disk).

    File format (FORMAT.md in the afterglow prep has the full spec):
      line 1: {"format": "puppetry-input-buffer", "version": 1, "clock": "unix",
               "start": t0, "length_s": L, "held_at_start": {"KEY_A": t_press, ...}}
      then one JSON event per line, oldest first:
        {"t": 1727665000.123456, "e": "kd"|"ku", "k": "KEY_A", "s": "r"|"m"}
        {"t": ..., "e": "mv", "dx": 3, "dy": -1, "s": "r"|"m"}
        {"t": ..., "e": "wh", "n": 1, "h": 0, "s": "r"|"m"}
        {"t": ..., "e": "ax", "a": "LX", "v": -0.53, "s": "r"|"m"}   (controller axes)
    Lines are appended as they happen (flushed every 100 ms); every few
    seconds the file is rewritten (atomically: temp file + rename) to drop
    what's older than the window. A reader should skip a final line without
    a newline (still being written)."""

    def __init__(self, path: Path, length_s: float, extra_s: float):
        self.path = path
        self.length_s, self.extra_s = length_s, extra_s
        self.events: deque = deque()
        self.base_held: dict = {}      # held at the oldest kept event
        self.base_axes: dict = {"r": {}, "m": {}}   # controller axes at the oldest kept event
        self._axis_last: dict = {}     # (axis, src) -> time last written
        self._axis_pending: dict = {}  # (axis, src) -> latest unwritten event
        self.lock = threading.Lock()
        self._fh = None
        self._pending_move = None      # [t, dx, dy, src]
        self._dirty = False
        self._last_compact = 0.0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.rewrite(time.time())

    def window(self) -> float:
        return self.length_s + self.extra_s

    def set_length(self, length_s: float) -> None:
        with self.lock:
            self.length_s = length_s

    def _emit(self, ev: dict) -> None:
        self.events.append(ev)
        if self._fh:
            self._fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
            self._dirty = True

    def add(self, ev: dict) -> None:
        with self.lock:
            if ev["e"] == "mv":
                pm = self._pending_move
                if pm and pm[3] == ev["s"] and ev["t"] - pm[0] <= BUFFER_MOVE_BATCH_S:
                    pm[1] += ev["dx"]
                    pm[2] += ev["dy"]
                    return
                self._flush_move()
                self._pending_move = [ev["t"], ev["dx"], ev["dy"], ev["s"]]
                return
            if ev["e"] == "ax":
                key = (ev["a"], ev["s"])
                if ev["t"] - self._axis_last.get(key, -1e18) < BUFFER_MOVE_BATCH_S and ev["v"] not in (0, 1, -1):
                    self._axis_pending[key] = ev          # thin a 1 kHz stick to ~125 Hz
                    return
                self._axis_pending.pop(key, None)
                self._axis_last[key] = ev["t"]
            self._flush_move()
            self._emit(ev)

    def _flush_axes(self, now: float) -> None:
        for key, ev in list(self._axis_pending.items()):
            if now - self._axis_last.get(key, -1e18) >= BUFFER_MOVE_BATCH_S:
                del self._axis_pending[key]
                self._axis_last[key] = ev["t"]
                self._emit(ev)

    def _flush_move(self) -> None:
        pm = self._pending_move
        if pm:
            self._pending_move = None
            self._emit({"t": round(pm[0], 6), "e": "mv", "dx": pm[1], "dy": pm[2], "s": pm[3]})

    def tick(self, now: float) -> None:
        """Called ~10x/s: flush, and compact when the file has grown well past the window."""
        with self.lock:
            if self._pending_move and now - self._pending_move[0] > BUFFER_MOVE_BATCH_S:
                self._flush_move()
            if self._axis_pending:
                self._flush_axes(now)
            if self._fh and self._dirty:
                self._fh.flush()
                self._dirty = False
            if now - self._last_compact >= 5.0:
                self._rewrite_locked(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window()
        ev = self.events
        while ev and ev[0]["t"] < cutoff:
            e = ev.popleft()
            if e["e"] == "ax":
                if e["v"]:
                    self.base_axes[e["s"]][e["a"]] = e["v"]
                else:
                    self.base_axes[e["s"]].pop(e["a"], None)
            elif e["s"] == "r":
                if e["e"] == "kd":
                    self.base_held.setdefault(e["k"], e["t"])
                elif e["e"] == "ku":
                    self.base_held.pop(e["k"], None)

    def rewrite(self, now: float) -> None:
        with self.lock:
            self._rewrite_locked(now)

    def _rewrite_locked(self, now: float) -> None:
        self._last_compact = now
        self._trim(now)
        start = self.events[0]["t"] if self.events else now
        head = {"format": "puppetry-input-buffer", "version": 1, "clock": "unix",
                "start": round(min(start, now - self.window()), 6), "length_s": self.length_s,
                "held_at_start": {k: round(v, 6) for k, v in self.base_held.items()},
                "axes_at_start": {"r": dict(self.base_axes["r"]), "m": dict(self.base_axes["m"])}}
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(head) + "\n")
            for e in self.events:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        if self._fh:
            self._fh.close()
        self._fh = open(self.path, "a")

    def close(self) -> None:
        with self.lock:
            if self._fh:
                self._fh.close()
                self._fh = None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, el: str | None = None):
        self.q: queue.Queue = queue.Queue(maxsize=2000)
        self.el = el                         # an element page (/el/<id>) or None (the whole page)

    def put(self, msg: str) -> None:
        try:
            self.q.put_nowait(msg)
        except queue.Full:                   # a stalled page loses events, never blocks us
            pass


class PageServer:
    def __init__(self, hub, page: str, port: int):
        self.hub, self.page, self.port = hub, page, port
        hub_ref, page_ref = hub, page

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _send(self, code, ctype, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def _el(self):
                """Element id from ?el=<id> (the element pages pass it on)."""
                from urllib.parse import parse_qs, urlparse
                v = parse_qs(urlparse(self.path).query).get("el")
                return v[0] if v else None

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if page_ref == "full" and path.startswith("/el/") and len(path) > 4:
                    return self._send(200, "text/html; charset=utf-8", (WEB_DIR / "full.html").read_bytes())
                if path in ("/", "/index.html"):
                    return self._send(200, "text/html; charset=utf-8", (WEB_DIR / f"{PAGE_FILE[page_ref]}.html").read_bytes())
                if path in STATIC:
                    return self._send(200, STATIC[path], (WEB_DIR / path.lstrip("/")).read_bytes())
                if path == "/config":
                    return self._send(200, "application/json",
                                      json.dumps(hub_ref.page_config(page_ref, self._el())).encode())
                if path == "/state":
                    return self._send(200, "application/json", json.dumps(hub_ref.snapshot()).encode())
                if path == "/events":
                    return self._events()
                return self._send(404, "text/plain", b"not found")

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                client = Client(self._el())
                hub_ref.add_client(page_ref, client)
                try:
                    cfg_now = hub_ref.page_config(page_ref, client.el)
                    self.wfile.write(f"event: config\ndata: {json.dumps(cfg_now)}\n\n".encode())
                    self.wfile.write(f"event: snapshot\ndata: {json.dumps(hub_ref.snapshot())}\n\n".encode())
                    self.wfile.flush()
                    while not hub_ref.stopping:
                        try:
                            msg = client.q.get(timeout=5.0)
                        except queue.Empty:
                            msg = ": keepalive\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    hub_ref.remove_client(page_ref, client)

        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.timeout = 0.25
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        # handle_request() in our own loop instead of serve_forever(): stop()
        # then never waits on the server thread (shutdown() could hang while
        # a Server-Sent-Events handler was mid-stream)
        while not self._stop:
            try:
                self.httpd.handle_request()
            except OSError:
                break

    def stop(self) -> None:
        self._stop = True
        try:
            self.httpd.server_close()      # frees the port right away
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self, cfg: dict | None = None, *, event_socket: Path | None = None, buffer_path: Path | None = None,
                 obs_factory=None, start_threads: bool = True):
        self.cfg = cfg or oc.load()
        self.lock = threading.Lock()
        self.ev_lock = threading.RLock()     # event thread vs ticker thread
        self._status_lock = threading.Lock()
        self.clients: dict = {p: set() for p in PAGES}
        self.servers: dict = {}
        self.state = kl.KbmState()          # epoch-second times
        self.stopping = False
        self.code_names = load_code_names()
        self.buffer_path = buffer_path or oc.buffer_file()
        self.replay: ReplayBuffer | None = None
        self.replay_length = float(self.cfg["replay"]["fallback_seconds"])
        self.length_source = "fallback"
        self.obs_status = "not checked"
        self.obs_factory = obs_factory
        self._pending_move = None           # [t, dx, dy, src] for pages
        self._axis_sent: dict = {}          # (axis, src) -> last broadcast time
        self._axis_pending: dict = {}
        self._last_obs_poll = 0.0
        self._cfg_mtime = self._mtime()
        self.source = EventSource(self, event_socket or oc.event_socket())
        self.apply_config(self.cfg)
        if start_threads:
            self.source.start()
            threading.Thread(target=self._ticker, daemon=True).start()

    # -- config ----------------------------------------------------------------
    def _mtime(self):
        try:
            return oc.config_file().stat().st_mtime
        except OSError:
            return None

    def page_config(self, page: str, el: str | None = None) -> dict:
        c = self.cfg
        if page == "full":
            out = {"style": c["style"], "layout": kl.build_scene(c["scene"], only=el), "piece": el or "scene",
                   "pad_labels": kl.PAD_LABELS.get(c["style"].get("controller_labels", "xbox"), kl.PAD_LABELS["xbox"])}
            if el and not any(e.get("id") == el for e in c["scene"].get("elements", [])):
                out["missing"] = el
            return out
        if page == "simple":
            st = c["simple_style"]
            labels = dict(kl.LABELS)
            labels.update(kl.PAD_LABELS.get(st.get("controller_labels", "xbox"), {}))
            return {"style": st, "labels": labels,
                    "pad_labels": kl.PAD_LABELS.get(st.get("controller_labels", "xbox"), kl.PAD_LABELS["xbox"])}
        return {"style": c["movement_style"]}

    def wanted_pages(self, cfg: dict) -> dict:
        return oc.pages(cfg)

    def apply_config(self, cfg: dict) -> None:
        self.cfg = cfg
        want = self.wanted_pages(cfg)
        for page in list(self.servers):
            if page not in want or self.servers[page].port != want[page]:
                self.servers.pop(page).stop()
        for page, port in want.items():
            if page not in self.servers:
                try:
                    self.servers[page] = PageServer(self, page, port)
                except OSError as e:
                    print(f"puppetry-overlay: can't serve {page} on port {port}: {e}", file=sys.stderr)
        if cfg["replay"]["enabled"] and self.replay is None:
            self.replay = ReplayBuffer(self.buffer_path, self.replay_length, float(cfg["replay"]["extra_seconds"]))
        elif not cfg["replay"]["enabled"] and self.replay is not None:
            self.replay.close()
            self.replay = None
            try:
                self.buffer_path.unlink()
            except OSError:
                pass
        if self.replay:
            self.replay.extra_s = float(cfg["replay"]["extra_seconds"])
            if self.length_source == "fallback":
                self.replay_length = float(cfg["replay"]["fallback_seconds"])
                self.replay.set_length(self.replay_length)
        for page in self.clients:                     # each client gets ITS page / element's config
            with self.lock:
                targets = list(self.clients[page])
            for cl in targets:
                cl.put(f"event: config\ndata: {json.dumps(self.page_config(page, cl.el), separators=(',', ':'))}\n\n")
        self.write_status()

    def reload_if_changed(self) -> bool:
        m = self._mtime()
        if m == self._cfg_mtime:
            return False
        self._cfg_mtime = m
        self.apply_config(oc.load())
        return True

    # -- OBS -----------------------------------------------------------------------
    def poll_obs(self) -> None:
        if not self.cfg["replay"]["enabled"]:
            return
        o = self.cfg["obs"]
        try:
            if self.obs_factory:
                client = self.obs_factory(o)
            else:
                from obs_client import ObsClient
                client = ObsClient(o["host"], o["port"], o.get("password", ""))
            with client as c:
                secs = c.replay_buffer_seconds()
            if secs:
                self.replay_length, self.length_source = secs, "obs"
                self.obs_status = f"connected; replay buffer {secs:g} s"
            else:
                self.obs_status = "connected, but OBS didn't report a replay buffer length"
                self.length_source = "fallback"
                self.replay_length = float(self.cfg["replay"]["fallback_seconds"])
        except Exception as e:  # noqa: BLE001 -- any failure just means "use the fallback"
            self.obs_status = f"not connected ({e})"
            self.length_source = "fallback"
            self.replay_length = float(self.cfg["replay"]["fallback_seconds"])
        if self.replay:
            self.replay.set_length(self.replay_length)
        self.write_status()

    def write_status(self) -> None:
        st = {"pid": os.getpid(), "updated": time.time(), "daemon_connected": self.source.connected,
              "pages": {p: f"http://127.0.0.1:{s.port}/" for p, s in self.servers.items()},
              "elements": [{"id": e.get("id"), "type": e.get("type")} for e in self.cfg["scene"].get("elements", [])],
              "element_urls": oc.element_urls(self.cfg),
              "replay": {"enabled": bool(self.replay), "file": str(self.buffer_path),
                         "length_s": self.replay_length, "length_source": self.length_source,
                         "extra_s": float(self.cfg["replay"]["extra_seconds"])},
              "obs": self.obs_status}
        try:
            with self._status_lock:
                p = oc.status_file()
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(st, indent=2))
                tmp.replace(p)
        except OSError:
            pass

    # -- clients -------------------------------------------------------------------
    def add_client(self, page, c) -> None:
        with self.lock:
            self.clients[page].add(c)

    def remove_client(self, page, c) -> None:
        with self.lock:
            self.clients[page].discard(c)

    def broadcast(self, page, event: str, data) -> None:
        with self.lock:
            targets = list(self.clients[page])
        if not targets:
            return
        msg = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        for c in targets:
            c.put(msg)

    def broadcast_all(self, event, data) -> None:
        for page in self.clients:
            self.broadcast(page, event, data)

    def snapshot(self) -> dict:
        now = time.time()
        s = self.state
        return {"now": now, "held": {k: now - t for k, t in s.held.items()},
                "out": {k: now - t for k, t in s.out_held.items()},
                "burst": [[round(t - now, 4), x, y] for t, x, y in s.burst[-600:]], "burst_src": s.burst_src,
                "last_move_age": now - s.last_move_t if s.burst else None,
                "axes": dict(s.axes), "out_axes": dict(s.out_axes),
                "motion": [[round(t - now, 4), x, y, src] for t, x, y, src in s.motion[-1500:]],
                "clicks": [[round(t - now, 4), b, src] for t, b, src in s.clicks],
                "wheels": [[round(t - now, 4), d, src] for t, d, src in s.wheels]}

    # -- events --------------------------------------------------------------------
    def name(self, code: int) -> str:
        return self.code_names.get(code, f"CODE_{code}")

    def on_hello(self, t: float, codes: list) -> None:
        for c in codes:
            self.state.key(self.name(c), True, t)
        self.broadcast_all("snapshot", self.snapshot())

    def on_disconnect(self, t: float) -> None:
        # the daemon went away: nothing can still be held
        for name in list(self.state.held):
            self.on_event("r", t, "k", -1, 0, name=name)
        for ax in list(self.state.axes):
            self.on_event("r", t, "a", -1, 0, name=ax)
        self.state.out_held.clear()

    def on_event(self, src: str, t: float, typ: str, a: int, b: int, name: str | None = None) -> None:
        with self.ev_lock:
            self._on_event(src, t, typ, a, b, name)

    def _on_event(self, src, t, typ, a, b, name) -> None:
        if typ == "k":
            nm = name or self.name(a)
            down = b == 1
            self.state.key(nm, down, t, src)
            ev = {"t": round(t, 6), "e": "kd" if down else "ku", "k": nm, "s": src}
            self._flush_page_move()
            self.broadcast_all("ev", ev)
            if self.replay:
                self.replay.add(ev)
        elif typ == "a":
            ax = name or kl.AXIS_NAMES.get(a)
            if ax is None:
                return
            v = round(b / 10000.0, 4)
            self.state.axis(ax, v, t, src)
            ev = {"t": round(t, 6), "e": "ax", "a": ax, "v": v, "s": src}
            key = (ax, src)
            if t - self._axis_sent.get(key, -1e18) >= AXIS_BATCH_S or v in (0, 1, -1):
                self._axis_sent[key] = t
                self._axis_pending.pop(key, None)
                self.broadcast_all("ev", ev)
            else:
                self._axis_pending[key] = ev
            if self.replay:
                self.replay.add(ev)
        elif typ == "m":
            self.state.move(a, b, t, src)
            pm = self._pending_move
            if pm and pm[3] == src and t - pm[0] <= MOVE_BATCH_S:
                pm[1] += a
                pm[2] += b
                pm[4] = t
            else:
                self._flush_page_move()
                self._pending_move = [t, a, b, src, t]
            if self.replay:
                self.replay.add({"t": round(t, 6), "e": "mv", "dx": a, "dy": b, "s": src})
        elif typ == "w":
            self.state.wheel(a, t, src)
            ev = {"t": round(t, 6), "e": "wh", "n": a, "h": b, "s": src}
            self._flush_page_move()
            self.broadcast_all("ev", ev)
            if self.replay:
                self.replay.add(ev)

    def _flush_page_move(self) -> None:
        pm = self._pending_move
        if pm:
            self._pending_move = None
            self.broadcast_all("ev", {"t": round(pm[4], 6), "e": "mv", "dx": pm[1], "dy": pm[2], "s": pm[3]})

    def tick(self, now: float) -> None:
        with self.ev_lock:
            if self._pending_move and now - self._pending_move[0] > MOVE_BATCH_S:
                self._flush_page_move()
            for key, ev in list(self._axis_pending.items()):
                if now - self._axis_sent.get(key, -1e18) >= AXIS_BATCH_S:
                    del self._axis_pending[key]
                    self._axis_sent[key] = now
                    self.broadcast_all("ev", ev)
        if self.replay:
            self.replay.tick(now)

    def _ticker(self) -> None:
        last_status = 0.0
        while not self.stopping:
            now = time.time()
            self.tick(now)
            if int(now * 10) % 5 == 0:
                self.reload_if_changed()
            if self.cfg["replay"]["enabled"] and now - self._last_obs_poll > 30:
                self._last_obs_poll = now
                threading.Thread(target=self.poll_obs, daemon=True).start()
            if now - last_status > 2:
                last_status = now
                self.write_status()
            if not oc.helper_wanted(self.cfg):
                self.stopping = True
            time.sleep(0.01)

    def stop(self) -> None:
        self.stopping = True
        for s in self.servers.values():
            s.stop()
        self.servers.clear()
        if self.replay:
            self.replay.close()


def serve() -> int:
    cfg = oc.load()
    if not oc.helper_wanted(cfg):
        print("puppetry-overlay: nothing enabled in overlay.json -- exiting")
        return 0
    hub = Hub(cfg)
    print(f"puppetry-overlay: pages {', '.join(f'{p}=:{s.port}' for p, s in hub.servers.items()) or 'none'}; "
          f"replay buffer {'on' if hub.replay else 'off'}", flush=True)
    try:
        while not hub.stopping:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    hub.stop()
    return 0
