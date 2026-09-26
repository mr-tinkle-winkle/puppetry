// Timing correctness for the new wait(): tiny waits must not hit the
// kernel sleep floor, longer waits must land on time (not early, not
// late by the wakeup latency), and consecutive waits must not drift.
// Bounds are deliberately loose -- this runs on shared CI/VM hardware --
// but each would catch the regression it's named for.
#include <algorithm>
#include <chrono>
#include <vector>
#include <cstdio>
#include <cstdlib>
#include <sys/prctl.h>
#include "primitives.hpp"

using namespace puppetry;
using clk = std::chrono::steady_clock;

static int checks = 0;
#define CHECK(cond, ...) do { ++checks; if (!(cond)) { std::fprintf(stderr, "FAILED: %s -- ", #cond); \
    std::fprintf(stderr, __VA_ARGS__); std::fprintf(stderr, "\n"); std::exit(1); } } while (0)

static double secs_since(clk::time_point t0) { return std::chrono::duration<double>(clk::now() - t0).count(); }

int main() {
    prctl(PR_SET_TIMERSLACK, 1UL, 0, 0, 0); // same as the daemon's main()
    Runtime rt;

    // 1. 100k tiny waits: the old code paid ~30-75us of kernel sleep EACH.
    auto t0 = clk::now();
    for (int i = 0; i < 100000; ++i) wait_fn(rt, 1e-9);
    double per_us = secs_since(t0) / 100000 * 1e6;
    CHECK(per_us < 2.0, "tiny wait costs %.2fus (sleep floor is back?)", per_us);

    // 2/3. Never early, ever; typically on time. "Typically" = the
    // median of 20 runs, because a shared VM can deschedule the thread
    // for milliseconds at a time, which no wait strategy can absorb --
    // that's host noise, not what this test is about.
    auto median_of = [&](double secs, bool precise) {
        std::vector<double> v;
        for (int i = 0; i < 20; ++i) {
            Runtime::wait_anchor().reset();
            auto s0 = clk::now();
            wait_fn(rt, secs, precise);
            double got = secs_since(s0);
            CHECK(got >= secs, "wait(%.4f) returned EARLY after %.6fs", secs, got);
            v.push_back(got);
        }
        std::sort(v.begin(), v.end());
        return v[v.size() / 2];
    };
    double med = median_of(0.005, false);
    CHECK(med < 0.0055, "wait(0.005) median %.6fs", med);
    double med_precise = median_of(0.003, true);
    CHECK(med_precise < 0.00305, "wait(0.003, precise=True) median %.6fs", med_precise);

    // 4. No drift: 200 x (work + wait(1ms)) should take ~200ms total --
    //    the per-iteration work is absorbed by the timeline anchor
    //    instead of accumulating (without anchoring it'd be 200ms +
    //    200 x overhead).
    Runtime::wait_anchor().reset();
    t0 = clk::now();
    for (int i = 0; i < 200; ++i) {
        auto busy_until = clk::now() + std::chrono::microseconds(100); // simulated per-line cost
        while (clk::now() < busy_until) {}
        wait_fn(rt, 0.001);
    }
    double total = secs_since(t0);
    CHECK(total >= 0.200 && total < 0.205, "200 x wait(1ms) with 100us work each took %.4fs (drift)", total);

    // 5. speed() scales it.
    Runtime::wait_anchor().reset();
    Runtime::speed_multiplier() = 0.5;
    t0 = clk::now();
    wait_fn(rt, 0.004);
    double scaled = secs_since(t0);
    Runtime::speed_multiplier() = 1.0;
    CHECK(scaled >= 0.002 && scaled < 0.003, "wait(0.004) at speed(0.5) took %.6fs", scaled);

    // 6. Abort interrupts a long wait promptly.
    Runtime::wait_anchor().reset();
    rt.abort_flag = true;
    t0 = clk::now();
    bool aborted = false;
    try { wait_fn(rt, 5.0); } catch (const MacroAborted&) { aborted = true; }
    CHECK(aborted && secs_since(t0) < 0.05, "abort didn't interrupt the wait");

    std::printf("All %d checks passed. (tiny wait %.3fus; wait(5ms) median +%.1fus; precise wait(3ms) median +%.1fus; 200x1ms %.4fs)\n",
                checks, per_us, (med - 0.005) * 1e6, (med_precise - 0.003) * 1e6, total);
    return 0;
}
