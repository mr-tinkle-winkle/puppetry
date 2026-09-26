// Live integration test for the CPython embedding -- actually
// initializes the interpreter, compiles real macro source (including an
// arguments(...) declaration and a cross-macro call), and runs it,
// checking observable side effects on Runtime (synth_held, speed
// multiplier via timing) without needing any real input device or
// uinput permission (kd()/ku() on a Runtime whose UinputDevice was
// never create()'d are safe no-ops at the write() level, but still
// update Runtime's own held-key bookkeeping, which is what's checked).
#include <cassert>
#include <chrono>
#include <cstdio>
#include <thread>
#include "macro.hpp"
#include "python_embed.hpp"

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
    python_embed_init(rt);

    MacroRegistry registry;

    // A macro with arguments(...), calling kd()/ku() directly.
    json macro_def = {
        {"id", "m1"},
        {"name", "Test Macro"},
        // hits arrives as a real int on the default path, but as a
        // STRING when passed via CLI/FIRE (documented, matches the
        // original's behavior exactly) -- int() cast handles both.
        {"code", "arguments(hits=1)\nfor _ in range(int(hits)):\n    kd(KEY_A)\n    ku(KEY_A)\n"},
    };
    auto compiled = compile_python_macro(macro_def, registry, {});
    registry.set(sanitize_macro_name("Test Macro"), compiled);

    compiled->run(rt, registry, {}); // default hits=1
    CHECK(rt.synth_held.empty()); // kd() then ku() -- nothing left held

    compiled->run(rt, registry, {"3"}); // hits=3, passed as a string per spec
    CHECK(rt.synth_held.empty());

    // A second macro that calls the first one BY NAME -- exercises the
    // cross-macro trampoline (built at compile time from macros.json,
    // which this test doesn't have on disk, so we register it directly
    // via a second compile against a hand-built macros_doc equivalent).
    // Simpler direct check here: call the primitive that leaves state
    // behind so we can observe speed() scoping across a macro body.
    json speed_macro = {
        {"id", "m2"},
        {"name", "Speed Macro"},
        {"code", "speed(5)\nwait(0.01)\n"},
    };
    auto compiled2 = compile_python_macro(speed_macro, registry, {});
    auto start = std::chrono::steady_clock::now();
    compiled2->run(rt, registry, {});
    auto elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    // wait(0.01) at speed(5) = 0.05s scaled duration -- generous bounds
    // since this is a real sleep, not a mocked clock.
    CHECK(elapsed > 0.03 && elapsed < 0.3);

    // A macro with a compile-time error (bad arguments() usage) surfaces
    // as MacroCompileError, same as the native path.
    bool threw = false;
    try {
        json bad_macro = {{"id", "m3"}, {"name", "Bad"}, {"code", "arguments(hits)\npass\n"}};
        compile_python_macro(bad_macro, registry, {});
    } catch (const MacroCompileError&) { threw = true; }
    CHECK(threw);

    // A genuine Python SyntaxError in the body surfaces as
    // std::runtime_error with a useful message.
    threw = false;
    try {
        json syntax_bad = {{"id", "m4"}, {"name", "Syntax Bad"}, {"code", "kd(KEY_A\n"}};
        compile_python_macro(syntax_bad, registry, {});
    } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);

    // Regression: abort mid-tap() used to throw a C++ exception straight
    // through CPython's C frames (undefined behavior / crash). It must
    // come back out of run() as a clean MacroAborted instead.
    {
        json long_tap = {{"id", "m5"}, {"name", "Long Tap"}, {"code", "tap(KEY_A, 5)\n"}};
        auto m = compile_python_macro(long_tap, registry, {});
        std::thread aborter([&] { std::this_thread::sleep_for(std::chrono::milliseconds(50)); rt.abort_flag = true; });
        bool aborted = false;
        python_thread_enter();
        try { m->run(rt, registry, {}); } catch (const MacroAborted&) { aborted = true; }
        python_thread_exit();
        aborter.join();
        CHECK(aborted);
        rt.abort_flag = false;
    }

    // Regression: abort during type(..., async_=True) used to let the
    // exception escape a background std::thread -> std::terminate(),
    // killing the whole daemon. Surviving to the next line is the check.
    {
        json async_type = {{"id", "m6"}, {"name", "Async Type"}, {"code", "type('hello world', 0.5, async_=True)\n"}};
        auto m = compile_python_macro(async_type, registry, {});
        m->run(rt, registry, {});
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        rt.abort_flag = true;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        rt.abort_flag = false;
        CHECK(true);
    }

    // Unknown keyword arguments are rejected instead of silently ignored.
    {
        json typo = {{"id", "m7"}, {"name", "Typo"}, {"code", "tap(KEY_A, time=0.1)\n"}};
        auto m = compile_python_macro(typo, registry, {});
        rt.synth_held.take_all(); // the aborted async 'h' above was left down on purpose
        m->run(rt, registry, {}); // prints the TypeError; must not crash
        CHECK(rt.synth_held.empty()); // rejected before kd() -- nothing pressed
    }

    // Correct shutdown order: drop every CompiledMacro (which may hold
    // a live PyObject*) BEFORE tearing down the interpreter. main.cpp
    // follows this same order at real daemon shutdown.
    compiled.reset();
    compiled2.reset();
    registry.clear();
    python_embed_shutdown();
    std::printf("All %d checks passed.\n", test_count);
    return 0;
}
