"""
Puppetry's on-disk config I/O for the Qt GUI -- reads/writes the exact
same files the C++ daemon uses (~/.config/macro-daemon/state.json,
macros.json, profiles/*.json, aliases.json), same paths and format as
native/src/config.cpp and the old Python daemon.

Theme settings live under state.json's "theme" key (one config location
for the whole app). Kit widgets call the theme provider from
constructors AND paintEvents, so state.json reads are cached by mtime
(UI_THEMING_GUIDE.md pitfall #1).

Also: locating the native helper binaries (daemon, transcriber), and
the two things the GUI asks them for -- validating a macro exactly the
way the daemon will compile it, and the key-name tables.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket as _socket
import subprocess
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from ui_kit.theme_config import ThemeSettings

CONFIG_DIR = Path.home() / ".config" / "macro-daemon"
STATE_FILE = CONFIG_DIR / "state.json"
MACROS_FILE = CONFIG_DIR / "macros.json"
ALIASES_FILE = CONFIG_DIR / "aliases.json"
PROFILES_DIR = CONFIG_DIR / "profiles"
CONTROL_SOCKET = CONFIG_DIR / "control.sock"

# Puppetry's own placeholder theme -- swap these hex values for whatever
# the user provides (see UI_THEMING_GUIDE.md section 2).
PUPPETRY_THEME_DEFAULTS = ThemeSettings()

DEFAULT_PROFILE_NAMES = ["Profile 1", "Profile 2", "Profile 3"]


# ---------------------------------------------------------------------------
# state.json (cached)
# ---------------------------------------------------------------------------

class _StateCache:
    """load_readonly() returns a shared cached object (never mutate it);
    load() returns a deep copy that's safe to change and save."""

    def __init__(self) -> None:
        self._key = None
        self._data: dict[str, Any] | None = None

    def load_readonly(self) -> dict[str, Any]:
        try:
            stat = STATE_FILE.stat()
        except FileNotFoundError:
            return _default_state()
        key = (stat.st_mtime_ns, stat.st_size)
        if self._data is None or key != self._key:
            try:
                self._data = json.loads(STATE_FILE.read_text())
            except (OSError, ValueError):
                return _default_state()
            self._key = key
        return self._data

    def load(self) -> dict[str, Any]:
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
        "record_time_seconds": 3.0,
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
    for i, name in enumerate(DEFAULT_PROFILE_NAMES, start=1):
        path = PROFILES_DIR / f"profile_{i}.json"
        if not path.exists():
            path.write_text(json.dumps({"name": name, "enabled": {}}, indent=2))


def load_state() -> dict[str, Any]:
    return _state_cache.load()


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
    _state_cache.invalidate()


_theme_cache: tuple[Any, ThemeSettings | None] = (None, None)


def load_theme_settings() -> ThemeSettings:
    """The theme provider (ui_kit.theme_config.set_settings_provider).
    Called from paintEvents, so it's an identity check in the common
    case: rebuilt only when state.json itself was re-read."""
    global _theme_cache
    state = _state_cache.load_readonly()
    if _theme_cache[0] is state and _theme_cache[1] is not None:
        return _theme_cache[1]
    merged = asdict(PUPPETRY_THEME_DEFAULTS)
    merged.update({k: v for k, v in (state.get("theme") or {}).items() if k in merged})
    theme = ThemeSettings(**merged)
    _theme_cache = (state, theme)
    return theme


# ---------------------------------------------------------------------------
# macros / aliases / profiles
# ---------------------------------------------------------------------------

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
    (PROFILES_DIR / f"{profile_id}.json").write_text(json.dumps(profile, indent=2))


def delete_profile(profile_id: str) -> None:
    path = PROFILES_DIR / f"{profile_id}.json"
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# daemon process + control socket
# ---------------------------------------------------------------------------

def restart_daemon_service() -> tuple[bool, str]:
    """Applies saved config the same way the old GTK editor did: restart
    the service (the daemon has no live-reload, by design)."""
    try:
        result = subprocess.run(["systemctl", "--user", "restart", "macro-daemon.service"],
                                capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return False, f"couldn't restart daemon: {exc}"
    if result.returncode == 0:
        return True, "Saved. Daemon restarted."
    return False, f"Saved, but restart failed: {result.stderr.strip() or result.returncode}"


def send_control_command(payload: dict[str, Any], timeout: float = 3.0) -> tuple[bool, str]:
    """Client for the daemon's JSON-line control socket (FIRE / ABORT /
    PAUSE / RESUME -- see native/src/control_socket.cpp)."""
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


# ---------------------------------------------------------------------------
# native helper binaries
# ---------------------------------------------------------------------------

def find_binary(name: str) -> str | None:
    """$PUPPETRY_BIN_DIR (set by the Nix wrapper), then a dev build next
    to this checkout (native/build), then $PATH."""
    candidates = []
    if os.environ.get("PUPPETRY_BIN_DIR"):
        candidates.append(Path(os.environ["PUPPETRY_BIN_DIR"]) / name)
    candidates.append(Path(__file__).resolve().parent.parent / "native" / "build" / name)
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which(name)


def check_macro(macro_def: dict[str, Any]) -> tuple[bool, str]:
    """Compiles the macro with EXACTLY the daemon's compilers (embedded
    Python or the native fast path) via `puppetry-daemon --check`. If the
    daemon binary can't be found, falls back to a Python syntax check
    for python_on macros (python_off can't be checked without it)."""
    daemon = find_binary("puppetry-daemon")
    if daemon:
        try:
            result = subprocess.run([daemon, "--check"], input=json.dumps(macro_def),
                                    capture_output=True, text=True, timeout=15)
            msg = (result.stdout.strip() or result.stderr.strip())
            return result.returncode == 0, msg or "OK"
        except Exception as exc:
            return False, f"couldn't run the macro checker: {exc}"
    if macro_def.get("python_on", True):
        import textwrap
        body = macro_def.get("code") or "pass"
        try:
            compile("def _macro(*_a, **_k):\n" + textwrap.indent(body, "    ") + "\n", "<macro>", "exec")
        except SyntaxError as exc:
            return False, f"SyntaxError: {exc}"
        return True, "OK (daemon binary not found -- only Python syntax was checked)"
    return True, "not checked (daemon binary not found)"


@lru_cache(maxsize=1)
def name_tables() -> dict[str, Any]:
    """{"simplified": {short: KEY_*}, "keys": [every KEY_*/BTN_* name]},
    straight from the daemon's own tables (`puppetry-daemon --dump-names`)
    so the reference panels can't drift from what the daemon resolves."""
    daemon = find_binary("puppetry-daemon")
    if daemon:
        try:
            out = subprocess.run([daemon, "--dump-names"], capture_output=True, text=True, timeout=10).stdout
            return json.loads(out)
        except Exception:
            pass
    return {"simplified": {}, "keys": []}
