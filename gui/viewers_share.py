"""
"Show it to viewers only": the on-screen overlay's twin inside OBS -- in a
scene of its own, so OBS's clips, recordings and stream never get it.

A screen share captures what the monitor shows, so an overlay only viewers
see can't live on the monitor. It lives in OBS instead, in the scene
"Puppetry: Share": your current program scene nested at the bottom (so it
shows whatever OBS shows) and ONE Browser Source ("Puppetry: Viewers only")
on top, placed with the on-screen overlay's own settings (overlay.json
"screen": content, position, size, opacity, distance from the edge), mapped
from screen pixels onto OBS's canvas. OBS records/streams/clips its PROGRAM
scene only, so the share scene stays out of all of them; a projector window
of it is what gets shared (Discord, a call).

    puppetry-overlay share [toggle|show|hide|update|window|projector|status]

  toggle / show / hide   the overlay's visibility in the share scene (re-synced
                         and re-placed first). The keybind is `toggle`.
  update                 re-sync + re-place, visibility unchanged (settings changed)
  window                 a projector window of the share scene (the overlay is
                         placed first if it never was; nothing is ever added twice)
  projector              a projector window of OBS's program output, nothing else
  status                 {"exists", "visible", ...}

Everything is idempotent: the scene, the source and its scene item are each
created once and updated after that. A "Puppetry: Viewers only" item left in
a program scene (Session 27 put it there) is removed, so clips stay clean.

The page it shows (the full layout or the simple list) must be served by the
overlay helper; when it's off, `show` turns it on and restarts the daemon
(like flipping its switch on the Input Visualizer page).
"""
from __future__ import annotations

import time
import urllib.request

import overlay_config as oc

SOURCE = "Puppetry: Viewers only"
SHARE_SCENE = "Puppetry: Share"
_CSS = ("body {{ background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; "
        "opacity: {opacity}; }}")


def page_for(cfg: dict) -> str:
    return "simple" if cfg.get("screen", {}).get("content") == "simple" else "full"


def page_size(cfg: dict, page: str) -> tuple:
    if page == "simple":
        return int(cfg["simple"].get("width", 900)), int(max(20, cfg["simple_style"]["font_px"] * 1.6))
    import kbm_layout as kl
    return kl.layout_pixel_size(kl.build_scene(cfg["scene"]), cfg["style"])


def plan(cfg: dict, canvas_w: int, canvas_h: int) -> dict:
    """Where the source goes on a canvas_w x canvas_h OBS canvas, from the on-screen
    overlay's settings. Screen pixels -> canvas pixels by canvas height / the
    screen height Puppetry knows (style.screen_height), so it looks the same size
    in OBS as on the monitor when the canvas matches the monitor."""
    from screen_overlay import placement
    sc = dict(oc.DEFAULTS["screen"], **cfg.get("screen", {}))
    page = page_for(cfg)
    w, h = page_size(cfg, page)
    screen_h = float(cfg.get("style", {}).get("screen_height") or canvas_h) or float(canvas_h)
    k = canvas_h / screen_h
    scale = max(0.1, float(sc.get("scale", 60)) / 100.0) * k
    dw, dh = int(round(w * scale)), int(round(h * scale))
    x, y = placement((0, 0, canvas_w, canvas_h), dw, dh, sc.get("position", "bottom-right"),
                     int(round(float(sc.get("margin", 24)) * k)))
    opacity = max(0.05, min(1.0, float(sc.get("opacity", 100)) / 100.0))
    return {"page": page, "url": oc.urls(cfg).get(page) or f"http://127.0.0.1:{_port(cfg, page)}/",
            "width": int(w), "height": int(h), "x": x, "y": y, "scale": scale,
            "css": _CSS.format(opacity=f"{opacity:g}")}


def _port(cfg: dict, page: str) -> int:
    return int(cfg["simple"]["port"] if page == "simple" else cfg["full"]["port"])


def _page_enabled(cfg: dict, page: str) -> bool:
    return bool(cfg["simple" if page == "simple" else "full"].get("enabled"))


def ensure_page(cfg: dict, wait_s: float = 12.0) -> dict:
    """Make sure the overlay helper serves the page; turn it on (and restart the
    daemon, which starts the helper) when it's off. Returns the config in use."""
    page = page_for(cfg)
    url = f"http://127.0.0.1:{_port(cfg, page)}/"
    if not _page_enabled(cfg, page):
        cfg["simple" if page == "simple" else "full"]["enabled"] = True
        oc.save(cfg)
        import puppetry_config as cfg_mod
        cfg_mod.restart_daemon_service()
    deadline = time.time() + wait_s
    while True:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return cfg
        except OSError:
            pass
        if time.time() > deadline:
            raise RuntimeError(f"the overlay page ({url}) isn't up -- is the Puppetry daemon running?")
        time.sleep(0.3)


def _client(cfg: dict):
    from obs_client import ObsClient
    o = cfg["obs"]
    return ObsClient(o["host"], o["port"], o.get("password", ""))


def _scene(c) -> str:
    s = c.request("GetCurrentProgramScene")
    return s.get("currentProgramSceneName") or s.get("sceneName")


def _items(c, scene: str) -> list:
    return c.request("GetSceneItemList", {"sceneName": scene}).get("sceneItems", [])


def _item_id(c, scene: str, source: str = SOURCE):
    for it in _items(c, scene):
        if it.get("sourceName") == source:
            return it.get("sceneItemId")
    return None


def _is_visible(c, scene: str, item) -> bool:
    return bool(item is not None and c.request("GetSceneItemEnabled", {
        "sceneName": scene, "sceneItemId": item}).get("sceneItemEnabled"))


def sync_scene(c) -> str:
    """The share scene exists and shows the current program scene underneath (only
    that one; a stale nested scene from before a scene switch is replaced). Any
    overlay item in the program scene is removed (clips must stay clean).
    -> the program scene's name."""
    program = _scene(c)
    if program == SHARE_SCENE:
        raise RuntimeError(f'OBS is showing "{SHARE_SCENE}" live -- switch OBS back to your normal scene '
                           "(that one is for the share window only)")
    scenes = {s.get("sceneName") for s in c.request("GetSceneList").get("scenes", [])}
    if SHARE_SCENE not in scenes:
        c.request("CreateScene", {"sceneName": SHARE_SCENE})
    stray = _item_id(c, program, SOURCE)
    if stray is not None:
        c.request("RemoveSceneItem", {"sceneName": program, "sceneItemId": stray})
    have_base = False
    for it in _items(c, SHARE_SCENE):
        name = it.get("sourceName")
        if name == SOURCE:
            continue
        if name == program and not have_base:
            have_base = True
            continue
        c.request("RemoveSceneItem", {"sceneName": SHARE_SCENE, "sceneItemId": it["sceneItemId"]})
    if not have_base:
        base = c.request("CreateSceneItem", {"sceneName": SHARE_SCENE, "sourceName": program,
                                             "sceneItemEnabled": True})["sceneItemId"]
        c.request("SetSceneItemIndex", {"sceneName": SHARE_SCENE, "sceneItemId": base, "sceneItemIndex": 0})
    return program


def place(c, cfg: dict) -> tuple:
    """Create or update the overlay source and its item in the share scene, on top,
    at the planned spot. Never creates a second one. -> (item id, plan)."""
    vs = c.request("GetVideoSettings")
    p = plan(cfg, int(vs.get("baseWidth", 1920)), int(vs.get("baseHeight", 1080)))
    settings = {"url": p["url"], "width": p["width"], "height": p["height"], "css": p["css"],
                "shutdown": False, "restart_when_active": False}
    inputs = {i.get("inputName") for i in c.request("GetInputList").get("inputs", [])}
    if SOURCE in inputs:
        c.request("SetInputSettings", {"inputName": SOURCE, "inputSettings": settings, "overlay": True})
    else:
        c.request("CreateInput", {"sceneName": SHARE_SCENE, "inputName": SOURCE, "inputKind": "browser_source",
                                  "inputSettings": settings, "sceneItemEnabled": False})
    item = _item_id(c, SHARE_SCENE)
    if item is None:                     # the source exists, just not in the share scene
        item = c.request("CreateSceneItem", {"sceneName": SHARE_SCENE, "sourceName": SOURCE,
                                             "sceneItemEnabled": False})["sceneItemId"]
    c.request("SetSceneItemTransform", {"sceneName": SHARE_SCENE, "sceneItemId": item, "sceneItemTransform": {
        "positionX": float(p["x"]), "positionY": float(p["y"]), "scaleX": p["scale"], "scaleY": p["scale"],
        "rotation": 0.0, "alignment": 5, "boundsType": "OBS_BOUNDS_NONE",
        "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0}})
    n = len(_items(c, SHARE_SCENE))
    c.request("SetSceneItemIndex", {"sceneName": SHARE_SCENE, "sceneItemId": item, "sceneItemIndex": max(0, n - 1)})
    return item, p


def share(action: str = "toggle", cfg: dict | None = None, client_factory=None) -> dict:
    cfg = cfg or oc.load()
    client_factory = client_factory or _client
    if action not in ("toggle", "show", "hide", "update", "window", "projector", "status"):
        raise ValueError(f"unknown share action {action!r}")
    if action == "projector":
        with client_factory(cfg) as c:
            c.request("OpenVideoMixProjector", {"videoMixType": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM"})
        return {"projector": "OBS program output"}
    if action == "status":
        with client_factory(cfg) as c:
            scenes = {s.get("sceneName") for s in c.request("GetSceneList").get("scenes", [])}
            item = _item_id(c, SHARE_SCENE) if SHARE_SCENE in scenes else None
            return {"exists": item is not None, "visible": _is_visible(c, SHARE_SCENE, item),
                    "scene": SHARE_SCENE}
    if action in ("toggle", "show", "update", "window"):
        cfg = ensure_page(cfg)
    with client_factory(cfg) as c:
        program = sync_scene(c)
        item = _item_id(c, SHARE_SCENE)
        was = _is_visible(c, SHARE_SCENE, item)
        if action == "window":
            want = was if item is not None else True       # first time: the share window shows the overlay
        else:
            want = {"show": True, "hide": False, "toggle": not was, "update": was}[action]
        p = None
        if want or action in ("update", "window") or item is not None:
            item, p = place(c, cfg)
        if item is not None:
            c.request("SetSceneItemEnabled", {"sceneName": SHARE_SCENE, "sceneItemId": item,
                                              "sceneItemEnabled": want})
        if action == "window":
            c.request("OpenSourceProjector", {"sourceName": SHARE_SCENE})
    out = {"visible": want, "scene": SHARE_SCENE, "program": program}
    if p:
        out.update(page=p["page"], x=p["x"], y=p["y"], scale=round(p["scale"], 4))
    if action == "window":
        out["window"] = f'projector of "{SHARE_SCENE}"'
    return out
