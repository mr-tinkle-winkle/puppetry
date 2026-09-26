// TranscriberCore with synthetic kernel events -- no devices needed.
#include <cstdio>
#include <cstdlib>
#include <linux/input-event-codes.h>
#include <string>
#include "transcriber.hpp"

using namespace puppetry;

static int checks = 0;
#define CHECK_EQ(got_expr, want_expr) do { ++checks; std::string got_ = (got_expr), want_ = (want_expr); \
    if (got_ != want_) { \
    std::fprintf(stderr, "FAILED line %d:\n--- got ---\n%s\n--- want ---\n%s\n", __LINE__, got_.c_str(), want_.c_str()); \
    std::exit(1); } } while (0)

static RawEvent ev(long long t, int type, int code, int value) {
    RawEvent e; e.type = (unsigned short)type; e.code = (unsigned short)code; e.value = value; e.time_us = t; return e;
}
static RawEvent syn(long long t) { return ev(t, EV_SYN, SYN_REPORT, 0); }

struct Harness {
    std::string out;
    TranscriberCore core;
    Harness(TranscribeOptions o, bool query_ok = true, int qx = 100, int qy = 200)
        : core(o, [this](const std::string& s) { out += s; },
               [=](int& x, int& y) { x = qx; y = qy; return query_ok; }) {}
    std::string take() { std::string s = out; out.clear(); return s; }
};

int main() {
    const long long T0 = 5000000000LL; // arbitrary monotonic origin

    // Keyboard: microsecond gaps, nothing dropped (the old code deleted
    // any gap under 20ms -- this 234us one included).
    {
        TranscribeOptions o; o.keyboard = true;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_A, 1));
        h.core.feed(Source::Keyboard, syn(T0 + 1000));
        h.core.feed(Source::Keyboard, ev(T0 + 1234, EV_KEY, KEY_A, 0));
        h.core.feed(Source::Keyboard, ev(T0 + 1300, EV_KEY, KEY_A, 2)); // autorepeat: ignored
        CHECK_EQ(h.take(), "wait(0.001000)\nkd(KEY_A)\nwait(0.000234)\nku(KEY_A)\n");
        // Same-microsecond events get no wait line at all.
        h.core.feed(Source::Keyboard, ev(T0 + 1234, EV_KEY, KEY_B, 1));
        CHECK_EQ(h.take(), "kd(KEY_B)\n");
    }

    // Ping (non-raw mouse): the gap is the move's duration; ping key itself never transcribed.
    {
        TranscribeOptions o; o.keyboard = true; o.mouse = true;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 500000, EV_KEY, KEY_INSERT, 1));
        h.core.feed(Source::Keyboard, ev(T0 + 600000, EV_KEY, KEY_INSERT, 0));
        CHECK_EQ(h.take(), "move_mouse(100, 200, move_to=True, time_=0.500000)\n");
        Harness fail(o, false);
        fail.core.start(T0);
        fail.core.feed(Source::Keyboard, ev(T0 + 1, EV_KEY, KEY_INSERT, 1));
        CHECK_EQ(fail.take(), "# ping failed -- couldn't read cursor position (needs KDE/kdotool)\n");
    }

    // Raw + precise: one line per hardware frame, precise waits, X+Y together.
    {
        TranscribeOptions o; o.mouse = true; o.raw = true; o.precise = true;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Mouse, ev(T0 + 1000, EV_REL, REL_X, 3));
        h.core.feed(Source::Mouse, ev(T0 + 1000, EV_REL, REL_Y, -2));
        h.core.feed(Source::Mouse, syn(T0 + 1000));
        h.core.feed(Source::Mouse, ev(T0 + 2000, EV_REL, REL_X, 1));
        h.core.feed(Source::Mouse, syn(T0 + 2000));
        CHECK_EQ(h.take(),
                 "wait(0.001000, precise=True)\nmove_mouse(3, -2, time_=0, easing=\"none\")\n"
                 "wait(0.001000, precise=True)\nmove_mouse(1, 0, time_=0, easing=\"none\")\n");
    }

    // Raw, default 60Hz: frames inside one tick merge into one line.
    {
        TranscribeOptions o; o.mouse = true; o.raw = true; o.raw_hz = 60;
        Harness h(o);
        h.core.start(T0);
        for (int i = 1; i <= 20; ++i) { // 1 frame/ms for 20ms, +1px each
            h.core.feed(Source::Mouse, ev(T0 + i * 1000, EV_REL, REL_X, 1));
            h.core.feed(Source::Mouse, syn(T0 + i * 1000));
        }
        // tick = 16666us -> first emission at the 17ms frame with 17px
        CHECK_EQ(h.take(), "wait(0.017000)\nmove_mouse(17, 0, time_=0, easing=\"none\")\n");
        // A click flushes the 3 pending px FIRST, so it lands where the cursor really was.
        h.core.feed(Source::Mouse, ev(T0 + 21000, EV_KEY, BTN_LEFT, 1));
        CHECK_EQ(h.take(), "wait(0.003000)\nmove_mouse(3, 0, time_=0, easing=\"none\")\nwait(0.001000)\nkd(BTN_LEFT)\n"); // motion keeps its own 20ms timestamp
        h.core.feed(Source::Mouse, ev(T0 + 30000, EV_REL, REL_WHEEL, -1));
        CHECK_EQ(h.take(), "wait(0.009000)\nwheel(-1)\n");
    }

    // Kernel overflow: noted, and the broken frame discarded.
    {
        TranscribeOptions o; o.mouse = true; o.raw = true; o.precise = true;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Mouse, ev(T0 + 1000, EV_SYN, SYN_DROPPED, 0));
        h.core.feed(Source::Mouse, ev(T0 + 1000, EV_REL, REL_X, 50)); // unreliable, discarded
        h.core.feed(Source::Mouse, syn(T0 + 1000));
        h.core.feed(Source::Mouse, ev(T0 + 2000, EV_REL, REL_X, 2));
        h.core.feed(Source::Mouse, syn(T0 + 2000));
        CHECK_EQ(h.take(),
                 "# kernel dropped input events here (buffer overflow) -- timing/motion around this point may be off\n"
                 "wait(0.002000, precise=True)\nmove_mouse(2, 0, time_=0, easing=\"none\")\n");
    }

    // set_positions: absolute coordinates, and resync re-bases correctly.
    {
        TranscribeOptions o; o.mouse = true; o.raw = true; o.precise = true; o.set_positions = true;
        Harness h(o, true, 500, 300);
        h.core.start(T0);
        h.core.feed(Source::Mouse, ev(T0 + 1000, EV_REL, REL_X, 10));
        h.core.feed(Source::Mouse, syn(T0 + 1000));
        CHECK_EQ(h.take(), "wait(0.001000, precise=True)\nmove_mouse(510, 300, move_to=True, time_=0)\n");
        long long sx, sy;
        h.core.mark_resync_started(T0 + 2000, sx, sy);
        h.core.feed(Source::Mouse, ev(T0 + 3000, EV_REL, REL_Y, 5)); // moves while the query runs
        h.core.feed(Source::Mouse, syn(T0 + 3000));
        h.out.clear();
        h.core.apply_resync(512, 301, sx, sy); // real position (at query start) was 512,301
        h.core.feed(Source::Mouse, ev(T0 + 4000, EV_REL, REL_X, 1));
        h.core.feed(Source::Mouse, syn(T0 + 4000));
        CHECK_EQ(h.take(), "wait(0.001000, precise=True)\nmove_mouse(513, 306, move_to=True, time_=0)\n");
    }

    // same_start emits the one absolute line up front.
    {
        TranscribeOptions o; o.mouse = true; o.raw = true; o.same_start = true;
        Harness h(o, true, 7, 8);
        h.core.start(T0);
        CHECK_EQ(h.take(), "move_mouse(7, 8, move_to=True, time_=0)\n");
    }

    std::printf("All %d checks passed.\n", checks);
    return 0;
}
