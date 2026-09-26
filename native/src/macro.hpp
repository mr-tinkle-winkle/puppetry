#pragma once
// Macro definitions, compilation dispatch, and repeat-mode runtime state
// -- mirrors macro_daemon.py's COMPILING MACRO BODY TEXT / RUNTIME
// MACRO STATE sections.
//
// A macro's body runs through one of two backends depending on its
// "python_on" flag (macros.json), decided once at compile time:
//   - python_on == true  -> PythonMacroBody (python_embed.cpp): genuine
//     embedded CPython, full compatibility with every macro written
//     under the old pure-Python daemon.
//   - python_on == false -> NativeMacroBody (native_vm.cpp): the fast
//     native path. Scope note (settled per the project handoff): this
//     ships as PRIMITIVES-ONLY (a flat sequence of primitive calls, no
//     if/while/variables) rather than a general control-flow
//     interpreter -- the handoff's own fallback clause explicitly
//     allows this trade-off once the fuller version's cost became
//     clear during this build. Revisit if per-macro branching turns out
//     to matter in practice.
#include <atomic>
#include <memory>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>
#include "runtime.hpp"
#include "third_party/nlohmann/json.hpp"

namespace puppetry {

using json = nlohmann::json;

class MacroCompileError : public std::runtime_error {
public:
    explicit MacroCompileError(const std::string& msg) : std::runtime_error(msg) {}
};

// Turns a macro's display name into a valid identifier for cross-macro
// calls, e.g. "Flick and Click" -> "Flick_and_Click". Mirrors
// sanitize_macro_name() exactly.
std::string sanitize_macro_name(const std::string& name);

class MacroRegistry; // fwd decl, see below

// A compiled macro body, whichever backend produced it. `args` are
// always strings -- CLI/FIRE-supplied macro arguments arrive as
// strings, same as the Python version's documented behavior (cast with
// int()/float() inside the macro if a numeric default is declared).
class CompiledMacro {
public:
    virtual ~CompiledMacro() = default;
    virtual void run(Runtime& rt, MacroRegistry& registry, const std::vector<std::string>& args) = 0;
};

// Every OTHER macro, looked up by sanitized name AT CALL TIME (not
// compile time) -- mirrors the Python version's trampoline/_FINAL_FUNCS
// scheme, which is what lets macro A call macro B call macro C to
// unlimited nesting depth regardless of compile order.
class MacroRegistry {
public:
    void set(const std::string& sanitized_name, std::shared_ptr<CompiledMacro> macro) {
        table_[sanitized_name] = std::move(macro);
    }
    // Throws std::runtime_error (surfaces as a Python NameError on the
    // python_on path, or a plain failure on the native path) if the
    // name never successfully compiled -- same "see the startup log"
    // message as the original.
    // Drops every registered macro. Callers must do this BEFORE
    // python_embed_shutdown() if any registered macro is a
    // PythonMacroBody -- see its destructor's comment for why holding
    // a live PyObject* past interpreter teardown is unsafe.
    void clear() { table_.clear(); }

    std::shared_ptr<CompiledMacro> get(const std::string& sanitized_name) const {
        auto it = table_.find(sanitized_name);
        if (it == table_.end()) {
            throw std::runtime_error("macro '" + sanitized_name +
                                      "' failed to compile -- see the startup log");
        }
        return it->second;
    }

private:
    std::unordered_map<std::string, std::shared_ptr<CompiledMacro>> table_;
};

// arguments(name1=default1, ...) declaration -- first line only. Mirrors
// _extract_arguments_signature(): every declared parameter needs a
// default (a macro can always be triggered with zero args), and the
// call is only valid as the literal first non-blank line.
struct ArgumentDecl {
    std::string name;
    std::string default_source; // raw source text of the default expression
};

struct ExtractedBody {
    std::string body;                     // with the arguments(...) line removed, if present
    std::vector<ArgumentDecl> params;      // empty if no arguments(...) declaration
    bool has_arguments_decl = false;
};

// Throws MacroCompileError for a misused arguments(...) (missing
// default, appears somewhere other than line one, uses *args/**kwargs --
// on the native path, which has no such Python-only concepts, only the
// "first line only" and "every param needs a default" rules apply).
ExtractedBody extract_arguments_signature(const std::string& body);

enum class RepeatMode { None, Hold, Toggle };
enum class TriggerEdge { Down, Up };

RepeatMode parse_repeat_mode(const std::string& s);
TriggerEdge parse_trigger_edge(const std::string& s);

struct RunningMacroState {
    std::thread thread;
    std::atomic<bool> stop_flag{false};
    std::atomic<bool> active_hold{false};
    std::atomic<bool> armed_up{false};

    ~RunningMacroState() {
        if (thread.joinable()) thread.detach();
    }
};

class Macro {
public:
    std::string id;
    std::string name;
    bool enabled = false;
    RepeatMode repeat_mode = RepeatMode::None;
    TriggerEdge trigger_edge = TriggerEdge::Down;
    std::vector<int> combo; // resolved key/button codes
    std::shared_ptr<CompiledMacro> func;
    std::shared_ptr<RunningMacroState> runtime = std::make_shared<RunningMacroState>();

    bool combo_is_subset_of(const std::vector<int>& held_set) const;
};

// Dispatches a completed trigger according to repeat_mode -- mirrors
// _trigger_macro()/_fire_once()/_loop_until_stopped()/_start_loop()/
// _stop_loop()/_is_looping() as one unit so they can't drift apart.
void trigger_macro(Runtime& rt, MacroRegistry& registry, Macro& macro,
                    const std::vector<std::string>& args = {});
bool macro_is_looping(const Macro& macro);
void stop_macro_loop(Macro& macro);

} // namespace puppetry
