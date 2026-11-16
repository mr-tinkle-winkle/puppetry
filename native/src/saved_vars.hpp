#pragma once
// Saved variables: values a macro keeps across runs, daemon restarts and
// reboots (saveVar / loadVar / clearVar). Each macro has its own set, keyed
// by the macro's id, so two macros can use the same name.
//
// File: <config dir>/saved_variables.json
//   {"<macro id>": {"<name>": <JSON value>, ...}, ...}
// The GUI's "Saved variables" panel edits the same file; the daemon notices
// (modification time) and re-reads it before the next loadVar. Values are
// JSON: numbers, strings, booleans, null, lists (and dicts from Python).
// Code that isn't running as a macro (a custom block called on its own) uses
// the "_global" set.
#include <string>

#include "third_party/nlohmann/json.hpp"

namespace puppetry {

using json = nlohmann::json;

// Throws std::invalid_argument for an empty name.
void saved_var_set(const std::string& owner, const std::string& name, const json& value);
// false (out untouched) if there's no such variable.
bool saved_var_get(const std::string& owner, const std::string& name, json& out);
// true if something was removed.
bool saved_var_clear(const std::string& owner, const std::string& name);

// The current macro's id, or "_global".
std::string saved_var_owner();

// Test hook: use this file instead of the config dir's.
void saved_vars_set_path_for_tests(const std::string& path);

} // namespace puppetry
