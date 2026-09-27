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
#include <linux/input-event-codes.h>
#include <string>
#include "macro.hpp"
#include "native_vm.hpp"
#include "python_embed.hpp"
#include "transcriber.hpp"

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

// The OTHER per-event hot path: transcription. Precise mode records every
// hardware frame of a 1000Hz mouse, so this runs ~2000 times a second
// while recording. Feeds the core synthetic frames (the same thing
// transcribe_main does with real ones) and reports how many it can turn
// into macro lines per second -- the interesting part being how far above
// the ~2k/s it actually needs to sustain this lands.
static void bench_transcriber(const char* label, bool precise, int frames) {
    TranscribeOptions opts;
    opts.keyboard = opts.mouse = opts.raw = true;
    opts.precise = precise;
    std::string sink;
    sink.reserve(1 << 20);
    long long written = 0;
    TranscriberCore core(opts, [&](const std::string& s) { written += (long long)s.size(); sink.clear(); },
                         [](int&, int&) { return false; });
    core.start(0);

    auto frame = [&](long long t, unsigned short type, unsigned short code, int value) {
        RawEvent e{};
        e.time_us = t; e.type = type; e.code = code; e.value = value;
        core.feed(Source::Both, e);
    };
    auto t0 = clk::now();
    long long t = 0;
    for (int i = 0; i < frames; ++i) {
        t += 1000; // a 1000Hz mouse: one frame per millisecond
        frame(t, EV_REL, REL_X, (i % 7) - 3);
        frame(t, EV_REL, REL_Y, (i % 5) - 2);
        frame(t, EV_SYN, SYN_REPORT, 0);
        if (i % 32 == 0) { // a keypress in amongst it
            frame(t, EV_KEY, KEY_A, 1);
            frame(t, EV_KEY, KEY_A, 0);
        }
    }
    double secs = std::chrono::duration<double>(clk::now() - t0).count();
    std::printf("%-12s %9.0f mouse frames/s (%6.2f us per frame, %lld bytes out)\n",
                label, frames / secs, secs / frames * 1e6, written);
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

    bench_transcriber("tr_60hz", false, iters);
    bench_transcriber("tr_precise", true, iters);
    return 0;
}
