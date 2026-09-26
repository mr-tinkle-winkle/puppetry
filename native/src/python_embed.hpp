#pragma once
// "python_on" macro backend -- genuine embedded CPython, so a macro
// written under the old pure-Python daemon keeps running completely
// unmodified. This is the compatibility path; native_vm.hpp is the fast
// path for macros with python_on=false.
//
// One process-wide interpreter (Py_Initialize() once, at daemon
// startup), not one per macro or per thread -- primitives already share
// daemon-global state (Runtime) by design, same as the old daemon's
// bare module globals, so per-macro isolation was never the point.
// Threads other than the one that called python_embed_init() acquire
// the GIL via PyGILState_Ensure()/Release() around each top-level
// run(), same pattern any embedder uses for calling into Python from a
// non-Python-created thread.
#include <memory>
#include <string>
#include "macro.hpp"
#include "third_party/nlohmann/json.hpp"

namespace puppetry {

// Call once, before compiling or running any python_on macro. Creates
// the interpreter and the `_puppetry_native` module exposing every
// primitive. `rt` must outlive every macro run (true for the daemon's
// single process-lifetime Runtime).
void python_embed_init(Runtime& rt);
void python_embed_shutdown();

// Compiles ONE macro's body into a PythonMacroBody. `macro_def` is the
// macro's JSON object from macros.json (code, simplified_names,
// ignore_keyboard/ignore_mouse_buttons/ignore_mouse_movement).
// `registry` is passed through so the trampoline for calling other
// macros by name can look them up (potentially not yet populated at
// compile time -- looked up lazily at CALL time, same as the original's
// _FINAL_FUNCS scheme). Throws MacroCompileError (bad arguments(...))
// or std::runtime_error (Python SyntaxError/other compile-time
// exception, with its message) on failure.
// `all_macro_names`: every OTHER macro's sanitized name, used to build
// cross-macro trampolines. Passed explicitly by the caller (which
// already has the full macros.json list in hand) rather than this
// function re-reading macros.json itself -- avoids a redundant re-read
// per macro and a theoretical race against a concurrent GUI save
// between daemon startup and finishing compiling every macro.
std::shared_ptr<CompiledMacro> compile_python_macro(const json& macro_def, MacroRegistry& registry,
                                                      const std::vector<std::string>& all_macro_names);

} // namespace puppetry
