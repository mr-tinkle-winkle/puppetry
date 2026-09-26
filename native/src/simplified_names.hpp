#pragma once
// Simplified names (opt-in per macro via "simplified_names": true) --
// mirrors macro_daemon.py's SIMPLIFIED_NAMES table and
// _build_simplified_namespace() exactly, including the LMB/RMB/MMB
// mouse-button compromise (arrow keys keep the plain "left"/"right").
#include <string>
#include <unordered_map>
#include "config.hpp"

namespace puppetry {

// name -> real KEY_*/BTN_* name (not yet resolved to a code).
const std::unordered_map<std::string, std::string>& simplified_names_table();

// Built-in simplified names resolved to codes, with custom aliases.json
// entries layered on top (a custom alias's target can be either a
// built-in simplified name or a raw KEY_*/BTN_* name) -- mirrors
// _build_simplified_namespace().
std::unordered_map<std::string, int> build_simplified_namespace();

} // namespace puppetry
