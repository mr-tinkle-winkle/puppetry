#pragma once
// Thin wrapper so the rest of the codebase includes "keycodes.hpp"
// rather than the generated file directly -- keeps the generated
// file's name/location a build detail.
#include "keycodes_generated.hpp"

namespace puppetry {

inline bool resolve_key_name(const std::string& name, int& out_code) {
    const auto& table = key_name_to_code();
    auto it = table.find(name);
    if (it == table.end()) return false;
    out_code = it->second;
    return true;
}

inline std::string key_code_name(int code) {
    const auto& table = key_code_to_name();
    auto it = table.find(code);
    return it == table.end() ? ("code:" + std::to_string(code)) : it->second;
}

// True if this is a typing key (KEY_*) with an actual KEY_ name, as
// opposed to a button (BTN_*). Mirrors the Python daemon's
// _is_key_name()/_is_button_name() split, which is what decides which
// virtual uinput device (keyboard vs mouse) a given code is routed to.
inline bool is_key_code(int code) {
    auto name = key_code_name(code);
    return name.rfind("KEY_", 0) == 0;
}

inline bool is_button_code(int code) {
    auto name = key_code_name(code);
    return name.rfind("BTN_", 0) == 0;
}

} // namespace puppetry
