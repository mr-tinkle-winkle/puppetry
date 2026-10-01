"""
Tests for the OBS overlay / layered replay buffer pieces:
    kbm_layout (layout, timers, curved arrow), the replay-buffer file,
    the overlay helper's HTTP/SSE pages (fed by a fake daemon socket),
    obs_client (against a fake obs-websocket v5 server), the renderer and
    `puppetry-overlay` CLI, and JS <-> Python parity of the arrow math.

    QT_QPA_PLATFORM=offscreen python3 test_overlay.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="puppetry_ovl_home_")
os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="puppetry_ovl_run_")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kbm_layout as kl  # noqa: E402
import overlay_config as oc  # noqa: E402

_checks, _fail = 0, []


def check(name, cond):
    global _checks
    _checks += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail.append(name)


# ---------------------------------------------------------------------------
def layout_tests():
    full = kl.build_layout()
    names = {i["name"] for i in full["items"]}
    check("layout: every letter, both shifts, the arrows and all five mouse buttons",
          all(f"KEY_{c}" in names for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
          and {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT", "KEY_UP", "KEY_LEFT"} <= names
          and set(kl.MOUSE_BUTTONS) <= names)
    check("layout: starts at (0, 0)", min(i["x"] for i in full["items"]) == 0 and min(i["y"] for i in full["items"]) == 0)
    small = kl.build_layout(show_function_row=False, show_nav=False, show_arrows=False)
    sn = {i["name"] for i in small["items"]}
    check("layout: sections can be hidden (and it gets smaller)",
          "KEY_F1" not in sn and "KEY_HOME" not in sn and "KEY_UP" not in sn and small["w"] < full["w"]
          and small["h"] < full["h"])
    mouse_only = kl.build_layout(show_keyboard=False)
    check("layout: mouse-only works", {i["kind"] for i in mouse_only["items"]} >= {"mouse_body", "arrow_box"}
          and not any(i["kind"] == "key" for i in mouse_only["items"]))
    check("layout: pixel_size follows key size", kl.pixel_size({"unit": 20, "padding": 0})[0] * 2
          <= kl.pixel_size({"unit": 40, "padding": 0})[0] + 2)

    check("timer: milliseconds", kl.format_hold(0.2344) == "0.234" and kl.format_hold(1.1) == "1.100")
    check("timer: minutes", kl.format_hold(75.5) == "1:15.500")
    check("timer: float noise doesn't lose a millisecond", kl.format_hold(2.1 - 1.0) == "1.100")

    # keyboard presets + mouse looks
    sizes = {p: kl.keyboard_layout(p) for p in kl.KEYBOARD_PRESETS}
    check("presets: full > 80% > 60% > half, by width",
          sizes["full"]["w"] > sizes["tkl"]["w"] > sizes["60"]["w"] > sizes["half"]["w"])
    names = {p: {i["name"] for i in l["items"]} for p, l in sizes.items()}
    check("presets: full has the numpad, 80% the print-screen row, 60% neither",
          "KEY_KP5" in names["full"] and "KEY_KP5" not in names["tkl"] and "KEY_SYSRQ" in names["tkl"]
          and "KEY_SYSRQ" not in names["60"] and "KEY_F1" not in names["60"] and "KEY_UP" not in names["60"])
    check("presets: half is the gaming side (WASD yes, P no)", {"KEY_W", "KEY_A", "KEY_S", "KEY_D"} <= names["half"]
          and "KEY_P" not in names["half"] and "KEY_6" not in names["half"])
    kp_plus = next(i for i in sizes["full"]["items"] if i["name"] == "KEY_KPPLUS")
    check("presets: numpad + and Enter are two keys tall", kp_plus["h"] == 2)
    for look in kl.MOUSE_LOOKS:
        ml = kl.mouse_layout(look)
        check(f"mouse look {look}: has left and right buttons",
              {"BTN_LEFT", "BTN_RIGHT"} <= {i["name"] for i in ml["items"]} and min(i["x"] for i in ml["items"]) == 0)

    # scene
    sc = {"elements": [{"id": "k", "type": "keyboard", "layout": "half", "x": 2, "y": 1, "scale": 2.0},
                       {"id": "p", "type": "controller", "x": 30, "y": 0, "scale": 0.5},
                       {"id": "j", "type": "joystick", "size": 3, "x": 0, "y": 20, "scale": 1}]}
    whole = kl.build_scene(sc)
    only_k = kl.build_scene(sc, only="k")
    check("scene: elements are placed and scaled", only_k["w"] == sizes["half"]["w"] * 2
          and all(i["fs"] == 2.0 and i["el"] == "k" for i in only_k["items"]))
    check("scene: one element alone starts at (0, 0)", min(i["x"] for i in only_k["items"]) == 0)
    raw = kl.build_scene(sc, normalize=False)
    check("scene: the editor keeps scene coordinates", min(i["x"] for i in raw["items"]) == 0
          and min(i["x"] for i in raw["items"] if i["el"] == "k") == 2)
    check("scene: movement views are motion items", any(i["kind"] == "motion" and i["mode"] == "joystick"
                                                       for i in whole["items"]))
    check("scene: new ids don't collide", kl.new_element_id(sc, "keyboard") == "keyboard"
          and kl.new_element_id({"elements": [{"id": "comet"}]}, "comet") == "comet2")

    # movement views
    st = kl.KbmState()
    t = 0.0
    for i in range(40):                      # right ...
        st.move(10, 0, t)
        t += 0.01
    for i in range(40):                      # ... and straight back (the case the arrow got wrong)
        st.move(-10, 0, t)
        t += 0.01
    style = {"screen_height": 1000, "pad_fraction": 80}
    f = kl.motion_frame(st, "comet", 200, style, t, "c1", "tail")
    xs = [p[0] for p in f["trail"]]
    check("comet: tail centered -- the oldest point sits in the middle", abs(f["trail"][0][0] - 100) < 1e-6
          and abs(f["trail"][0][1] - 100) < 1e-6)
    check("comet: moving back on yourself draws the whole out-and-back path",
          max(xs) - 100 > 30 and abs(f["dot"][0] - 100) < 2)
    widths = [p[2] for p in f["trail"]]
    alphas = [p[3] for p in f["trail"]]
    check("comet: the trail thins toward its old end", widths[0] < widths[-1] and widths == sorted(widths))
    q = len(alphas) // 4
    check("comet: only the oldest quarter fades", alphas[0] == 0 and all(a == 1 for a in alphas[q + 1:]))
    check("comet: the cursor dot is wider than the trail", f["dot"][2] > max(widths) / 2)
    fh = kl.motion_frame(st, "comet", 200, style, t, "c2", "head")
    check("comet: head centered -- the newest point sits in the middle", abs(fh["dot"][0] - 100) < 1e-6)
    big = kl.KbmState()
    for i in range(60):
        big.move(200, 0, i * 0.01)           # far bigger than the view
    fz = kl.motion_frame(big, "comet", 200, style, 0.6, "z", "tail")
    check("comet: auto zoom keeps the whole trail on screen", all(0 <= p[0] <= 200 for p in fz["trail"])
          and len(fz["trail"]) > 50)
    fc = kl.motion_frame(big, "comet", 200, dict(style, auto_zoom=False), 0.6, "nz", "tail")
    check("comet: without auto zoom, old points are dropped so the head stays on screen",
          len(fc["trail"]) < len(fz["trail"]) and 0 <= fc["dot"][0] <= 200)
    fz2 = kl.motion_frame(big, "comet", 200, style, 0.65, "z", "tail")
    check("comet: zooming back in is gradual", big.views["z"]["z"] <= 1.0)
    mp = kl.KbmState()
    for i in range(20):
        mp.move(5, 0, i * 0.01)
    f1 = kl.motion_frame(mp, "mousepad", 200, style, 0.2, "m", "tail")
    check("mousepad: the dot moves away from the middle with the mouse", f1["dot"][0] > 105)
    kl.motion_frame(mp, "mousepad", 200, style, 2.0, "m", "tail")
    f2 = kl.motion_frame(mp, "mousepad", 200, style, 3.0, "m", "tail")
    check("mousepad: re-centers after resting", abs(f2["dot"][0] - 100) < 2)
    js = kl.KbmState()
    for i in range(20):
        js.move(30, 0, i * 0.01)             # 3000 px/s to the right
    fj = kl.motion_frame(js, "joystick", 200, {"joystick_speed": 3000}, 0.19, "j", "tail")
    check("joystick: the dot leans the way you're moving, by speed", fj["dot"][0] > 150 and abs(fj["dot"][1] - 100) < 1)
    fj2 = kl.motion_frame(js, "joystick", 200, {"joystick_speed": 3000}, 2.0, "j", "tail")
    check("joystick: back to the middle when the mouse stops", abs(fj2["dot"][0] - 100) < 1e-6)
    ck = kl.KbmState()
    ck.key("BTN_LEFT", True, 1.0)
    ck.key("BTN_RIGHT", True, 1.0)
    ck.key("BTN_MIDDLE", True, 1.0)
    ck.key("BTN_SIDE", True, 1.0)
    ck.key("BTN_EXTRA", True, 1.0)
    ck.wheel(1, 1.0)
    ck.wheel(-1, 1.05)
    fr = kl.motion_frame(ck, "comet", 200, {}, 1.1, "r", "tail")
    rings = {(r["arcs"] and tuple(tuple(a) for a in r["arcs"])): r for r in fr["rings"]}
    full_rings = [r for r in fr["rings"] if r["arcs"] is None]
    check("clicks: left ring grows outward, right ring shrinks inward",
          len(full_rings) == 2 and full_rings[0]["r"] < full_rings[1]["r"])
    check("clicks: middle = two 60-degree arcs top and bottom; side buttons = one arc each side",
          ((90.0, 60.0), (270.0, 60.0)) in rings and ((180.0, 60.0),) in rings and ((0.0, 60.0),) in rings)
    fi = kl.motion_frame(ck, "comet", 200, {"invert_side_rings": True}, 1.1, "ri", "tail")
    side_arcs = [r["arcs"][0][0] for r in fr["rings"] if r["arcs"] and len(r["arcs"]) == 1]
    inv_arcs = [r["arcs"][0][0] for r in fi["rings"] if r["arcs"] and len(r["arcs"]) == 1]
    check("clicks: back goes left and forward right; 'invert side button rings' swaps them",
          side_arcs == [180.0, 0.0] and inv_arcs == [0.0, 180.0])
    check("comet follows its head by default", kl.DEFAULT_SCENE["elements"][2]["center"] == "head"
          and kl.default_element({"elements": []}, "comet")["center"] == "head"
          and oc.DEFAULTS["movement_style"]["center"] == "head")
    old = oc.merged({"scene": {"elements": [{"id": "c", "type": "comet", "center": "tail", "x": 0, "y": 0}]},
                     "movement_style": {"center": "tail"}})
    check("saved configs from before move to 'follows head' once", old["scene"]["elements"][0]["center"] == "head"
          and old["movement_style"]["center"] == "head")
    kept = oc.merged(dict(old, scene={"elements": [{"id": "c", "type": "comet", "center": "tail", "x": 0, "y": 0}]}))
    check("... and a later choice of 'follows tail' is kept", kept["scene"]["elements"][0]["center"] == "tail")
    inv_item = kl.element_layout({"id": "c", "type": "comet", "invert_side": True})["items"][0]
    check("an element's own 'invert side button rings' reaches its item", inv_item["invert_side"] is True)
    check("wheel: chevrons above for up, below for down",
          {c["dir"] for c in fr["chevrons"]} == {1, -1}
          and all((c["y"] < fr["dot"][1]) == (c["dir"] > 0) for c in fr["chevrons"]))
    check("held: a held button rings the dot", fr["held"] == "r")
    mix = kl.KbmState()
    for i in range(10):
        mix.move(5, 0, i * 0.01, "r")
    for i in range(10):
        mix.move(5, 0, 0.1 + i * 0.01, "m")
    fm = kl.motion_frame(mix, "comet", 200, {}, 0.2, "mx", "tail")
    shares = [p[4] for p in fm["trail"]]
    check("colors: your movement then a macro's blends across the switch", shares[0] == 0 and shares[-1] == 1
          and any(0 < v < 1 for v in shares))

    st = kl.KbmState()
    st.move(5, 0, 0.0)
    st.move(5, 0, 0.05)
    st.move(0, 5, 1.0)          # after a pause: a new burst
    check("state: a pause starts a new movement burst", st.burst[-1][1:] == (0.0, 5.0) and len(st.burst) == 2)


# ---------------------------------------------------------------------------
def replay_tests():
    import overlay_render as orr
    import overlay_server as osv
    d = Path(tempfile.mkdtemp())
    rb = osv.ReplayBuffer(d / "b.jsonl", 10.0, 2.0)
    T = 1_800_000_000.0
    rb.add({"t": T, "e": "kd", "k": "KEY_A", "s": "r"})
    for i in range(20):
        rb.add({"t": T + 0.5 + i * 0.001, "e": "mv", "dx": 1, "dy": 2, "s": "r"})
    rb.add({"t": T + 1.0, "e": "ku", "k": "KEY_A", "s": "r"})
    rb.add({"t": T + 1.1, "e": "kd", "k": "KEY_B", "s": "r"})
    rb.tick(T + 1.2)
    hdr, evs = orr.load_buffer(d / "b.jsonl")
    mv = [e for e in evs if e["e"] == "mv"]
    check("buffer: header identifies the format", hdr.get("format") == "puppetry-input-buffer" and hdr["version"] == 1)
    check("buffer: 1 kHz motion is folded into ~125 Hz steps, nothing lost",
          len(mv) <= 4 and sum(e["dx"] for e in mv) == 20 and sum(e["dy"] for e in mv) == 40)
    rb.add({"t": T + 20.0, "e": "kd", "k": "KEY_C", "s": "r"})
    rb.rewrite(T + 20.5)
    hdr, evs = orr.load_buffer(d / "b.jsonl")
    check("buffer: compaction drops what's older than length + margin",
          all(e["t"] >= T + 20.5 - 12 for e in evs) and [e["k"] for e in evs if e["e"] == "kd"] == ["KEY_C"])
    check("buffer: keys still held from before the window are in held_at_start",
          hdr["held_at_start"] == {"KEY_B": T + 1.1})
    with open(d / "b.jsonl", "a") as f:
        f.write('{"t": 1800000021.0, "e": "k')          # a line still being written
    _h, evs2 = orr.load_buffer(d / "b.jsonl")
    check("buffer: a half-written last line is ignored", len(evs2) == len(evs))
    rb.add({"t": T + 20.6, "e": "ax", "a": "LX", "v": 0.3, "s": "r"})
    for i in range(1, 40):                                   # a stick moving at 1 kHz
        rb.add({"t": T + 20.6 + i * 0.001, "e": "ax", "a": "LX", "v": round(0.3 + i * 0.01, 3), "s": "r"})
    rb.tick(T + 20.8)
    _h, evs3 = orr.load_buffer(d / "b.jsonl")
    axs = [e for e in evs3 if e["e"] == "ax"]
    check("buffer: controller axes are thinned to ~125 Hz, ending on the latest value",
          2 <= len(axs) <= 8 and axs[-1]["v"] == 0.69)
    rb.rewrite(T + 40)
    hdr3, _e = orr.load_buffer(d / "b.jsonl")
    check("buffer: axes still off-center at the window start are in axes_at_start",
          hdr3["axes_at_start"]["r"].get("LX") == 0.69)
    check("buffer: file is private (0600)", oct(os.stat(d / "b.jsonl").st_mode & 0o777) == "0o600")
    rb.close()


# ---------------------------------------------------------------------------
class FakeDaemon:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(str(path))
        self.srv.listen(4)
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            c.sendall(f"h {int(time.time() * 1e6)} 42\n".encode())    # KEY_LEFTSHIFT already held
            self.conns.append(c)

    def send(self, line):
        for c in self.conns:
            c.sendall((line + "\n").encode())

    def wait_client(self, timeout=5):
        t = time.time()
        while not self.conns and time.time() - t < timeout:
            time.sleep(0.02)
        return bool(self.conns)


def _get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def hub_tests():
    import overlay_server as osv
    cfg = oc.merged({"full": {"enabled": True, "port": 18480},
                     "simple": {"enabled": True, "port": 18481, "mouse_movement": True, "mouse_port": 18482},
                     "replay": {"enabled": True, "fallback_seconds": 20}})
    cfg["full"]["element_sources"] = True
    cfg["scene"]["elements"].append({"id": "pad", "type": "controller", "x": 24, "y": 0, "scale": 1})
    oc.save(cfg)
    fake = FakeDaemon(oc.event_socket())
    hub = osv.Hub(oc.load(), obs_factory=lambda o: (_ for _ in ()).throw(RuntimeError("no OBS here")))
    check("hub: connects to the daemon's event socket", fake.wait_client())
    time.sleep(0.2)
    check("hub: the hello line marks already-held keys", "KEY_LEFTSHIFT" in hub.state.held)
    st, ctype, body = _get("http://127.0.0.1:18480/")
    check("hub: full page served", st == 200 and "text/html" in ctype and b"drawKbm" in body)
    st, _c, body = _get("http://127.0.0.1:18480/config")
    c = json.loads(body)
    check("hub: /config has the style and the whole arrangement", c["style"]["unit"] == 48
          and len(c["layout"]["items"]) == len(kl.build_scene(cfg["scene"])["items"]) and c["piece"] == "scene")
    check("hub: simple + movement pages served", _get("http://127.0.0.1:18481/")[0] == 200
          and b"drawMotion" in _get("http://127.0.0.1:18482/")[2])
    st_el, _c, body_el = _get("http://127.0.0.1:18480/el/pad")
    cc = json.loads(_get("http://127.0.0.1:18480/config?el=pad")[2])
    check("hub: each element is its own page at /el/<id> (the controller here)",
          st_el == 200 and b"drawKbm" in body_el and cc["piece"] == "pad"
          and any(i["kind"] == "stick" for i in cc["layout"]["items"])
          and not any(i["kind"] == "key" for i in cc["layout"]["items"]) and cc["pad_labels"]["BTN_SOUTH"] == "A")
    check("hub: scripts served", b"motionFrame" in _get("http://127.0.0.1:18482/common.js")[2]
          and b"drawKbm" in _get("http://127.0.0.1:18480/kbm.js")[2])

    # SSE: open a stream, send an event, read it back
    s = socket.create_connection(("127.0.0.1", 18480), timeout=3)
    s.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
    got = b""
    t0 = time.time()
    while b"event: snapshot" not in got and time.time() - t0 < 3:
        got += s.recv(65536)
    now = int(time.time() * 1e6)
    fake.send(f"r {now} k 30 1")
    fake.send(f"r {now + 1000} m 7 -3")
    fake.send(f"m {now + 2000} k 48 1")
    fake.send(f"r {now + 3000} a 0 -5000")          # left stick halfway left
    fake.send(f"r {now + 3100} k 304 1")            # BTN_SOUTH
    fake.send(f"m {now + 4000} m 3 3")              # a macro moving the mouse
    t0 = time.time()
    while (b'"k":"KEY_B"' not in got or b'"e":"ax"' not in got or b'"dx":3,"dy":3' not in got) and time.time() - t0 < 3:
        got += s.recv(65536)
    s.close()
    text = got.decode()
    check("hub: SSE sends config and a snapshot first", "event: config" in text and "event: snapshot" in text)
    check("hub: SSE forwards key events with names", '"e":"kd","k":"KEY_A","s":"r"' in text)
    check("hub: SSE forwards mouse motion (batched)", '"e":"mv","dx":7,"dy":-3' in text)
    check("hub: macro output is marked as such", '"k":"KEY_B","s":"m"' in text)
    check("hub: macro output doesn't count as held real input", "KEY_B" not in hub.state.held
          and "KEY_B" in hub.state.out_held)
    check("hub: controller axes and buttons reach the pages", '"e":"ax","a":"LX","v":-0.5,"s":"r"' in text
          and '"k":"BTN_SOUTH"' in text and hub.state.axes.get("LX") == -0.5)
    check("hub: a macro's mouse movement is forwarded as macro movement",
          '"e":"mv","dx":3,"dy":3,"s":"m"' in text)
    time.sleep(0.3)
    lines = oc.buffer_file().read_text().splitlines()
    check("hub: replay file records the events", any('"k":"KEY_A"' in ln for ln in lines))
    status = json.loads(oc.status_file().read_text())
    check("hub: status file reports pages, file, and the fallback length when OBS is unreachable",
          set(status["pages"]) == {"full", "simple", "movement"}
          and status["element_urls"]["pad"] == "http://127.0.0.1:18480/el/pad"
          and status["replay"]["length_source"] == "fallback"
          and status["replay"]["length_s"] == 20)
    hub.poll_obs()
    check("hub: an unreachable OBS falls back to the configured length", hub.replay_length == 20
          and "no OBS here" in hub.obs_status)

    class FakeObs:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def replay_buffer_seconds(self):
            return 45.0
    hub.obs_factory = lambda o: FakeObs()
    hub.poll_obs()
    check("hub: the length OBS reports becomes the buffer length", hub.replay.length_s == 45.0
          and hub.length_source == "obs")

    # live config: a style change reaches the page config; a port change moves the server
    c2 = oc.load()
    c2["style"]["unit"] = 30
    c2["full"]["port"] = 18490
    oc.save(c2)
    os.utime(oc.config_file(), (time.time() + 5, time.time() + 5))
    t0 = time.time()
    while time.time() - t0 < 3 and "full" in hub.servers and hub.servers["full"].port != 18490:
        time.sleep(0.05)
    check("hub: overlay.json edits apply live (style + port)",
          json.loads(_get("http://127.0.0.1:18490/config")[2])["style"]["unit"] == 30)
    fake.srv.close()
    for cn in fake.conns:
        cn.close()
    time.sleep(0.5)
    check("hub: when the daemon goes away nothing stays held", not hub.state.held)
    c3 = oc.load()
    for k in ("full", "simple", "replay"):
        c3[k]["enabled"] = False
    oc.save(c3)
    os.utime(oc.config_file(), (time.time() + 10, time.time() + 10))
    t0 = time.time()
    while not hub.stopping and time.time() - t0 < 3:
        time.sleep(0.05)
    check("hub: turning everything off stops the helper", hub.stopping)
    hub.stop()


# ---------------------------------------------------------------------------
class FakeObsServer:
    """Speaks just enough obs-websocket v5 (auth included) for the client."""

    def __init__(self, password="secret", replay_seconds=90):
        self.password, self.replay_seconds = password, replay_seconds
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.inputs = {"Mic": {}}
        self.created = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    @staticmethod
    def _send(c, text):
        data = text.encode()
        head = bytes([0x81])
        head += bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack("!H", len(data))
        c.sendall(head + data)

    @staticmethod
    def _recv(c, buf):
        def need(n):
            while len(buf[0]) < n:
                buf[0] += c.recv(65536)
            out, buf[0] = buf[0][:n], buf[0][n:]
            return out
        b0, b1 = need(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", need(2))[0]
        mask = need(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(need(n)))
        return b0 & 0x0F, data

    def _client(self, c):
        buf = [b""]
        while b"\r\n\r\n" not in buf[0]:
            buf[0] += c.recv(4096)
        req, buf[0] = buf[0].split(b"\r\n\r\n", 1)
        key = [ln.split(b": ", 1)[1] for ln in req.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        salt, chal = "saltysalt", "challenge123"
        self._send(c, json.dumps({"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": salt, "challenge": chal}}}))
        op, data = self._recv(c, buf)
        ident = json.loads(data)
        secret = base64.b64encode(hashlib.sha256((self.password + salt).encode()).digest())
        want = base64.b64encode(hashlib.sha256(secret + chal.encode()).digest()).decode()
        if ident["d"].get("authentication") != want:
            c.sendall(bytes([0x88, 2]) + struct.pack("!H", 4009))
            c.close()
            return
        self._send(c, json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        while True:
            try:
                op, data = self._recv(c, buf)
            except (OSError, ValueError):
                return
            if op == 0x8:
                c.close()
                return
            r = json.loads(data)["d"]
            rt, rd = r["requestType"], r.get("requestData", {})
            ok, resp = True, {}
            if rt == "GetOutputList":
                resp = {"outputs": [{"outputName": "Replay Buffer (localized)", "outputKind": "replay_buffer"},
                                    {"outputName": "adv_file_output", "outputKind": "ffmpeg_muxer"}]}
            elif rt == "GetOutputSettings":
                ok = rd.get("outputName") == "Replay Buffer (localized)"
                resp = {"outputSettings": {"max_time_sec": self.replay_seconds}}
            elif rt == "GetCurrentProgramScene":
                resp = {"currentProgramSceneName": "Gaming"}
            elif rt == "GetInputList":
                resp = {"inputs": [{"inputName": n} for n in self.inputs]}
            elif rt == "CreateInput":
                self.inputs[rd["inputName"]] = rd["inputSettings"]
                self.created.append((rd["sceneName"], rd["inputName"], rd["inputKind"]))
            elif rt == "SetInputSettings":
                self.inputs[rd["inputName"]] = rd["inputSettings"]
            else:
                ok = False
            self._send(c, json.dumps({"op": 7, "d": {"requestType": rt, "requestId": r["requestId"],
                                                     "requestStatus": {"result": ok, "code": 100 if ok else 600},
                                                     "responseData": resp}}))


def obs_tests():
    from obs_client import ObsClient, ObsError
    fake = FakeObsServer("hunter2", 120)
    with ObsClient("127.0.0.1", fake.port, "hunter2") as c:
        secs = c.replay_buffer_seconds()
        srcs = [{"name": "Puppetry Input Overlay", "url": "http://127.0.0.1:17380/", "width": 1077, "height": 321}]
        first = c.add_browser_sources(srcs)
        again = c.add_browser_sources(srcs)
    check("obs: authenticates and reads the replay buffer length (found by output kind)", secs == 120.0)
    check("obs: Add to OBS creates a browser source in the current scene",
          first == [("Puppetry Input Overlay", "created")]
          and fake.created == [("Gaming", "Puppetry Input Overlay", "browser_source")])
    check("obs: running it again updates instead of duplicating", again == [("Puppetry Input Overlay", "updated")])
    check("obs: the source is sized to the page", fake.inputs["Puppetry Input Overlay"]["width"] == 1077)
    try:
        ObsClient("127.0.0.1", fake.port, "wrong").connect()
        bad = False
    except ObsError as e:
        bad = "password" in str(e).lower()
    check("obs: a wrong password gives a clear error", bad)
    try:
        ObsClient("127.0.0.1", 1, "").connect()
        unreachable = False
    except ObsError as e:
        unreachable = "WebSocket server" in str(e)
    check("obs: OBS not running gives a clear error", unreachable)


# ---------------------------------------------------------------------------
def render_tests():
    import overlay_render as orr
    import overlay_server as osv
    if not shutil.which("ffmpeg"):
        check("render: ffmpeg available (skipping render tests)", False)
        return
    d = Path(tempfile.mkdtemp())
    T = 1_800_000_000.0
    rb = osv.ReplayBuffer(d / "buf.jsonl", 60, 5)
    rb.add({"t": T + 0.5, "e": "kd", "k": "KEY_W", "s": "r"})
    for i in range(30):
        rb.add({"t": T + 0.6 + i * 0.01, "e": "mv", "dx": -6, "dy": 0, "s": "r"})
    rb.add({"t": T + 1.5, "e": "ku", "k": "KEY_W", "s": "r"})
    rb.tick(T + 2)
    rb.close()
    clip = d / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:size=640x360:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(clip)], check=True)
    cfg = oc.merged({})
    cfg["scene"]["elements"].append({"id": "controller", "type": "controller", "x": 24, "y": 0, "scale": 1})
    out = d / "out.mp4"
    r = orr.composite(clip, d / "buf.jsonl", out, clip_end=T + 2, cfg=cfg, position="top-left", margin=0)
    info = orr.probe(out)
    check("render: composite keeps the clip's size, length and fps",
          (info["width"], info["height"]) == (640, 360) and abs(info["duration"] - 2.0) < 0.15 and info["fps"] == 30)
    has_audio = "audio" in subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv",
                                           str(out)], capture_output=True, text=True).stdout
    check("render: audio is kept", has_audio)
    check("render: start = clip_end - duration", abs(r["start"] - T) < 1e-6 and r["coverage"] == "full")

    def frame_rgb(path, t, x, y):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
        w = orr.probe(path)["width"]
        i = (y * w + x) * 3
        return raw[i:i + 3]
    # the W key's position in the overlay (drawn at 35% of 640 px wide, top-left)
    lay = kl.build_scene(cfg["scene"])
    wkey = next(i for i in lay["items"] if i["name"] == "KEY_W")
    s = (640 * 0.35 // 2 * 2) / kl.layout_pixel_size(lay, cfg["style"])[0]
    u, pad = cfg["style"]["unit"] * s, cfg["style"]["padding"] * s
    wx, wy = int(pad + (wkey["x"] + 0.12) * u), int(pad + (wkey["y"] + 0.12) * u)
    held = frame_rgb(out, 1.0, wx, wy)
    released = frame_rgb(out, 1.9, wx, wy)
    check("render: a held key is drawn in the pressed color at the right time",
          held[0] > 150 and held[0] > held[2] + 40 and released[0] < 120)

    ov = d / "ov.mov"
    r2 = orr.render(d / "buf.jsonl", T, T + 1, ov, cfg=cfg, fps=30)
    pf = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,pix_fmt", "-of", "csv=p=0",
                         str(ov)], capture_output=True, text=True).stdout.strip()
    check("render: overlay-only video keeps transparency (qtrle argb)", pf == "qtrle,argb" and r2["frames"] == 30)
    r3 = orr.apply(clip, ov, d / "out2.mp4", position="bottom-right")
    check("render: apply puts a pre-rendered overlay on a clip", orr.probe(r3["out"])["width"] == 640)
    # render-while-OBS-writes: the overlay starts 0.5 s early; apply skips it forward
    early = d / "early.mov"
    orr.render(d / "buf.jsonl", T - 0.5, T + 2, early, cfg=cfg, fps=30)
    r4 = orr.apply(clip, early, d / "out3.mp4", position="top-left", margin=0, clip_end=T + 2, overlay_start=T - 0.5)
    check("render: apply lines an early-started overlay up with the clip", abs(r4["overlay_skip_s"] - 0.5) < 1e-6
          and frame_rgb(d / "out3.mp4", 1.0, wx, wy)[0] > 150 and frame_rgb(d / "out3.mp4", 1.9, wx, wy)[0] < 120)
    try:
        orr.apply(clip, early, d / "x.mp4", clip_end=T + 1.0, overlay_start=T + 0.5)
        late = False
    except orr.RenderError:
        late = True
    check("render: apply refuses an overlay that starts after the clip", late)
    for m in ("simple", "mouse"):
        rr = orr.render(d / "buf.jsonl", T, T + 1, d / f"o_{m}.webm", mode=m, cfg=cfg, fps=10)
        check(f"render: {m} mode renders", rr["frames"] == 10 and (d / f"o_{m}.webm").stat().st_size > 0)
    try:
        orr.render(d / "buf.jsonl", T, T + 1, d / "bad.mp4", cfg=cfg)
        bad = False
    except orr.RenderError:
        bad = True
    check("render: refuses an overlay format that would lose transparency", bad)

    st = kl.KbmState()
    st.key("KEY_LEFTCTRL", True, 0.0)
    st.key("KEY_A", True, 0.5)
    txt = orr.simple_text(st, dict(cfg["simple_style"], show_timers=True), 1.0)
    check("render: simple text matches the page's format", txt == "Ctrl 1.000 + A 0.500")
    st.key("KEY_E", True, 0.7, "m")
    st.key("BTN_SOUTH", True, 0.8)
    parts = orr.simple_parts(st, dict(cfg["simple_style"]), 1.0)
    check("render: simple list marks macro keys and names controller buttons",
          parts == [("Ctrl", "r"), ("A", "r"), ("E", "m"), ("A", "r")] or parts[2] == ("E", "m") and parts[3][0] == "A")
    check("render: old 'mouse_style' settings carry over to the movement page",
          oc.merged({"mouse_style": {"size": 321}})["movement_style"]["size"] == 321)

    # pieces, macro colors, controller
    T2 = T + 10
    rb2 = osv.ReplayBuffer(d / "pad.jsonl", 60, 5)
    rb2.add({"t": T2 + 0.2, "e": "kd", "k": "KEY_E", "s": "m"})
    rb2.add({"t": T2 + 0.2, "e": "ax", "a": "RT", "v": 1.0, "s": "r"})
    rb2.add({"t": T2 + 0.2, "e": "kd", "k": "BTN_EAST", "s": "m"})
    rb2.tick(T2 + 1)
    rb2.close()

    def overlay_rgba(path, t, x, y):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f",
                              "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True).stdout
        w = orr.probe(path)["width"]
        i = (y * w + x) * 4
        return raw[i:i + 4]
    style = cfg["style"]
    u, pad = style["unit"], style["padding"]
    kb = d / "kb.mov"
    orr.render(d / "pad.jsonl", T2, T2 + 1, kb, mode="keyboard", cfg=cfg, fps=10)
    lay = kl.build_scene(cfg["scene"], only="keyboard")
    e_key = next(i for i in lay["items"] if i["name"] == "KEY_E")
    px = overlay_rgba(kb, 0.5, int(pad + (e_key["x"] + 0.12) * u), int(pad + (e_key["y"] + 0.12) * u))
    check("render: a macro's key press is drawn in the output color (blue)", px[2] > 150 and px[2] > px[0] + 40)
    check("render: the picture's background is transparent", overlay_rgba(kb, 0.5, 1, 1)[3] == 0)
    padv = d / "pad.mov"
    orr.render(d / "pad.jsonl", T2, T2 + 1, padv, mode="controller", cfg=cfg, fps=10)
    play = kl.build_controller_layout()
    rt_item = next(i for i in play["items"] if i["name"] == "BTN_TR2")
    east = next(i for i in play["items"] if i["name"] == "BTN_EAST")
    trig = overlay_rgba(padv, 0.5, int(pad + (rt_item["x"] + 0.2) * u), int(pad + (rt_item["y"] + rt_item["h"] - 0.1) * u))
    eb = overlay_rgba(padv, 0.5, int(pad + (east["x"] + east["w"] / 2) * u), int(pad + (east["y"] + 0.12) * u))
    check("render: controller piece -- a fully pulled trigger fills with your color (orange)",
          trig[0] > 150 and trig[0] > trig[2] + 40)
    check("render: controller piece -- a macro's button press is blue", eb[2] > 150 and eb[2] > eb[0] + 40)
    check("render: controller piece size", (orr.probe(padv)["width"], orr.probe(padv)["height"])[0]
          == kl.layout_pixel_size(kl.build_controller_layout(), style)[0] // 2 * 2)
    bare = dict(cfg, scene={"elements": [{"id": "keyboard", "type": "keyboard", "x": 0, "y": 0, "scale": 1}]})
    jv = d / "joy.mov"
    orr.render(d / "pad.jsonl", T2, T2 + 1, jv, mode="mousepad", cfg=bare, fps=10)
    check("render: a type missing from the layout is drawn with its defaults", orr.probe(jv)["width"] > 0)
    try:
        orr.render(d / "pad.jsonl", T2, T2 + 1, d / "bad.mov", mode="el:nope", cfg=bare, fps=10)
        bad = False
    except orr.RenderError:
        bad = True
    check("render: an unknown el:<id> is an error", bad)
    for ext, dec in ((".mov", []), (".mkv", []), (".webm", ["-c:v", "libvpx-vp9"])):
        f = d / f"alpha{ext}"
        orr.render(d / "pad.jsonl", T2, T2 + 0.3, f, mode="el:mouse", cfg=cfg, fps=10)
        raw = subprocess.run(["ffmpeg", "-v", "error", *dec, "-i", str(f), "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "rgba", "-"], capture_output=True).stdout
        check(f"render: {ext} keeps transparency" + (" (decoded with libvpx)" if dec else ""),
              raw[3] == 0 and max(raw[3::4]) == 255)
    # afterglow editor flow: render ahead -> align to the clip -> layer pieces at chosen spots
    ahead = d / "ahead.mov"
    orr.render(d / "buf.jsonl", T - 0.5, T + 2, ahead, cfg=cfg, fps=30, mode="keyboard")
    al = orr.align(ahead, d / "aligned.mov", overlay_start=T - 0.5, clip_start=T, duration=2.0)
    ai = orr.probe(d / "aligned.mov")
    check("align: stream-copies the overlay to start with the clip and match its length",
          abs(al["skip_s"] - 0.5) < 1e-6 and abs(ai["duration"] - 2.0) < 0.05)
    lay_kb = kl.build_scene(cfg["scene"], only="keyboard")
    wk = next(i for i in lay_kb["items"] if i["name"] == "KEY_W")
    ov_w = orr.probe(d / "aligned.mov")["width"]
    res = orr.layer(clip, [{"file": d / "aligned.mov", "x": 0.5, "y": 0.5, "w": 0.5},
                           {"file": padv, "x": 0.0, "y": 0.0, "w": 0.25}], d / "layered.mp4")
    s_k = (640 * 0.5 // 2 * 2) / ov_w
    held_px = frame_rgb(d / "layered.mp4", 1.0, int(320 + (pad + (wk["x"] + 0.12) * u) * s_k),
                        int(180 + (pad + (wk["y"] + 0.12) * u) * s_k))
    check("layer: pieces land where they're put, lined up in time", res["pieces"] == 2 and held_px[0] > 150
          and held_px[0] > held_px[2] + 40)

    # CLI
    import overlay_cli
    from io import StringIO
    old = sys.stdout
    sys.stdout = buf = StringIO()
    rc = overlay_cli.main(["render", "--buffer", str(d / "buf.jsonl"), "--start", str(T), "--end", str(T + 0.5),
                           "--fps", "10", str(d / "cli.mov")])
    rc2 = overlay_cli.main(["composite", "--clip", "/nonexistent.mp4", "--clip-end", "1", str(d / "x.mp4")])
    sys.stdout = old
    outs = [json.loads(ln) for ln in buf.getvalue().splitlines()]
    check("cli: render prints one JSON result", rc == 0 and outs[0]["frames"] == 5)
    check("cli: failures print {\"error\"} and exit 1", rc2 == 1 and "error" in outs[1])
    srcs = overlay_cli.obs_sources(oc.merged({"full": {"enabled": True}, "simple": {"enabled": True, "mouse_movement": True}}))
    check("cli: Add to OBS makes one source per enabled page, sized to it",
          [s["name"] for s in srcs] == ["Puppetry Input Overlay", "Puppetry Input List", "Puppetry Mouse Movement"]
          and (srcs[0]["width"], srcs[0]["height"]) == kl.layout_pixel_size(kl.build_scene(None), oc.DEFAULTS["style"]))
    srcs2 = overlay_cli.obs_sources(oc.merged({"full": {"enabled": True, "split": True}, "controller": {"enabled": True}}))
    check("cli: old split + controller settings -> one source per element (controller added to the scene)",
          [s["name"] for s in srcs2] == ["Puppetry: keyboard", "Puppetry: mouse", "Puppetry: comet",
                                         "Puppetry: controller"]
          and srcs2[3]["url"].endswith("/el/controller"))
    try:
        orr.FrameMaker("el:nope", cfg)
        bad_el = False
    except orr.RenderError as e:
        bad_el = "no element" in str(e)
    check("render: an unknown element is a clear error", bad_el)


# ---------------------------------------------------------------------------
def js_parity_tests():
    node = shutil.which("node")
    if not node:
        print("SKIP js parity (no node)")
        return
    web = Path(__file__).resolve().parent / "overlay_web"
    # the same input + the same frame times through both implementations
    events = []
    t = 0.0
    for i in range(50):
        events.append(["mv", t, 37, -12 if i < 25 else 15, "r"])
        t += 0.008
    for i in range(30):
        events.append(["mv", t, -50, 3, "m" if i % 3 == 0 else "r"])
        t += 0.008
    events += [["kd", t, "BTN_LEFT", "r"], ["wh", t + 0.01, 1, "r"], ["kd", t + 0.02, "BTN_MIDDLE", "m"],
               ["kd", t + 0.025, "BTN_SIDE", "r"], ["kd", t + 0.026, "BTN_EXTRA", "r"]]
    times = [t + 0.03 + k * 0.016 for k in range(12)]
    style = {"screen_height": 900, "pad_fraction": 60, "trail_seconds": 0.5, "joystick_speed": 2500}
    cases = [("comet", "tail", True, False), ("comet", "head", True, True), ("comet", "tail", False, False),
             ("mousepad", "tail", True, False), ("mousepad", "tail", False, True), ("joystick", "tail", True, False)]

    def py_frames():
        out = []
        for kind, center, auto, inv in cases:
            st = kl.KbmState()
            for e in events:
                if e[0] == "mv":
                    st.move(e[2], e[3], e[1], e[4])
                elif e[0] == "kd":
                    st.key(e[2], True, e[1], e[3])
                else:
                    st.wheel(e[2], e[1], e[3])
            for tt in times:
                fr = kl.motion_frame(st, kind, 240, dict(style, auto_zoom=auto, invert_side_rings=inv), tt, "v", center)
            out.append(fr)
        return out
    js_events = json.dumps(events)
    script = (f"global.performance={{now:()=>0}};\n{(web / 'common.js').read_text()}\n"
              f"const ev={js_events}, times={json.dumps(times)}, style={json.dumps(style)}, cases={json.dumps(cases)};\n"
              "const res=[];\n"
              "for (const [kind, center, auto, inv] of cases) {\n"
              "  P.motion=[]; P.clicks=[]; P.wheels=[]; P.views={}; P.held={}; P.out={}; P.released={};\n"
              "  for (const e of ev) {\n"
              "    if (e[0]==='mv') addMotion(e[1], e[2], e[3], e[4]);\n"
              "    else if (e[0]==='kd') { P.clicks.push([e[1], e[2], e[3]]); (e[3]==='r'?P.held:P.out)[e[2]]=e[1]; }\n"
              "    else P.wheels.push([e[1], e[2] > 0 ? 1 : -1, e[3]]);\n"
              "  }\n"
              "  let fr; for (const tt of times) fr = motionFrame(P, kind, 240, Object.assign({}, style, {auto_zoom: auto, invert_side_rings: inv}), tt, 'v', center);\n"
              "  res.push(fr);\n"
              "}\n"
              "console.log(JSON.stringify({res, h: formatHold(75.5)}));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    try:
        js = json.loads(out.stdout)
    except ValueError:
        check("js: common.js runs", False)
        print(out.stderr[-2000:])
        return

    def close(a, b):
        if isinstance(a, dict):
            return set(a) == set(b) and all(close(a[k], b[k]) for k in a)
        if isinstance(a, (list, tuple)):
            return len(a) == len(b) and all(close(x, y) for x, y in zip(a, b))
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(a - b) < 1e-6
        return a == b
    pyr = json.loads(json.dumps(py_frames()))
    for (kind, center, auto, inv), p_fr, j_fr in zip(cases, pyr, js["res"]):
        check(f"js: {kind} ({center}, auto zoom {'on' if auto else 'off'}{', inverted' if inv else ''}) "
              "draws exactly what Python draws",
              close(p_fr, j_fr))
    check("js: the page's timer format matches Python", js["h"] == kl.format_hold(75.5))
    lay = json.loads(json.dumps(kl.build_scene(None)))
    check("js: layout survives JSON (what the page receives)", lay == kl.build_scene(None))


def main() -> int:
    layout_tests()
    replay_tests()
    obs_tests()
    hub_tests()
    render_tests()
    js_parity_tests()
    print(f"\n{_checks - len(_fail)}/{_checks} checks passed.")
    if _fail:
        print("FAILED:", _fail)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
