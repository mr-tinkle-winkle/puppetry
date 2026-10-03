"""
"Show it to viewers only": the on-screen overlay's twin inside OBS.

A screen share captures what the monitor shows, so an overlay only viewers
see can't live on the monitor. It lives in OBS instead, as ONE Browser
Source ("Puppetry: Viewers only") placed with the on-screen overlay's own
settings (overlay.json "screen": content, position, size, opacity, distance
from the edge), mapped from screen pixels onto OBS's canvas. Viewers of
OBS's output -- a stream, a recording, or OBS's projector window shared in
Discord -- see it; your monitor doesn't.

    puppetry-overlay share [toggle|show|hide|update|window|status]

  toggle / show / hide   the source's visibility in the current program scene
                         (placed / re-placed first). The keybind is `toggle`.
  update                 re-place it, visibility unchanged (after a settings change)
  window                 open OBS's program output as a window (to share in Discord)
  status                 {"exists", "visible", ...}

The page it shows (the full layout or the simple list) must be served by the
overlay helper; when it's off, `show` turns it on and restarts the daemon
(like flipping its switch on the Input Visualizer page).
"""
from __future__ import annotations

import time
import urllib.request

import overlay_config as oc

SOURCE = "Puppetry: Viewers only"
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


def _item_id(c, scene: str):
    from obs_client import ObsError
    try:
        return c.request("GetSceneItemId", {"sceneName": scene, "sourceName": SOURCE})["sceneItemId"]
    except (ObsError, KeyError):
        return None


def place(c, cfg: dict) -> tuple:
    """Create or update the source and its scene item in the current program scene,
    on top, at the planned spot. -> (scene, item id, plan)."""
    vs = c.request("GetVideoSettings")
    p = plan(cfg, int(vs.get("baseWidth", 1920)), int(vs.get("baseHeight", 1080)))
    scene = _scene(c)
    settings = {"url": p["url"], "width": p["width"], "height": p["height"], "css": p["css"],
                "shutdown": False, "restart_when_active": False}
    inputs = {i.get("inputName") for i in c.request("GetInputList").get("inputs", [])}
    if SOURCE in inputs:
        c.request("SetInputSettings", {"inputName": SOURCE, "inputSettings": settings, "overlay": True})
    else:
        c.request("CreateInput", {"sceneName": scene, "inputName": SOURCE, "inputKind": "browser_source",
                                  "inputSettings": settings, "sceneItemEnabled": False})
    item = _item_id(c, scene)
    if item is None:                     # the source exists, just not in this scene
        item = c.request("CreateSceneItem", {"sceneName": scene, "sourceName": SOURCE,
                                             "sceneItemEnabled": False})["sceneItemId"]
    c.request("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item, "sceneItemTransform": {
        "positionX": float(p["x"]), "positionY": float(p["y"]), "scaleX": p["scale"], "scaleY": p["scale"],
        "rotation": 0.0, "alignment": 5, "boundsType": "OBS_BOUNDS_NONE",
        "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0}})
    n = len(c.request("GetSceneItemList", {"sceneName": scene}).get("sceneItems", []))
    c.request("SetSceneItemIndex", {"sceneName": scene, "sceneItemId": item, "sceneItemIndex": max(0, n - 1)})
    return scene, item, p


def share(action: str = "toggle", cfg: dict | None = None, client_factory=None) -> dict:
    cfg = cfg or oc.load()
    client_factory = client_factory or _client
    if action == "window":
        with client_factory(cfg) as c:
            c.request("OpenVideoMixProjector", {"videoMixType": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM"})
        return {"window": "OBS program output (windowed projector)"}
    if action == "status":
        with client_factory(cfg) as c:
            scene = _scene(c)
            item = _item_id(c, scene)
            vis = bool(item is not None and c.request("GetSceneItemEnabled", {
                "sceneName": scene, "sceneItemId": item}).get("sceneItemEnabled"))
        return {"exists": item is not None, "visible": vis, "scene": scene}
    if action not in ("toggle", "show", "hide", "update"):
        raise ValueError(f"unknown share action {action!r}")
    if action != "hide":
        cfg = ensure_page(cfg)
    with client_factory(cfg) as c:
        scene = _scene(c)
        item = _item_id(c, scene)
        was = bool(item is not None and c.request("GetSceneItemEnabled", {
            "sceneName": scene, "sceneItemId": item}).get("sceneItemEnabled"))
        want = {"show": True, "hide": False, "toggle": not was, "update": was}[action]
        if action == "hide" and item is None:
            return {"visible": False, "scene": scene}
        if want or action == "update":
            scene, item, p = place(c, cfg)
        else:
            p = None
        c.request("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item, "sceneItemEnabled": want})
    out = {"visible": want, "scene": scene}
    if p:
        out.update(page=p["page"], x=p["x"], y=p["y"], scale=round(p["scale"], 4))
    return out
