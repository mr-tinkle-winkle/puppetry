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

    // Abort key: consumed silently, flags the session to end; a normal
    // key around it still transcribes fine.
    {
        TranscribeOptions o; o.keyboard = true; o.abort_code = KEY_PAUSE;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_A, 1));
        if (h.core.abort_requested()) { std::fprintf(stderr, "FAILED: abort flagged too early\n"); std::exit(1); }
        h.core.feed(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_PAUSE, 1));
        CHECK_EQ(h.take(), "wait(0.001000)\nkd(KEY_A)\n"); // KEY_PAUSE itself never emitted
        if (!h.core.abort_requested()) { std::fprintf(stderr, "FAILED: abort not flagged\n"); std::exit(1); }
        h.core.feed(Source::Keyboard, ev(T0 + 3000, EV_KEY, KEY_PAUSE, 0)); // release: still nothing emitted
        CHECK_EQ(h.take(), "");
    }

    // Toggle hotkey: filtered from the transcript, but never ends the session.
    {
        TranscribeOptions o; o.keyboard = true; o.hotkey_code = KEY_F9;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_F9, 1));
        h.core.feed(Source::Keyboard, ev(T0 + 1500, EV_KEY, KEY_F9, 0));
        CHECK_EQ(h.take(), "");
        if (h.core.abort_requested()) { std::fprintf(stderr, "FAILED: hotkey must not flag abort\n"); std::exit(1); }
        h.core.feed(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_A, 1));
        CHECK_EQ(h.take(), "wait(0.002000)\nkd(KEY_A)\n"); // filtered keys never move last_us_ forward
    }

    // Restart hotkey: filtered from the transcript exactly like the toggle
    // hotkey -- the GUI does the actual stop/start, this side just must
    // never let the keypress itself leak into either recording.
    {
        TranscribeOptions o; o.keyboard = true; o.restart_code = KEY_F10;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_F10, 1));
        h.core.feed(Source::Keyboard, ev(T0 + 1500, EV_KEY, KEY_F10, 0));
        CHECK_EQ(h.take(), "");
        if (h.core.abort_requested()) { std::fprintf(stderr, "FAILED: restart key must not flag abort\n"); std::exit(1); }
        h.core.feed(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_A, 1));
        CHECK_EQ(h.take(), "wait(0.002000)\nkd(KEY_A)\n");
    }

    // Checkpoint key: UNLIKE the other special keys, it DOES produce a
    // line -- a literal checkpoint() call -- but only on press, never on
    // release or autorepeat, and it never itself shows up as a keypress.
    {
        TranscribeOptions o; o.keyboard = true; o.checkpoint_code = KEY_F11;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_A, 1));
        h.core.feed(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_F11, 1));
        CHECK_EQ(h.take(), "wait(0.001000)\nkd(KEY_A)\nwait(0.001000)\ncheckpoint()\n");
        h.core.feed(Source::Keyboard, ev(T0 + 2100, EV_KEY, KEY_F11, 2)); // autorepeat: ignored
        h.core.feed(Source::Keyboard, ev(T0 + 2200, EV_KEY, KEY_F11, 0)); // release: ignored
        CHECK_EQ(h.take(), "");
        // A checkpoint can be followed by mouse motion accumulated before
        // it -- flush_motion() is called first, same guarantee a normal
        // keypress gets (a click never overtakes the motion that preceded
        // it, and neither does a checkpoint).
        TranscribeOptions o2; o2.keyboard = true; o2.mouse = true; o2.raw = true; o2.checkpoint_code = KEY_F11;
        Harness h2(o2);
        h2.core.start(T0);
        h2.core.feed(Source::Mouse, ev(T0 + 500, EV_REL, REL_X, 5));
        h2.core.feed(Source::Mouse, syn(T0 + 500));
        h2.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_F11, 1));
        CHECK_EQ(h2.take(), "wait(0.000500)\nmove_mouse(5, 0, time_=0, easing=\"none\")\n"
                             "wait(0.000500)\ncheckpoint()\n");
    }

    // Alt+Tab filtering: Tab while Alt is held drops both keys entirely;
    // a plain Alt press (no Tab) still transcribes, just emitted once
    // Alt comes back up instead of immediately.
    {
        TranscribeOptions o; o.keyboard = true; o.ignore_alt_tab = true;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_LEFTALT, 1));
        CHECK_EQ(h.take(), ""); // buffered, not yet known if this is Alt+Tab
        h.core.feed(Source::Keyboard, ev(T0 + 1500, EV_KEY, KEY_TAB, 1));
        h.core.feed(Source::Keyboard, ev(T0 + 1600, EV_KEY, KEY_TAB, 0));
        h.core.feed(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_TAB, 1)); // cycling further
        h.core.feed(Source::Keyboard, ev(T0 + 2100, EV_KEY, KEY_TAB, 0));
        CHECK_EQ(h.take(), ""); // the whole Alt+Tab session, dropped
        h.core.feed(Source::Keyboard, ev(T0 + 2500, EV_KEY, KEY_LEFTALT, 0));
        CHECK_EQ(h.take(), ""); // Alt release: nothing to flush, it was consumed by Tab
        // A normal (non-Tab) Alt press still shows up, just delayed to
        // release time -- and normal keys around it are unaffected.
        h.core.feed(Source::Keyboard, ev(T0 + 3000, EV_KEY, KEY_LEFTALT, 1));
        CHECK_EQ(h.take(), "");
        h.core.feed(Source::Keyboard, ev(T0 + 3500, EV_KEY, KEY_LEFTALT, 0));
        CHECK_EQ(h.take(), "wait(0.003000)\nkd(KEY_LEFTALT)\nwait(0.000500)\nku(KEY_LEFTALT)\n");
        h.core.feed(Source::Keyboard, ev(T0 + 4000, EV_KEY, KEY_A, 1));
        CHECK_EQ(h.take(), "wait(0.000500)\nkd(KEY_A)\n");
    }

    // Alt+Tab, released in the OTHER order (Alt let go while Tab is
    // still physically held -- people don't always release both keys in
    // a fixed order): Tab's own release must still be caught later,
    // instead of leaking through once alt_pending_ is gone.
    {
        TranscribeOptions o; o.keyboard = true; o.ignore_alt_tab = true;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_LEFTALT, 1));
        h.core.feed(Source::Keyboard, ev(T0 + 1500, EV_KEY, KEY_TAB, 1));
        h.core.feed(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_LEFTALT, 0)); // Alt up FIRST, Tab still held
        CHECK_EQ(h.take(), "");
        h.core.feed(Source::Keyboard, ev(T0 + 2200, EV_KEY, KEY_TAB, 0)); // Tab's release, afterward
        CHECK_EQ(h.take(), ""); // must still be dropped, not transcribed as a lone ku(KEY_TAB)
        h.core.feed(Source::Keyboard, ev(T0 + 2700, EV_KEY, KEY_A, 1));
        CHECK_EQ(h.take(), "wait(0.002700)\nkd(KEY_A)\n"); // and normal typing afterward is unaffected
    }

    // Ignore Puppetry: nothing is transcribed while focused, and the
    // paused span is excluded from the next gap once focus moves away.
    {
        TranscribeOptions o; o.keyboard = true;
        Harness h(o);
        h.core.start(T0);
        h.core.feed(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_A, 1));
        CHECK_EQ(h.take(), "wait(0.001000)\nkd(KEY_A)\n");
        h.core.set_puppetry_focused(true, T0 + 2000);
        h.core.feed(Source::Keyboard, ev(T0 + 2500, EV_KEY, KEY_B, 1)); // typing in Puppetry itself
        CHECK_EQ(h.take(), ""); // not listening
        h.core.set_puppetry_focused(false, T0 + 10000); // 8ms spent focused on Puppetry
        h.core.feed(Source::Keyboard, ev(T0 + 10500, EV_KEY, KEY_C, 1));
        CHECK_EQ(h.take(), "wait(0.001500)\nkd(KEY_C)\n"); // the 8ms spent focused on Puppetry never happened
    }

    // same_start emits the one absolute line up front.
    {
        TranscribeOptions o; o.mouse = true; o.raw = true; o.same_start = true;
        Harness h(o, true, 7, 8);
        h.core.start(T0);
        CHECK_EQ(h.take(), "move_mouse(7, 8, move_to=True, time_=0)\n");
    }

    // -----------------------------------------------------------------
    // EventReorderQueue: the keyboard and the mouse are separate fds, and
    // whichever poll() happens to report first used to decide the order
    // events reached the core.
    // -----------------------------------------------------------------
    {
        // The actual bug: the mouse's click is stamped BEFORE the
        // keyboard's keypress but arrives (is pushed) after it, because
        // its fd woke up a moment later. Fed straight through, the core's
        // no-backwards-clock guard would clamp the gap to zero and emit
        // them in the wrong order; through the queue they come out by
        // timestamp.
        EventReorderQueue q;
        q.push(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_A, 1)); // arrived first...
        q.push(Source::Mouse, ev(T0 + 1500, EV_KEY, BTN_LEFT, 1)); // ...but happened earlier
        std::string order;
        q.drain_all([&](Source role, const RawEvent& e) {
            order += (role == Source::Mouse ? "M" : "K");
            order += std::to_string(e.time_us - T0) + " ";
        });
        CHECK_EQ(order, "M1500 K2000 ");
        ++checks;
        if (!q.empty()) { std::fprintf(stderr, "FAILED: drain_all left events behind\n"); std::exit(1); }

        // And end to end through the core: the click lands first, with the
        // real 500us gap between them preserved rather than flattened.
        TranscribeOptions o; o.keyboard = true; o.mouse = true;
        Harness h(o);
        h.core.start(T0);
        EventReorderQueue q2;
        q2.push(Source::Keyboard, ev(T0 + 2000, EV_KEY, KEY_A, 1));
        q2.push(Source::Mouse, ev(T0 + 1500, EV_KEY, BTN_LEFT, 1));
        q2.drain_all([&](Source role, const RawEvent& e) { h.core.feed(role, e); });
        CHECK_EQ(h.take(), "wait(0.001500)\nkd(BTN_LEFT)\nwait(0.000500)\nkd(KEY_A)\n");
    }
    {
        // Only events past the window are released; the rest stay queued
        // so a straggler from the other device can still slot in front.
        EventReorderQueue q;
        q.push(Source::Keyboard, ev(T0 + 1000, EV_KEY, KEY_A, 1));
        q.push(Source::Keyboard, ev(T0 + 9000, EV_KEY, KEY_B, 1));
        std::string got;
        auto collect = [&](Source, const RawEvent& e) { got += std::to_string(e.code) + " "; };
        q.drain_until(T0 + 5000, collect);
        CHECK_EQ(got, std::to_string(KEY_A) + " ");
        ++checks;
        if (q.size() != 1) { std::fprintf(stderr, "FAILED: expected 1 event still held\n"); std::exit(1); }
        ++checks;
        if (q.oldest_us() != T0 + 9000) { std::fprintf(stderr, "FAILED: wrong oldest_us\n"); std::exit(1); }

        // A late arrival stamped in between goes out before the one that
        // was already waiting.
        q.push(Source::Mouse, ev(T0 + 6000, EV_KEY, BTN_RIGHT, 1));
        got.clear();
        q.drain_until(T0 + 100000, collect);
        CHECK_EQ(got, std::to_string(BTN_RIGHT) + " " + std::to_string(KEY_B) + " ");
    }
    {
        // Events sharing a timestamp -- one hardware frame is several --
        // must keep the order the device reported, so a press can't
        // overtake the motion in its own frame.
        EventReorderQueue q;
        q.push(Source::Mouse, ev(T0 + 1000, EV_REL, REL_X, 5));
        q.push(Source::Mouse, ev(T0 + 1000, EV_REL, REL_Y, -3));
        q.push(Source::Mouse, ev(T0 + 1000, EV_SYN, SYN_REPORT, 0));
        std::string got;
        q.drain_all([&](Source, const RawEvent& e) { got += std::to_string(e.type) + ":" + std::to_string(e.code) + " "; });
        CHECK_EQ(got, std::to_string(EV_REL) + ":" + std::to_string(REL_X) + " " +
                      std::to_string(EV_REL) + ":" + std::to_string(REL_Y) + " " +
                      std::to_string(EV_SYN) + ":" + std::to_string(SYN_REPORT) + " ");
    }

    std::printf("All %d checks passed.\n", checks);
    return 0;
}
