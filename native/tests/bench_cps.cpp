// Daemon-side throughput benchmark for the autoclicker pattern
//     speed(<tiny>); tap(KEY_SPACE); wait(1)
// run back-to-back the way a hold/toggle loop runs it, in both the
// python_on and python_off backends. Output goes to /dev/null instead
// of /dev/uinput (so it works without the device), which still pays the
// real write() syscall per event -- this measures what the DAEMON can
// emit, not what a compositor/app will register (see the GUI's Input
// Visualizer tab for that).
//
// Not part of ctest (timing-dependent). Run: ./bench_cps
#include <chrono>
#include <cstdio>
#include "macro.hpp"
#include "native_vm.hpp"
#include "python_embed.hpp"

using namespace puppetry;
using clk = std::chrono::steady_clock;

static void bench(const char* label, CompiledMacro& m, Runtime& rt, MacroRegistry& reg, int iters) {
    for (int i = 0; i < 200; ++i) { Runtime::speed_multiplier() = 1.0; m.run(rt, reg, {}); } // warm up
    auto t0 = clk::now();
    for (int i = 0; i < iters; ++i) {
        Runtime::speed_multiplier() = 1.0; // what loop_until_stopped does per iteration
        m.run(rt, reg, {});
    }
    double secs = std::chrono::duration<double>(clk::now() - t0).count();
    std::printf("%-12s %9.0f taps/s  (%6.2f us per tap cycle)\n", label, iters / secs, secs / iters * 1e6);
}

int main(int argc, char** argv) {
    int iters = argc > 1 ? std::atoi(argv[1]) : 20000;
    Runtime rt;
    rt.ui_keyboard.open_sink("/dev/null");
    rt.ui_mouse.open_sink("/dev/null");
    python_embed_init(rt);
    MacroRegistry reg;

    const char* body = "speed(0.0000001)\ntap(KEY_SPACE)\nwait(1)\n";
    json def = {{"id", "b"}, {"name", "bench"}, {"code", body}};
    {
        auto native = compile_native_macro(def, reg);
        auto py = compile_python_macro(def, reg, {});
        bench("python_off", *native, rt, reg, iters);
        python_thread_enter();
        bench("python_on", *py, rt, reg, iters);
        python_thread_exit();
    }
    reg.clear();
    python_embed_shutdown();
    return 0;
}
