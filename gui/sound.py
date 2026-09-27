"""
Fire-and-forget sound playback for the GUI (transcription start/finish
cues).

Why an external player rather than QtMultimedia: QSoundEffect only
handles wav, and QMediaPlayer needs the FFmpeg backend plugins, which on
NixOS are a separate output that may or may not be in the wrapped
environment -- a missing plugin there fails at runtime with no useful
error. A player binary on PATH is something we can check for up front and
report honestly, and spawning it detached means a slow or hung player
can't touch the GUI's event loop.

Players are tried in order of preference: the first few handle every
format anyone is likely to pick (mp3 included), the last two are
wav/ogg/flac-only fallbacks that are almost always present on a PipeWire
or ALSA system anyway.
"""
from __future__ import annotations

import os
import shutil

from PySide6.QtCore import QProcess

# (binary, args-before-file, handles-compressed-formats)
PLAYERS = [
    ("mpv", ["--no-video", "--really-quiet", "--no-terminal"], True),
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"], True),
    ("mpg123", ["-q"], True),
    ("paplay", [], False),
    ("aplay", ["-q"], False),
]

AUDIO_FILTER = "Audio (*.wav *.mp3 *.ogg *.oga *.flac *.opus *.m4a *.aac);;All files (*)"
_COMPRESSED = {".mp3", ".m4a", ".aac", ".opus"}

_cached_player: tuple[str, list[str], bool] | None = None
_cache_checked = False


def find_player(prefer_compressed: bool = True) -> tuple[str, list[str], bool] | None:
    """The first available player, as (absolute path, leading args, handles
    compressed). Cached -- PATH doesn't change under a running GUI."""
    global _cached_player, _cache_checked
    if _cache_checked and (_cached_player is None or _cached_player[2] or not prefer_compressed):
        return _cached_player
    for name, args, compressed in PLAYERS:
        path = shutil.which(name)
        if path and (compressed or not prefer_compressed):
            _cached_player, _cache_checked = (path, args, compressed), True
            return _cached_player
    # Nothing that handles compressed audio -- take anything at all.
    for name, args, compressed in PLAYERS:
        path = shutil.which(name)
        if path:
            _cached_player, _cache_checked = (path, args, compressed), True
            return _cached_player
    _cached_player, _cache_checked = None, True
    return None


def describe_players() -> str:
    """For a tooltip: what we'd use, or what to install."""
    found = find_player()
    if found is None:
        return "No audio player found on PATH (looked for: " + ", ".join(p[0] for p in PLAYERS) + ")."
    return f"Played with {os.path.basename(found[0])}."


def play(path: str) -> str | None:
    """Starts `path` playing, detached. Returns an error message, or None
    on success. Never raises and never blocks -- a cue that can't play must
    not get in the way of the thing it was announcing."""
    if not path:
        return None
    if not os.path.isfile(path):
        return f"Sound file not found: {path}"
    ext = os.path.splitext(path)[1].lower()
    player = find_player(prefer_compressed=ext in _COMPRESSED)
    if player is None:
        return ("No audio player found on PATH -- install one of: "
                + ", ".join(p[0] for p in PLAYERS) + ".")
    exe, args, compressed = player
    if ext in _COMPRESSED and not compressed:
        return f"{os.path.basename(exe)} can't play {ext} files -- use a wav/ogg/flac file, or install mpv."
    ok, _pid = QProcess.startDetached(exe, [*args, path])
    if not ok:
        return f"Couldn't start {os.path.basename(exe)}."
    return None
