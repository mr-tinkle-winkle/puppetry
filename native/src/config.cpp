#include "config.hpp"
#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <pwd.h>
#include <unistd.h>

namespace puppetry {

static fs::path home_dir() {
    if (const char* h = std::getenv("HOME")) return fs::path(h);
    if (struct passwd* pw = getpwuid(getuid())) return fs::path(pw->pw_dir);
    return fs::path("/");
}

fs::path config_dir() { return home_dir() / ".config" / "macro-daemon"; }
fs::path state_file() { return config_dir() / "state.json"; }
fs::path macros_file() { return config_dir() / "macros.json"; }
fs::path aliases_file() { return config_dir() / "aliases.json"; }
fs::path profiles_dir() { return config_dir() / "profiles"; }
fs::path control_socket_path() { return config_dir() / "control.sock"; }

static json default_state() {
    return json{
        {"keyboard_path", nullptr},
        {"keyboard_name", nullptr},
        {"mouse_path", nullptr},
        {"mouse_name", nullptr},
        {"active_profile", "profile_1"},
        {"autosave", false},
        {"abort_key", "KEY_PAUSE"},
    };
}

static const char* kDefaultProfileNames[] = {"Profile 1", "Profile 2", "Profile 3"};

static void write_json(const fs::path& path, const json& data) {
    std::ofstream out(path);
    out << data.dump(2);
}

static json read_json(const fs::path& path) {
    std::ifstream in(path);
    json data;
    in >> data;
    return data;
}

void ensure_config_exists() {
    fs::create_directories(profiles_dir());

    if (!fs::exists(state_file())) write_json(state_file(), default_state());
    if (!fs::exists(macros_file())) write_json(macros_file(), json{{"macros", json::array()}});
    if (!fs::exists(aliases_file())) write_json(aliases_file(), json{{"aliases", json::object()}});

    int i = 1;
    for (const char* name : kDefaultProfileNames) {
        fs::path p = profiles_dir() / ("profile_" + std::to_string(i) + ".json");
        if (!fs::exists(p)) {
            write_json(p, json{{"name", name}, {"enabled", json::object()}});
        }
        ++i;
    }
}

json load_state() {
    if (!fs::exists(state_file())) return default_state();
    return read_json(state_file());
}

void save_state(const json& state) { write_json(state_file(), state); }

json load_macros() {
    if (!fs::exists(macros_file())) return json{{"macros", json::array()}};
    return read_json(macros_file());
}

void save_macros(const json& data) { write_json(macros_file(), data); }

fs::path custom_blocks_file() { return config_dir() / "custom_blocks.json"; }

json load_custom_blocks() {
    if (!fs::exists(custom_blocks_file())) return json{{"blocks", json::array()}};
    try {
        return read_json(custom_blocks_file());
    } catch (const std::exception&) {
        return json{{"blocks", json::array()}};
    }
}

json load_aliases() {
    if (!fs::exists(aliases_file())) return json{{"aliases", json::object()}};
    return read_json(aliases_file());
}

void save_aliases(const json& data) { write_json(aliases_file(), data); }

json load_profile(const std::string& profile_id) {
    return read_json(profiles_dir() / (profile_id + ".json"));
}

void save_profile(const std::string& profile_id, const json& profile) {
    write_json(profiles_dir() / (profile_id + ".json"), profile);
}

std::vector<std::pair<std::string, std::string>> list_profile_ids() {
    std::vector<std::pair<std::string, std::string>> result;
    if (!fs::exists(profiles_dir())) return result;
    std::vector<fs::path> paths;
    for (const auto& entry : fs::directory_iterator(profiles_dir())) {
        auto name = entry.path().filename().string();
        if (name.rfind("profile_", 0) == 0 && entry.path().extension() == ".json") {
            paths.push_back(entry.path());
        }
    }
    std::sort(paths.begin(), paths.end());
    for (const auto& path : paths) {
        std::string id = path.stem().string();
        std::string display = id;
        try {
            json data = read_json(path);
            if (data.contains("name") && data["name"].is_string()) {
                display = data["name"].get<std::string>();
            }
        } catch (...) {
            // Unreadable/corrupt profile file -- fall back to the id
            // itself as the display name, same as the Python version's
            // bare `except Exception` in list_profile_ids().
        }
        result.emplace_back(id, display);
    }
    return result;
}

void delete_profile(const std::string& profile_id) {
    fs::path path = profiles_dir() / (profile_id + ".json");
    if (fs::exists(path)) fs::remove(path);
}

} // namespace puppetry
