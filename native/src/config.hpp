#pragma once
// Config I/O -- mirrors macro_daemon.py's CONFIG PATHS / state / macros /
// aliases / profiles section exactly, same file layout and defaults, so
// this daemon and the old Python one (and the GUI, whichever side of the
// port it's on) can read/write the same on-disk config interchangeably.
#include <filesystem>
#include <string>
#include <vector>
#include <optional>
#include "third_party/nlohmann/json.hpp"

namespace puppetry {

namespace fs = std::filesystem;
using json = nlohmann::json;

fs::path config_dir();
fs::path state_file();
fs::path macros_file();
fs::path aliases_file();
fs::path profiles_dir();
fs::path control_socket_path();
// $XDG_RUNTIME_DIR/puppetry (tmpfs, per user); config_dir() if unset.
fs::path runtime_dir();
fs::path event_socket_path();
fs::path overlay_config_file();

// Creates config dir + shared macros.json + 3 default empty profiles if
// nothing exists yet. Safe to call every startup -- never overwrites
// existing files. Mirrors ensure_config_exists().
void ensure_config_exists();

json load_state();
void save_state(const json& state);

json load_macros();       // {"macros": [...]}
void save_macros(const json& data);

// Custom blocks made in the editor's "Create a custom block" dialog:
// {"blocks": [{"name", "code", "python_on", ...editor-only fields}]}.
// The daemon registers each one like a macro (callable by sanitized name
// from any macro), never triggered by a combo.
fs::path custom_blocks_file();
json load_custom_blocks();
json load_overlay_config(); // overlay.json (written by the GUI; read by puppetry-overlay)

json load_aliases();      // {"aliases": {...}}
void save_aliases(const json& data);

json load_profile(const std::string& profile_id);
void save_profile(const std::string& profile_id, const json& profile);

// [(profile_id, display_name), ...] sorted by filename.
std::vector<std::pair<std::string, std::string>> list_profile_ids();

void delete_profile(const std::string& profile_id);

} // namespace puppetry
