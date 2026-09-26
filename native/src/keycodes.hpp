#pragma once
// Thin wrapper so the rest of the codebase includes "keycodes.hpp"
// rather than the generated file directly -- keeps the generated
// file's name/location a build detail.
//
// HOT PATH NOTE: is_mouse_button()/is_button_code() are called on every
// synthetic key event. They used to build a std::string name per call
// (a heap allocation + hash lookup per kd()/ku()); they're now a
// single array index into tables built once.
#include <array>
#include <linux/input-event-codes.h>
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

namespace detail {
struct CodeTables {
    std::array<bool, KEY_CNT> is_key{};     // has a KEY_* name
    std::array<bool, KEY_CNT> is_button{};  // has a BTN_* name
    std::array<bool, KEY_CNT> is_mouse{};   // one of the 8 buttons our virtual MOUSE declares
    CodeTables() {
        for (const auto& [name, code] : key_name_to_code()) {
            if (code < 0 || code >= KEY_CNT) continue;
            if (name.rfind("KEY_", 0) == 0) is_key[code] = true;
            if (name.rfind("BTN_", 0) == 0) is_button[code] = true;
        }
        for (int c : {BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA, BTN_FORWARD, BTN_BACK, BTN_TASK})
            is_mouse[c] = true;
    }
};
inline const CodeTables& code_tables() {
    static const CodeTables t;
    return t;
}
} // namespace detail

inline bool is_key_code(int code) {
    return code >= 0 && code < KEY_CNT && detail::code_tables().is_key[code];
}

inline bool is_button_code(int code) {
    return code >= 0 && code < KEY_CNT && detail::code_tables().is_button[code];
}

// Which virtual device a synthetic event goes out on: exactly the old
// Python daemon's _device_for_code() rule (the 8 mouse buttons -> the
// virtual mouse, everything else -> the virtual keyboard).
inline bool is_mouse_button(int code) {
    return code >= 0 && code < KEY_CNT && detail::code_tables().is_mouse[code];
}

} // namespace puppetry
