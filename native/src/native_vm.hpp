#pragma once
// "python_off" macro backend -- the fast native path, scoped per the
// project handoff's own fallback clause: PRIMITIVES-ONLY (a flat
// sequence of primitive calls, no if/while/variables-as-control-flow),
// not a general interpreter. Revisit if per-macro branching turns out
// to matter in practice; the harder general-interpreter version was
// explicitly not attempted first.
//
// Grammar (one statement per line, blank lines and lines starting with
// '#' ignored):
//   arguments(name1=default1, ...)      -- optional, first line only
//   primitive_name(arg1, arg2, kw=val)  -- exactly one call per line
//   other_macro_name(arg1, ...)         -- calling another macro by name
//
// Arguments accepted: integer/float literals, single- or double-quoted
// strings (no escapes beyond \" and \' -- deliberately minimal),
// True/False, and bare identifiers, which resolve against (in order)
// this macro's own declared parameters, KEY_*/BTN_* names, and (if
// simplified_names is set) the simplified-name table. No expressions,
// no operators, no nested calls.
#include <memory>
#include "macro.hpp"
#include "third_party/nlohmann/json.hpp"

namespace puppetry {

using json = nlohmann::json;

// Throws MacroCompileError for anything outside the grammar above (a
// clear, specific message naming the offending line), so an author who
// writes real control flow into a python_off macro gets told to either
// simplify it or switch the macro to python_on, rather than a cryptic
// failure at run time.
std::shared_ptr<CompiledMacro> compile_native_macro(const json& macro_def, MacroRegistry& registry);

} // namespace puppetry
