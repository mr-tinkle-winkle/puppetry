"""
AppModel -- the single in-memory copy of everything the GUI edits:
state.json, macros.json, and the active profile. Every page reads and
writes through it, so the Macros page, the editor and Settings can never
disagree about unsaved changes.

Save semantics (same as the old GTK app):
  - most edits mark the model dirty; "Save" writes macros + profile +
    state and restarts the daemon (which is how config takes effect);
  - with Autosave on, every such edit saves immediately;
  - pure UI preferences (record time, autosave itself, transcription
    options, ping key, profile order) are written to state.json at once
    and never need a daemon restart.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

import puppetry_config as cfg


class AppModel(QObject):
    SAVE_COOLDOWN_S = 1.0
    macros_changed = Signal()      # list contents/structure changed -> rebuild rows
    dirty_changed = Signal(bool)
    status = Signal(str)
    profiles_changed = Signal()
    devices_changed = Signal()
    categories_toggled = Signal(str)

    def __init__(self):
        super().__init__()
        cfg.ensure_config_exists()
        self.state: dict[str, Any] = cfg.load_state()
        self.macros_data: dict[str, Any] = cfg.load_macros()
        self.profile_id: str = self.state.get("active_profile") or "profile_1"
        self.profile: dict[str, Any] = cfg.load_profile(self.profile_id)
        self.dirty = False
        # Save throttling (autosave): the first save after a quiet second
        # happens at once; any save requested within a second of the last
        # one waits until the requests stop for a full second, then ONE
        # save writes everything and restarts the daemon once.
        self._last_save = 0.0
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(int(self.SAVE_COOLDOWN_S * 1000))
        self._save_timer.timeout.connect(self.save)

    # ------------------------------------------------------------------ macros

    def macros(self) -> list[dict[str, Any]]:
        return self.macros_data.setdefault("macros", [])

    def find(self, macro_id: str | None) -> dict[str, Any] | None:
        return next((m for m in self.macros() if m.get("id") == macro_id), None)

    def set_field(self, macro_id: str, key: str, value: Any) -> None:
        m = self.find(macro_id)
        if m is None or m.get(key) == value:
            return
        m[key] = value
        self.mark_dirty()

    def is_enabled(self, macro_id: str) -> bool:
        return bool(self.profile.get("enabled", {}).get(macro_id, False))

    def set_enabled(self, macro_id: str, enabled: bool) -> None:
        if self.is_enabled(macro_id) == enabled:
            return
        self.profile.setdefault("enabled", {})[macro_id] = enabled
        self.mark_dirty()

    def commit_macro(self, macro: dict[str, Any]) -> bool:
        """From the editor. Returns True if it was new. New macros start
        enabled -- on the profile they were created on only."""
        existing = self.find(macro.get("id"))
        if existing is None:
            if not macro.get("id"):
                macro["id"] = str(uuid.uuid4())
            self.macros().append(dict(macro))
            self.profile.setdefault("enabled", {})[macro["id"]] = True
            self.macros_changed.emit()
            return True
        existing.clear()
        existing.update(macro)
        self.macros_changed.emit()
        return False

    def delete_macro(self, macro_id: str) -> None:
        self.macros_data["macros"] = [m for m in self.macros() if m.get("id") != macro_id]
        self.profile.get("enabled", {}).pop(macro_id, None)
        self.macros_changed.emit()
        self.mark_dirty()

    # ------------------------------------------------------------------ categories
    #
    # macros.json: "categories": [{"name": str, "enabled": bool}] (ordered)
    # and each macro's "category" (absent/"" = Uncategorized). A category
    # switched off disables every macro in it regardless of the profile
    # (the daemon applies it at startup). Global -- not per profile.
    # Collapsed/expanded is a UI pref in state.json.

    def categories(self) -> list[dict[str, Any]]:
        cats = self.macros_data.setdefault("categories", [])
        known = {c.get("name") for c in cats}
        for m in self.macros():   # a macro naming an unknown category gets one
            name = m.get("category") or ""
            if name and name not in known:
                cats.append({"name": name, "enabled": True})
                known.add(name)
        return cats

    def category(self, name: str) -> dict[str, Any] | None:
        return next((c for c in self.categories() if c.get("name") == name), None)

    def category_names(self) -> list[str]:
        return [c.get("name", "") for c in self.categories()]

    def category_enabled(self, name: str) -> bool:
        c = self.category(name) if name else None
        return True if c is None else bool(c.get("enabled", True))

    def is_effectively_enabled(self, macro_id: str) -> bool:
        m = self.find(macro_id)
        return self.is_enabled(macro_id) and self.category_enabled((m or {}).get("category") or "")

    def add_category(self, name: str) -> str:
        base = name.strip() or "New Category"
        name, n = base, 2
        while self.category(name) is not None:
            name, n = f"{base} {n}", n + 1
        self.categories().append({"name": name, "enabled": True})
        self.macros_changed.emit()
        self.mark_dirty()
        return name

    def rename_category(self, old: str, new: str) -> None:
        new = new.strip()
        c = self.category(old)
        if c is None or not new or new == old or self.category(new) is not None:
            return
        c["name"] = new
        for m in self.macros():
            if m.get("category") == old:
                m["category"] = new
        collapsed = self.state.get("collapsed_categories") or []
        if old in collapsed:
            self.set_pref("collapsed_categories", [new if x == old else x for x in collapsed])
        self.macros_changed.emit()
        self.mark_dirty()

    def delete_category(self, name: str) -> None:
        """Its macros move to Uncategorized (nothing is deleted)."""
        self.macros_data["categories"] = [c for c in self.categories() if c.get("name") != name]
        for m in self.macros():
            if m.get("category") == name:
                m.pop("category", None)
        self.macros_changed.emit()
        self.mark_dirty()

    def move_category(self, name: str, direction: int) -> None:
        cats = self.categories()
        i = next((k for k, c in enumerate(cats) if c.get("name") == name), None)
        if i is None or not 0 <= i + direction < len(cats):
            return
        cats[i], cats[i + direction] = cats[i + direction], cats[i]
        self.macros_changed.emit()
        self.mark_dirty()

    def set_category_enabled(self, name: str, enabled: bool) -> None:
        c = self.category(name)
        if c is None or bool(c.get("enabled", True)) == enabled:
            return
        c["enabled"] = enabled
        self.categories_toggled.emit(name)
        self.mark_dirty()

    def set_macro_category(self, macro_id: str, name: str) -> None:
        m = self.find(macro_id)
        if m is None or (m.get("category") or "") == (name or ""):
            return
        if name:
            if self.category(name) is None:
                self.categories().append({"name": name, "enabled": True})
            m["category"] = name
        else:
            m.pop("category", None)
        self.macros_changed.emit()
        self.mark_dirty()

    def is_collapsed(self, name: str) -> bool:
        return name in (self.state.get("collapsed_categories") or [])

    def set_collapsed(self, name: str, collapsed: bool) -> None:
        cur = [x for x in (self.state.get("collapsed_categories") or []) if x != name]
        if collapsed:
            cur.append(name)
        self.set_pref("collapsed_categories", cur)

    # ------------------------------------------------------------------ saving

    def mark_dirty(self) -> None:
        if self.state.get("autosave"):
            self.request_save()
            return
        if not self.dirty:
            self.dirty = True
            self.dirty_changed.emit(True)
        self.status.emit("Unsaved changes")

    def request_save(self) -> None:
        """Throttled save (see __init__): immediate unless a save happened
        within the last second, in which case it's batched."""
        if self._save_timer.isActive() or time.monotonic() - self._last_save < self.SAVE_COOLDOWN_S:
            if not self.dirty:
                self.dirty = True
                self.dirty_changed.emit(True)
            self._save_timer.start()   # (re)start: waits for a quiet second
            self.status.emit("Saving in a moment…")
            return
        self.save()

    def save_pending(self) -> bool:
        return self._save_timer.isActive()

    def flush_pending_save(self) -> None:
        if self._save_timer.isActive():
            self.save()

    def save(self) -> bool:
        self._save_timer.stop()
        self.state["active_profile"] = self.profile_id
        try:
            cfg.save_macros(self.macros_data)
            cfg.save_profile(self.profile_id, self.profile)
            cfg.save_state(self.state)
        except Exception as exc:
            self.status.emit(f"Save failed, config unchanged on disk: {exc}")
            return False
        ok, msg = cfg.restart_daemon_service()
        self._last_save = time.monotonic()
        self.dirty = False
        self.dirty_changed.emit(False)
        self.status.emit(msg)
        return True

    def set_pref(self, key: str, value: Any) -> None:
        """UI preference: written to state.json immediately, no restart."""
        self.state[key] = value
        try:
            # Only this key -- never flush other pending (unsaved) state
            # changes such as a new device path to disk as a side effect.
            on_disk = cfg.load_state()
            on_disk[key] = value
            cfg.save_state(on_disk)
        except Exception:
            pass

    # ------------------------------------------------------------------ devices / abort key

    def set_device(self, kind: str, path: str | None, name: str | None, persist_now: bool = False) -> None:
        self.state[f"{kind}_path"] = path
        self.state[f"{kind}_name"] = name
        self.devices_changed.emit()
        if persist_now:
            self.set_pref(f"{kind}_path", path)
            self.set_pref(f"{kind}_name", name)
        else:
            self.mark_dirty()

    def device(self, kind: str) -> tuple[str | None, str | None]:
        return self.state.get(f"{kind}_path"), self.state.get(f"{kind}_name")

    def set_abort_key(self, name: str) -> None:
        self.state["abort_key"] = name
        self.mark_dirty()

    # ------------------------------------------------------------------ profiles

    def ordered_profiles(self) -> list[tuple[str, str]]:
        raw = cfg.list_profile_ids()
        names = dict(raw)
        if self.profile_id in names:
            names[self.profile_id] = self.profile.get("name", names[self.profile_id])
        order = [pid for pid in (self.state.get("profile_order") or []) if pid in names]
        order += [pid for pid, _ in raw if pid not in order]
        return [(pid, names[pid]) for pid in order]

    def switch_profile(self, new_id: str) -> None:
        """Caller resolves unsaved changes first (discard or save)."""
        self.profile_id = new_id
        self.profile = cfg.load_profile(new_id)
        self.set_pref("active_profile", new_id)
        self.dirty = False
        self.dirty_changed.emit(False)
        self.profiles_changed.emit()
        self.macros_changed.emit()
        # The daemon only reads the active profile at startup.
        ok, msg = cfg.restart_daemon_service()
        self.status.emit(f"Switched to {self.profile.get('name', new_id)}. " + ("" if ok else msg))

    def add_profile(self, name: str) -> str:
        new_id = f"profile_{uuid.uuid4().hex[:8]}"
        cfg.save_profile(new_id, {"name": name.strip() or "New Profile", "enabled": {}})
        order = [pid for pid, _ in self.ordered_profiles()]
        if new_id not in order:
            order.append(new_id)
        self.set_pref("profile_order", order)
        self.profiles_changed.emit()
        return new_id

    def rename_profile(self, profile_id: str, name: str) -> None:
        prof = self.profile if profile_id == self.profile_id else cfg.load_profile(profile_id)
        prof["name"] = name.strip() or prof.get("name", profile_id)
        # Renaming is saved at once (it never affects which macros run).
        disk = cfg.load_profile(profile_id)
        disk["name"] = prof["name"]
        cfg.save_profile(profile_id, disk)
        self.profiles_changed.emit()

    def delete_profile(self, profile_id: str) -> str | None:
        """Returns an error string, or None on success."""
        profiles = self.ordered_profiles()
        if len(profiles) <= 1:
            return "Can't delete the last profile."
        ids = [pid for pid, _ in profiles]
        idx = ids.index(profile_id)
        cfg.delete_profile(profile_id)
        self.set_pref("profile_order", [pid for pid in ids if pid != profile_id])
        if profile_id == self.profile_id:
            remaining = [pid for pid in ids if pid != profile_id]
            self.switch_profile(remaining[max(0, idx - 1)])
        self.profiles_changed.emit()
        return None

    def move_profile(self, profile_id: str, direction: int) -> None:
        ids = [pid for pid, _ in self.ordered_profiles()]
        i = ids.index(profile_id)
        j = i + direction
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            self.set_pref("profile_order", ids)
            self.profiles_changed.emit()
