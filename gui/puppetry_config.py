"""
Puppetry's own config I/O for the Qt GUI -- reads/writes the exact same
on-disk files the C++ daemon uses (~/.config/macro-daemon/state.json,
macros.json, profiles/*.json, aliases.json), so the GUI and the daemon
never disagree about where config lives. Mirrors native/src/config.cpp's
paths and defaults exactly.

Also hosts Puppetry's ThemeSettings storage: rather than a separate TOML
file, theme values live under state.json's "theme" key -- one config
location for the whole app, matching how state.json already holds
device paths / active profile / abort key.

Caching per UI_THEMING_GUIDE.md pitfall #1: kit widgets call
get_settings() (theme_config.py) from constructors AND paintEvents, so
re-parsing JSON on every call is a measured, real performance problem
(afterglow: 400+ calls per refresh, over half the refresh time). This
module caches state.json by mtime and only re-reads when it actually
changed.
"""
from __future__ import annotations

import json
import os
import socket as _socket
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ui_kit.theme_config import ThemeSettings

CONFIG_DIR = Path.home() / ".config" / "macro-daemon"
STATE_FILE = CONFIG_DIR / "state.json"
MACROS_FILE = CONFIG_DIR / "macros.json"
ALIASES_FILE = CONFIG_DIR / "aliases.json"
PROFILES_DIR = CONFIG_DIR / "profiles"
CONTROL_SOCKET = CONFIG_DIR / "control.sock"

# Puppetry's own placeholder theme -- swap these hex values for
# whatever the user provides (see UI_THEMING_GUIDE.md section 2). Left
# as the kit's own monochrome placeholder until real colors are given;
# ask which role each provided color maps to rather than guessing.
PUPPETRY_THEME_DEFAULTS = ThemeSettings()


class _StateCache:
    """Caches state.json by (mtime, size), same two-entry-point shape as
    the guide's own pitfall #1 fix: `load()` for a mutate-and-save copy,
    `load_readonly()` for the shared cached object kit widgets pull
    through via the theme provider. Never mutate what load_readonly()
    returns."""

    def __init__(self) -> None:
        self._mtime: float | None = None
        self._size: int | None = None
        self._data: dict[str, Any] | None = None

    def load_readonly(self) -> dict[str, Any]:
        try:
            stat = STATE_FILE.stat()
        except FileNotFoundError:
            return _default_state()
        if self._data is not None and stat.st_mtime == self._mtime and stat.st_size == self._size:
            return self._data
        self._data = json.loads(STATE_FILE.read_text())
        self._mtime, self._size = stat.st_mtime, stat.st_size
        return self._data

    def load(self) -> dict[str, Any]:
        import copy
        return copy.deepcopy(self.load_readonly())

    def invalidate(self) -> None:
        self._data = None


_state_cache = _StateCache()


def _default_state() -> dict[str, Any]:
    return {
        "keyboard_path": None, "keyboard_name": None,
        "mouse_path": None, "mouse_name": None,
        "active_profile": "profile_1", "autosave": False,
        "abort_key": "KEY_PAUSE",
        "theme": asdict(PUPPETRY_THEME_DEFAULTS),
    }


def ensure_config_exists() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(_default_state(), indent=2))
    if not MACROS_FILE.exists():
        MACROS_FILE.write_text(json.dumps({"macros": []}, indent=2))
    if not ALIASES_FILE.exists():
        ALIASES_FILE.write_text(json.dumps({"aliases": {}}, indent=2))
    for i, name in enumerate(["Profile 1", "Profile 2", "Profile 3"], start=1):
        path = PROFILES_DIR / f"profile_{i}.json"
        if not path.exists():
            path.write_text(json.dumps({"name": name, "enabled": {}}, indent=2))


def load_state() -> dict[str, Any]:
    return _state_cache.load()


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
    _state_cache.invalidate()


def load_theme_settings() -> ThemeSettings:
    """The function to hand to ui_kit.theme_config.set_settings_provider().
    Returns the SHARED cached object underlying load_readonly()'s
    "theme" key, turned into a ThemeSettings -- cheap enough to call
    from paintEvent, per the guide's own requirement."""
    state = _state_cache.load_readonly()
    theme_dict = state.get("theme") or {}
    # Merge onto defaults so a state.json saved before a new theme field
    # existed doesn't crash -- same "stale saved config" concern the
    # guide's pitfall #11 warns about, just for missing keys rather than
    # a stale default value.
    merged = asdict(PUPPETRY_THEME_DEFAULTS)
    merged.update(theme_dict)
    return ThemeSettings(**{k: merged[k] for k in asdict(PUPPETRY_THEME_DEFAULTS)})


def save_theme_settings(theme: ThemeSettings) -> None:
    state = load_state()
    state["theme"] = asdict(theme)
    save_state(state)


def load_macros() -> dict[str, Any]:
    if not MACROS_FILE.exists():
        return {"macros": []}
    return json.loads(MACROS_FILE.read_text())


def save_macros(data: dict[str, Any]) -> None:
    MACROS_FILE.write_text(json.dumps(data, indent=2))


def load_aliases() -> dict[str, Any]:
    if not ALIASES_FILE.exists():
        return {"aliases": {}}
    return json.loads(ALIASES_FILE.read_text())


def save_aliases(data: dict[str, Any]) -> None:
    ALIASES_FILE.write_text(json.dumps(data, indent=2))


def list_profile_ids() -> list[tuple[str, str]]:
    result = []
    if not PROFILES_DIR.exists():
        return result
    for path in sorted(PROFILES_DIR.glob("profile_*.json")):
        try:
            data = json.loads(path.read_text())
            result.append((path.stem, data.get("name", path.stem)))
        except Exception:
            result.append((path.stem, path.stem))
    return result


def load_profile(profile_id: str) -> dict[str, Any]:
    path = PROFILES_DIR / f"{profile_id}.json"
    if not path.exists():
        return {"name": profile_id, "enabled": {}}
    return json.loads(path.read_text())


def save_profile(profile_id: str, profile: dict[str, Any]) -> None:
    path = PROFILES_DIR / f"{profile_id}.json"
    path.write_text(json.dumps(profile, indent=2))


def restart_daemon_service() -> None:
    """Applies an on-disk config change the same way the old GTK editor
    did: `systemctl --user restart macro-daemon`, NOT a live-reload
    message -- the C++ daemon has no live-reload logic either, same
    design as the Python one it replaces."""
    os.system("systemctl --user restart macro-daemon")


def send_control_command(payload: dict[str, Any], timeout: float = 3.0) -> tuple[bool, str]:
    """Synchronous client for the daemon's control socket -- replaces
    macro_daemon.py's send_control_command() now that the daemon is a
    separate C++ process this GUI never links against. Same JSON-line
    protocol (see native/src/control_socket.cpp): one line in, one line
    out. Used by the `puppetry --name=.../--abort` CLI and by the combo
    recorder's PAUSE/RESUME, NOT by ordinary editing (that still only
    touches disk + restart_daemon_service(), same as before)."""
    if not CONTROL_SOCKET.exists():
        return False, "macro-daemon isn't running (no control socket found)"
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(CONTROL_SOCKET))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        raw = sock.recv(65536).decode("utf-8", "replace").strip()
        sock.close()
    except OSError as exc:
        return False, f"couldn't reach macro-daemon: {exc}"
    try:
        resp = json.loads(raw)
    except (ValueError, TypeError):
        return False, raw or "no response from macro-daemon"
    if resp.get("ok"):
        return True, resp.get("message", "OK")
    return False, resp.get("error", "unknown error")
