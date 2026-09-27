"""
Settings page: everything that used to sit on the old main screen --
profiles (switch / new / rename / delete / reorder), keyboard + mouse
devices (Detect, name/path toggle, full device list), abort key, record
time, autosave, Save -- plus the appearance (theme) editor.
"""
from __future__ import annotations

from dataclasses import asdict

from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

import puppetry_config as cfg
from input_tools import DetectDevice, DetectKey, list_all_devices
from model import AppModel
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.custom_group_box import CustomGroupBox
from ui_kit.custom_message_dialog import show_message
from ui_kit.custom_spinbox import CustomDoubleSpinBox
from ui_kit.theme_editor import ThemeEditorGroup
from ui_kit.theme import Theme
from widgets import Collapsible, PageBase, ask, dim_label, label_style, prompt_text


class SettingsPage(PageBase):
    def __init__(self, model: AppModel, parent=None):
        super().__init__(parent)
        self.model = model
        self._threads = []
        text = Theme().text()

        # ------------------------------------------------------------ status + save
        top = QHBoxLayout()
        self.status = QLabel("")
        self.status.setStyleSheet(label_style(text.darker(130)))
        top.addWidget(self.status, stretch=1)
        self.save_btn = CustomButton("Save (restarts daemon)")
        self.save_btn.clicked.connect(model.save)
        top.addWidget(self.save_btn)
        self.content_layout.addLayout(top)
        model.status.connect(self.status.setText)

        # ------------------------------------------------------------ profiles
        prof = CustomGroupBox("Profiles")
        pl = prof.make_layout(QVBoxLayout)
        pl.addWidget(dim_label("The active profile decides which macros are enabled (the switch on each macro row)."))
        self.profile_rows = QVBoxLayout()
        pl.addLayout(self.profile_rows)
        new_prof = CustomButton("+ New Profile")
        new_prof.clicked.connect(self._new_profile)
        row = QHBoxLayout()
        row.addWidget(new_prof)
        row.addStretch(1)
        pl.addLayout(row)
        self.content_layout.addWidget(prof)
        model.profiles_changed.connect(self.refresh_profiles)
        self.refresh_profiles()

        # ------------------------------------------------------------ devices
        dev = CustomGroupBox("Devices")
        dl = dev.make_layout(QVBoxLayout)
        self.dev_labels = {}
        self.show_paths = {}
        for kind, detect_text in (("keyboard", "Detect (press a key)"), ("mouse", "Detect (move or click)")):
            r = QHBoxLayout()
            r.addWidget(QLabel("Keyboard" if kind == "keyboard" else "Mouse"))
            lbl = QLabel()
            self.dev_labels[kind] = lbl
            r.addWidget(lbl, stretch=1)
            eye = CustomButton("\U0001F441")
            eye.setCheckable(True)
            eye.setToolTip("Show the raw device path instead of its name")
            eye.toggled.connect(lambda _on: self.refresh_devices())
            self.show_paths[kind] = eye
            r.addWidget(eye)
            det = CustomButton(detect_text)
            det.clicked.connect(lambda _=False, k=kind, b=det, t=detect_text: self._detect(k, b, t))
            r.addWidget(det)
            dl.addLayout(r)
        self.all_devices = QVBoxLayout()
        all_box = QWidget()
        all_box.setLayout(self.all_devices)
        dl.addWidget(Collapsible("All input devices", all_box))
        refresh = CustomButton("Refresh device list")
        refresh.clicked.connect(self.refresh_device_list)
        dl.addWidget(refresh)
        self.content_layout.addWidget(dev)
        model.devices_changed.connect(self.refresh_devices)
        self.refresh_devices()
        self.refresh_device_list()

        # ------------------------------------------------------------ abort key
        ab = CustomGroupBox("Abort key")
        al = ab.make_layout(QVBoxLayout)
        r = QHBoxLayout()
        self.abort_label = QLabel(model.state.get("abort_key") or "KEY_PAUSE")
        r.addWidget(self.abort_label, stretch=1)
        self.abort_btn = CustomButton("Change")
        self.abort_btn.clicked.connect(self._change_abort)
        r.addWidget(self.abort_btn)
        al.addLayout(r)
        al.addWidget(dim_label("Instantly stops every running macro, releases stuck keys/clicks, "
                               "releases any ignore() grab and clears every actAs(). Takes effect after Save."))
        self.content_layout.addWidget(ab)

        # ------------------------------------------------------------ behavior
        beh = CustomGroupBox("Behavior")
        bl = beh.make_layout(QVBoxLayout)
        r = QHBoxLayout()
        r.addWidget(QLabel("Record time (seconds a combo must be held still)"))
        self.record_time = CustomDoubleSpinBox()
        self.record_time.setRange(0.5, 30.0)
        self.record_time.setSingleStep(0.5)
        self.record_time.setValue(float(model.state.get("record_time_seconds", 3.0)))
        self.record_time.valueChanged.connect(lambda v: model.set_pref("record_time_seconds", v))
        r.addWidget(self.record_time)
        r.addStretch(1)
        bl.addLayout(r)
        self.autosave = CustomCheckBox("Auto-save on change")
        self.autosave.setChecked(bool(model.state.get("autosave", False)))
        self.autosave.toggled.connect(self._autosave_toggled)
        bl.addWidget(self.autosave)

        self.flat_accel = CustomCheckBox("No pointer acceleration on Puppetry's virtual mouse")
        self.flat_accel.setChecked(bool(model.state.get("disable_pointer_accel", True)))
        self.flat_accel.setToolTip("Recommended. KDE otherwise applies its usual mouse acceleration curve to\n"
                                   "Puppetry's virtual mouse, so a macro asking to move 400px moves however\n"
                                   "far the curve decides -- and a newly-created virtual mouse starts out\n"
                                   "accelerated until it's turned off by hand in System Settings.\n\n"
                                   "The daemon writes this one device's entry in kcminputrc on startup; your\n"
                                   "real mouse's settings aren't touched. KDE only.")
        self.flat_accel.toggled.connect(lambda on: model.set_pref("disable_pointer_accel", on))
        bl.addWidget(self.flat_accel)
        bl.addWidget(dim_label("Applies on the next daemon start (any Save restarts it). Turning it back "
                               "off leaves the setting in place -- change it in System Settings -> Mouse."))
        self.content_layout.addWidget(beh)

        # ------------------------------------------------------------ appearance
        self.theme_editor = ThemeEditorGroup(current=cfg.load_theme_settings(), defaults=cfg.PUPPETRY_THEME_DEFAULTS,
                                             labels={"app_theme_enabled": "Puppetry Theme Colors"})
        self.content_layout.addWidget(self.theme_editor)
        save_theme = CustomButton("Save Appearance")
        save_theme.clicked.connect(self._save_theme)
        r = QHBoxLayout()
        r.addWidget(save_theme)
        r.addStretch(1)
        self.content_layout.addLayout(r)
        self.content_layout.addStretch(1)

        model.dirty_changed.connect(lambda d: self.save_btn.setEnabled(True))

    # ------------------------------------------------------------------ profiles

    def refresh_profiles(self) -> None:
        while self.profile_rows.count():
            w = self.profile_rows.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        profiles = self.model.ordered_profiles()
        for i, (pid, name) in enumerate(profiles):
            w = QWidget()
            r = QHBoxLayout(w)
            r.setContentsMargins(0, 0, 0, 0)
            up = CustomButton("▲")
            up.setEnabled(i > 0)
            up.clicked.connect(lambda _=False, p=pid: self.model.move_profile(p, -1))
            down = CustomButton("▼")
            down.setEnabled(i < len(profiles) - 1)
            down.clicked.connect(lambda _=False, p=pid: self.model.move_profile(p, 1))
            active = pid == self.model.profile_id
            select = CustomButton(("✓ " if active else "") + name)
            if active:
                select.set_fill_color(Theme().highlight())
            select.clicked.connect(lambda _=False, p=pid: self._select_profile(p))
            select.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            rename = CustomButton("✎")
            rename.setToolTip("Rename")
            rename.clicked.connect(lambda _=False, p=pid, n=name: self._rename_profile(p, n))
            delete = CustomButton("−")
            delete.setToolTip("Delete")
            delete.clicked.connect(lambda _=False, p=pid, n=name: self._delete_profile(p, n))
            for b in (up, down):
                r.addWidget(b)
            r.addWidget(select, stretch=1)
            r.addWidget(rename)
            r.addWidget(delete)
            self.profile_rows.addWidget(w)

    def _select_profile(self, pid: str) -> None:
        if pid == self.model.profile_id:
            return
        if self.model.dirty:
            choice = ask(self, "You have unsaved changes.", "", ["Cancel", "Discard", "Save"], default=2)
            if choice == 2:
                self.model.save()
            elif choice != 1:
                return
            else:
                # Discard: drop in-memory edits by reloading from disk.
                self.model.macros_data = cfg.load_macros()
        self.model.switch_profile(pid)

    def _new_profile(self) -> None:
        name = prompt_text(self, "New profile", "")
        if name is None:
            return
        pid = self.model.add_profile(name)
        self._select_profile(pid)

    def _rename_profile(self, pid: str, current: str) -> None:
        name = prompt_text(self, "Rename profile", current)
        if name is not None:
            self.model.rename_profile(pid, name)

    def _delete_profile(self, pid: str, name: str) -> None:
        if ask(self, f"Delete profile '{name}'?", "This can't be undone.", ["Cancel", "Delete"]) != 1:
            return
        err = self.model.delete_profile(pid)
        if err:
            self.status.setText(err)

    # ------------------------------------------------------------------ devices

    def refresh_devices(self) -> None:
        for kind, lbl in self.dev_labels.items():
            path, name = self.model.device(kind)
            if not path:
                lbl.setText("(not set)")
            elif self.show_paths[kind].isChecked():
                lbl.setText(path)
            else:
                lbl.setText(name or path)

    def refresh_device_list(self) -> None:
        while self.all_devices.count():
            w = self.all_devices.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        devices = list_all_devices()
        if not devices:
            self.all_devices.addWidget(dim_label("No readable input devices (check the `input` group / udev rule)."))
        for path, name in devices:
            w = QWidget()
            r = QHBoxLayout(w)
            r.setContentsMargins(0, 0, 0, 0)
            tags = [t for t, p in (("current keyboard", kb), ("current mouse", mouse)) if p == path]
            r.addWidget(QLabel(f"{name}  ({path})" + (f"  [{', '.join(tags)}]" if tags else "")), stretch=1)
            for kind in ("keyboard", "mouse"):
                b = CustomButton(f"Set as {kind.capitalize()}")
                b.clicked.connect(lambda _=False, k=kind, p=path, n=name: self._set_device_now(k, p, n))
                r.addWidget(b)
            self.all_devices.addWidget(w)

    def _set_device_now(self, kind: str, path: str, name: str) -> None:
        # Immediate + permanent, like the old --testing panel.
        self.model.set_device(kind, path, name, persist_now=True)
        self.status.setText(f"Set {kind}: {name} (saved -- takes effect after the daemon restarts)")
        self.refresh_device_list()

    def _detect(self, kind: str, button: CustomButton, text: str) -> None:
        button.setEnabled(False)
        button.setText("Listening…")
        self.status.setText("Move or click your mouse now…" if kind == "mouse" else "Press a key now…")
        t = DetectDevice(kind, parent=self)

        def done(path, name):
            button.setEnabled(True)
            button.setText(text)
            if path:
                self.model.set_device(kind, path, name)
                self.status.setText(f"Detected {kind}: {name or path} -- Save to apply.")
            else:
                self.status.setText(f"No {kind} input detected in 10s -- the device may lack permission "
                                    "(check the `input` group / udev rule), or a different device was used.")
            self._threads.remove(t)

        t.found.connect(done)
        self._threads.append(t)
        t.start()

    def _change_abort(self) -> None:
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        self.abort_btn.setEnabled(False)
        self.abort_btn.setText("Listening…")
        self.status.setText("Press the key you want as the abort/panic key…")
        t = DetectKey(kb, mouse, parent=self)

        def done(code, name):
            self.abort_btn.setEnabled(True)
            self.abort_btn.setText("Change")
            if code is not None:
                self.abort_label.setText(name)
                self.model.set_abort_key(name)
                self.status.setText(f"Abort key set to {name} -- takes effect after Save.")
            else:
                self.status.setText("No key detected in 10s -- kept the previous abort key.")
            self._threads.remove(t)

        t.found.connect(done)
        self._threads.append(t)
        t.start()

    # ------------------------------------------------------------------ misc

    def _autosave_toggled(self, on: bool) -> None:
        self.model.set_pref("autosave", on)
        if on and self.model.dirty:
            self.model.save()
        self.model.dirty_changed.emit(self.model.dirty)

    def _save_theme(self) -> None:
        theme = cfg.load_theme_settings()
        from ui_kit.theme_config import ThemeSettings
        target = ThemeSettings(**asdict(theme))
        bad = self.theme_editor.apply_to(target)
        self.model.set_pref("theme", asdict(target))
        msg = "Appearance saved. Reopen Puppetry to see it everywhere."
        if bad:
            msg += "\n\nThese colors weren't valid and were kept as before: " + ", ".join(bad)
        show_message(self, "Appearance", msg)

    def stop_threads(self) -> None:
        for t in list(self._threads):
            t.wait(11000)
