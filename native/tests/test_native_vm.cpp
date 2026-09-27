// Live tests for the python_off (native) fast path: primitives-only
// execution, arguments(...) with default vs. CLI-string override,
// cross-macro-by-name calls, rejection of anything outside the grammar
// (control flow, unknown names).
#include <cassert>
#include <cstdio>
#include "macro.hpp"
#include "native_vm.hpp"

using namespace puppetry;

static int test_count = 0;
#define CHECK(cond) do { \
    ++test_count; \
    if (!(cond)) { \
        std::fprintf(stderr, "FAILED: %s (line %d)\n", #cond, __LINE__); \
        std::exit(1); \
    } \
} while (0)

int main() {
    Runtime rt;
    MacroRegistry registry;

    // Basic primitives-only body, with a declared parameter used both
    // as a key name (default) and overridden by a CLI-style string arg.
    json macro_def = {
        {"id", "m1"}, {"name", "Native Macro"},
        {"code", "arguments(key=KEY_A, times=1)\nkd(key)\nku(key)\n"},
    };
    auto compiled = compile_native_macro(macro_def, registry);
    registry.set(sanitize_macro_name("Native Macro"), compiled);

    compiled->run(rt, registry, {}); // defaults: key=KEY_A
    CHECK(rt.synth_held.empty()); // kd then ku -- nothing left held

    // CLI-style override: key passed as the literal string "KEY_B".
    compiled->run(rt, registry, {"KEY_B"});
    CHECK(rt.synth_held.empty());

    // Cross-macro call by name.
    json caller_def = {
        {"id", "m2"}, {"name", "Caller Macro"},
        {"code", "Native_Macro()\n"},
    };
    auto caller = compile_native_macro(caller_def, registry);
    caller->run(rt, registry, {}); // should resolve "Native_Macro" via the registry and run it

    // combo() with a keyword time_ argument.
    json combo_def = {
        {"id", "m3"}, {"name", "Combo Macro"},
        {"code", "combo(KEY_LEFTCTRL, KEY_A, time_=0.01)\n"},
    };
    auto combo_macro = compile_native_macro(combo_def, registry);
    combo_macro->run(rt, registry, {});
    CHECK(rt.synth_held.empty());

    // checkpoint() -- a pure marker for the editor, no runtime effect at
    // all. Compiles, runs, touches nothing (no held keys, no crash), and
    // rejects an argument the way every other zero-arg call would.
    json checkpoint_def = {
        {"id", "m6"}, {"name", "Checkpoint Macro"},
        {"code", "kd(KEY_A)\ncheckpoint()\nku(KEY_A)\n"},
    };
    auto checkpoint_macro = compile_native_macro(checkpoint_def, registry);
    checkpoint_macro->run(rt, registry, {});
    CHECK(rt.synth_held.empty());

    bool threw = false;
    try {
        json bad3 = {{"id", "m7"}, {"name", "Bad3"}, {"code", "checkpoint(KEY_A)\n"}};
        compile_native_macro(bad3, registry);
    } catch (const MacroCompileError&) { threw = true; }
    CHECK(threw);

    // Rejects control flow outright, with a clear per-line message.
    threw = false;
    try {
        json bad = {{"id", "m4"}, {"name", "Bad"}, {"code", "if True:\n    kd(KEY_A)\n"}};
        compile_native_macro(bad, registry);
    } catch (const MacroCompileError&) { threw = true; }
    CHECK(threw);

    // Rejects an unresolvable bare name.
    threw = false;
    try {
        json bad2 = {{"id", "m5"}, {"name", "Bad2"}, {"code", "kd(not_a_real_key)\n"}};
        compile_native_macro(bad2, registry);
    } catch (const MacroCompileError&) { threw = true; }
    CHECK(threw);

    std::printf("All %d checks passed.\n", test_count);
    return 0;
}
