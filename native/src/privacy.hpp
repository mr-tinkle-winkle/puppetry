#pragma once
// Blocking the input visualizer: while blocked, the event stream sends
// nothing (EventStream::set_paused), so nothing reaches the OBS pages, the
// layered replay buffer (afterglow), the in-app picture or the on-screen
// overlay. Two sources, either one blocks:
//
//   manual   the user's toggle (control-socket VISUALIZER command:
//            `puppetry-overlay block toggle`, a keybind, the GUI). Kept in
//            $XDG_RUNTIME_DIR/puppetry/visualizer_blocked so a daemon
//            restart (every macro save) never silently unblocks.
//   apps     overlay.json "ignored_apps": [{"match": "class"|"title",
//            "value": "...", "when": "focused"|"open"}]. Checked every
//            0.5 s through kdotool (KWin); value is a case-insensitive
//            substring of the window class (app id) or title.
//
// State for the GUI: $XDG_RUNTIME_DIR/puppetry/privacy.json
//   {"blocked": bool, "manual": bool, "app": "...", "why": "manual"|"focused"|"open"|"",
//    "watching": bool (kdotool answers), "rules": n}
#include <string>
#include <vector>

#include "config.hpp"

namespace puppetry {

struct Runtime;

struct IgnoredApp {
    std::string match;   // "class" or "title"
    std::string value;   // lowercase
    bool when_open = false;
};

std::vector<IgnoredApp> parse_ignored_apps(const json& overlay);
// Case-insensitive substring match against the window's class or title.
bool ignored_app_matches(const IgnoredApp& rule, const std::string& window_class, const std::string& title);
// A regex matching `literal` anywhere, case-insensitively, without relying on
// the matcher's flags: "Key.X" -> ".*[kK][eE][yY]\.[xX].*".
std::string icase_substring_regex(const std::string& literal);

// Manual toggle: mode 1 = block, 0 = unblock, -1 = toggle. Returns the new state.
bool privacy_set_manual(Runtime& rt, int mode);
void privacy_load_manual(Runtime& rt);
// Pushes (manual || app) to the event stream and rewrites privacy.json.
void privacy_apply(Runtime& rt);
// Thread body: watches the ignored apps forever.
void run_privacy_watch(Runtime& rt);

} // namespace puppetry
