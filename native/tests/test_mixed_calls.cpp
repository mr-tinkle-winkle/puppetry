// A python_on macro calling a python_off macro by name, and vice versa
// -- exercises the dynamic_cast branch in python_embed.cpp's
// trampoline_call() that decides whether a cross-macro call can stay
// inside Python (full fidelity) or has to cross into the native VM
// (positional strings only).
#include <cstdio>
#include "macro.hpp"
#include "native_vm.hpp"
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

    // Native (python_off) macro, registered first.
    json native_def = {{"id", "n1"}, {"name", "Native Helper"}, {"code", "kd(KEY_A)\nku(KEY_A)\n"}};
    auto native_macro = compile_native_macro(native_def, registry);
    registry.set(sanitize_macro_name("Native Helper"), native_macro);

    // Python macro that calls the native one by name.
    json py_def = {{"id", "p1"}, {"name", "Python Caller"}, {"code", "Native_Helper()\n"}};
    auto py_macro = compile_python_macro(py_def, registry, {"Native_Helper"});
    registry.set(sanitize_macro_name("Python Caller"), py_macro);

    py_macro->run(rt, registry, {});
    CHECK(rt.synth_held.empty()); // the native macro's kd()/ku() ran and left nothing held

    // Python macro, registered so a native macro can call it back.
    json py_def2 = {{"id", "p2"}, {"name", "Python Helper"}, {"code", "kd(KEY_B)\nku(KEY_B)\n"}};
    auto py_macro2 = compile_python_macro(py_def2, registry, {});
    registry.set(sanitize_macro_name("Python Helper"), py_macro2);

    json native_def2 = {{"id", "n2"}, {"name", "Native Caller"}, {"code", "Python_Helper()\n"}};
    auto native_macro2 = compile_native_macro(native_def2, registry);
    native_macro2->run(rt, registry, {});
    CHECK(rt.synth_held.empty());

    native_macro.reset();
    py_macro.reset();
    py_macro2.reset();
    native_macro2.reset();
    registry.clear();
    python_embed_shutdown();

    std::printf("All %d checks passed.\n", test_count);
    return 0;
}
