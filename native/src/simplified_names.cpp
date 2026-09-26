#include "simplified_names.hpp"
#include <cctype>
#include "keycodes.hpp"

namespace puppetry {

const std::unordered_map<std::string, std::string>& simplified_names_table() {
    static const std::unordered_map<std::string, std::string> table = [] {
        std::unordered_map<std::string, std::string> m;
        for (int i = 0; i < 26; ++i) {
            char letter = 'A' + i;
            std::string name = std::string("KEY_") + letter;
            m[std::string(1, letter)] = name;
            m[std::string(1, (char)std::tolower(letter))] = name;
        }
        for (int d = 0; d < 10; ++d) {
            std::string name = "KEY_" + std::to_string(d);
            m["D" + std::to_string(d)] = name;
            m["d" + std::to_string(d)] = name;
        }
        for (int i = 1; i <= 12; ++i) {
            m["F" + std::to_string(i)] = "KEY_F" + std::to_string(i);
        }
        m.insert({
            {"SPACE", "KEY_SPACE"}, {"ENTER", "KEY_ENTER"}, {"ESC", "KEY_ESC"}, {"TAB", "KEY_TAB"},
            {"BACKSPACE", "KEY_BACKSPACE"}, {"DEL", "KEY_DELETE"}, {"DELETE", "KEY_DELETE"},
            {"CAPSLOCK", "KEY_CAPSLOCK"},
            {"SHIFT", "KEY_LEFTSHIFT"}, {"RSHIFT", "KEY_RIGHTSHIFT"},
            {"CTRL", "KEY_LEFTCTRL"}, {"RCTRL", "KEY_RIGHTCTRL"},
            {"ALT", "KEY_LEFTALT"}, {"RALT", "KEY_RIGHTALT"},
            {"META", "KEY_LEFTMETA"}, {"WIN", "KEY_LEFTMETA"}, {"SUPER", "KEY_LEFTMETA"},
            {"RMETA", "KEY_RIGHTMETA"}, {"RWIN", "KEY_RIGHTMETA"},
            {"UP", "KEY_UP"}, {"DOWN", "KEY_DOWN"}, {"LEFT", "KEY_LEFT"}, {"RIGHT", "KEY_RIGHT"},
            {"HOME", "KEY_HOME"}, {"END", "KEY_END"}, {"PAGEUP", "KEY_PAGEUP"}, {"PAGEDOWN", "KEY_PAGEDOWN"},
            {"INSERT", "KEY_INSERT"},
            {"LMB", "BTN_LEFT"}, {"RMB", "BTN_RIGHT"}, {"MMB", "BTN_MIDDLE"},
            {"MB4", "BTN_SIDE"}, {"MB5", "BTN_EXTRA"},
        });
        // Case-insensitive: every name above also gets a lowercase alias.
        for (const auto& [k, v] : std::unordered_map<std::string, std::string>(m)) {
            std::string lower;
            for (char c : k) lower += (char)std::tolower((unsigned char)c);
            m[lower] = v;
        }
        return m;
    }();
    return table;
}

std::unordered_map<std::string, int> build_simplified_namespace() {
    std::unordered_map<std::string, int> ns;
    for (const auto& [simple_name, real_name] : simplified_names_table()) {
        int code;
        if (resolve_key_name(real_name, code)) ns[simple_name] = code;
    }

    try {
        json aliases_doc = load_aliases();
        if (aliases_doc.contains("aliases") && aliases_doc["aliases"].is_object()) {
            for (auto& [custom_name, target] : aliases_doc["aliases"].items()) {
                if (!target.is_string()) continue;
                std::string target_str = target.get<std::string>();
                const auto& table = simplified_names_table();
                auto it = table.find(target_str);
                std::string real_name = (it != table.end()) ? it->second : target_str;
                int code;
                if (resolve_key_name(real_name, code)) ns[custom_name] = code;
            }
        }
    } catch (...) {
        // Missing/corrupt aliases.json is fine -- just no custom aliases.
    }

    return ns;
}

} // namespace puppetry
