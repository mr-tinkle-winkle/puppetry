#pragma once
// Live input transcription: turns real key/click/movement events into
// macro code (kd/ku/wait/move_mouse/wheel lines) as you perform them.
// C++ port of macro_gui.py's InputTranscriber, run as its own process
// (puppetry-transcribe, see transcribe_main.cpp) that streams lines to
// the editor on stdout.
//
// TIMING -- the reason for the rewrite. Every gap comes from the
// KERNEL's timestamp on each event (stamped when the kernel received it
// from the hardware; microsecond resolution, on CLOCK_MONOTONIC), not
// from when this program got around to reading it. Microseconds are the
// finest timing Linux itself records for input, so that's the
// resolution written out: wait(0.001234). The old Python transcriber:
//   - timed events with time.monotonic() at READ time (adds delivery +
//     Python jitter),
//   - rounded to 1ms (3 decimals),
//   - silently DELETED every gap under 20ms -- and reset its clock
//     anyway, so fast sequences (rolls, double-taps) replayed compressed,
//   - polled raw mouse motion on a 10ms select() timeout.
// None of that survives here; no time is ever dropped.
//
// Mouse motion modes (same user-facing options as before):
//   - not raw: motion isn't recorded; pressing the ping key samples the
//     cursor (kdotool/KWin) and emits one move_mouse(x, y, move_to=True,
//     time_=<gap>).
//   - raw, default: raw deltas are accumulated and emitted at most
//     raw_hz times a second (fewer, more readable lines).
//   - raw + precise: one line per HARDWARE FRAME at the mouse's own
//     polling rate, with wait(..., precise=True). This matters beyond
//     resolution: pointer acceleration depends on per-frame deltas and
//     their timing, so replaying the exact original frames reproduces
//     the original cursor path, while resampled (merged) frames get
//     accelerated differently. Costs CPU on playback (spin-waits) and
//     produces ~2 lines per mouse frame.
#include <algorithm>
#include <functional>
#include <string>
#include <vector>
#include "evdev_device.hpp"

namespace puppetry {

enum class Source { Keyboard, Mouse, Both };

// Merges the keyboard's and the mouse's event streams into ONE correctly
// ordered stream before either reaches TranscriberCore.
//
// The two devices are separate file descriptors, and poll() returns as
// soon as EITHER is readable -- so sorting each wakeup's harvest by
// timestamp (which is what this used to do) only orders events that
// happened to arrive together. When the mouse's fd became readable a
// fraction of a millisecond after the keyboard's, its events could carry
// an EARLIER kernel timestamp and still be fed second. TranscriberCore
// refuses to let its clock run backwards, so the gap between them got
// clamped to zero: a click and a keypress half a millisecond apart came
// out in the wrong order, back-to-back. Occasional, and exactly as subtle
// as "transcription is slightly inaccurate sometimes".
//
// The fix is to hold events briefly before feeding them, so anything that
// was going to arrive out of order has arrived. This delays when a line is
// WRITTEN by the window, and changes recorded timing not at all -- every
// gap comes from the kernel timestamp the event was already carrying.
class EventReorderQueue {
public:
    // How long to hold events. Needs to exceed the skew between the two
    // fds becoming readable (poll wakeup jitter -- well under a
    // millisecond in practice, a couple of milliseconds on a loaded
    // machine). Imperceptible either way: the GUI only inserts text into
    // the editor every 50ms anyway.
    static constexpr long long kWindowUs = 3000;

    struct Item {
        RawEvent ev;
        Source role;
    };

    void push(Source role, const RawEvent& ev) { items_.push_back({ev, role}); }
    bool empty() const { return items_.empty(); }
    size_t size() const { return items_.size(); }

    // Oldest queued timestamp. Only meaningful when !empty(); the caller
    // uses it to work out how long it can afford to sleep.
    long long oldest_us() const {
        long long oldest = items_.front().ev.time_us;
        for (const auto& it : items_) oldest = std::min(oldest, it.ev.time_us);
        return oldest;
    }

    // Hands every event stamped at or before cutoff_us to fn(role, ev),
    // oldest first, and forgets them.
    template <class F>
    void drain_until(long long cutoff_us, F&& fn) {
        sort_pending();
        size_t n = 0;
        while (n < items_.size() && items_[n].ev.time_us <= cutoff_us) {
            fn(items_[n].role, items_[n].ev);
            ++n;
        }
        items_.erase(items_.begin(), items_.begin() + (long)n);
    }

    // Everything still queued, whatever its age -- for shutdown, so the
    // last few events of a session aren't dropped.
    template <class F>
    void drain_all(F&& fn) {
        sort_pending();
        for (const auto& it : items_) fn(it.role, it.ev);
        items_.clear();
    }

private:
    void sort_pending() {
        // STABLE: events sharing a timestamp (one hardware frame emits
        // several) must keep the order the device reported them in.
        std::stable_sort(items_.begin(), items_.end(),
                         [](const Item& a, const Item& b) { return a.ev.time_us < b.ev.time_us; });
    }
    std::vector<Item> items_;
};

struct TranscribeOptions {
    bool keyboard = false;       // transcribe KEY_* presses
    bool mouse = false;          // transcribe BTN_* clicks, wheel, and (per mode) motion
    bool raw = false;            // record raw motion instead of ping waypoints
    bool set_positions = false;  // raw: absolute move_to coordinates per line
    bool same_start = false;     // raw: one absolute move_to at the start, then relative
    bool precise = false;        // per-frame motion + precise=True waits
    double raw_hz = 60;          // raw, non-precise: max motion lines per second
    int ping_code = 110;         // KEY_INSERT
    int abort_code = -1;         // daemon's abort key -- pressing it ends the session (-1 = none)
    int hotkey_code = -1;        // the editor's start/stop transcribe toggle (-1 = none)
    int restart_code = -1;       // the editor's stop-then-start-fresh toggle (-1 = none)
    int checkpoint_code = -1;    // inserts a literal checkpoint() line (-1 = none)
    bool ignore_alt_tab = false; // drop Alt+Tab (both keys) from the transcript entirely
    bool ignore_puppetry = false; // mute everything while Puppetry itself is the focused window
};

class TranscriberCore {
public:
    using Emit = std::function<void(const std::string&)>;
    using CursorQuery = std::function<bool(int& x, int& y)>;

    TranscriberCore(TranscribeOptions opts, Emit emit, CursorQuery query);

    // Call once before any events. `now_us` on the same clock as event
    // timestamps (CLOCK_MONOTONIC). Emits the same_start line if asked.
    void start(long long now_us);

    void feed(Source src, const RawEvent& ev);

    // set_positions drift correction: the caller queries the cursor
    // asynchronously (never blocking the event loop) and reports it here
    // together with the motion totals captured when the query started.
    bool wants_resync(long long now_us) const;
    void mark_resync_started(long long now_us, long long& snap_x, long long& snap_y);
    void apply_resync(int x, int y, long long snap_x, long long snap_y);

    // True once the abort key has been seen (checked by the caller after
    // every feed() to end the process the same way the daemon's abort key
    // ends every running macro -- see transcribe_main.cpp).
    bool abort_requested() const { return abort_requested_; }

    // "Ignore Puppetry": the caller polls window focus itself (kdotool is
    // its own subprocess round-trip, not something to do per-event) and
    // reports transitions here. While focused on, feed() drops every
    // event; once it goes off, the paused span is subtracted back out of
    // the timeline so the next real gap doesn't include however long the
    // user spent alt-tabbed into Puppetry itself.
    void set_puppetry_focused(bool on, long long now_us);

    // Formatting helpers (exposed for tests).
    static std::string format_seconds(long long us);
    std::string wait_line(long long gap_us) const;

private:
    // Every emission is bracketed by these two: begin_line() clears the
    // reusable buffer and puts the gap-since-last-line in it, end_line()
    // terminates it, advances the clock and hands it to the callback. No
    // per-event allocation anywhere in between (precise mode emits ~2000
    // lines a second).
    std::string& begin_line(long long ts_us);
    void end_line(long long ts_us);
    void append_wait(std::string& out, long long gap_us) const;

    void emit_timed(long long ts_us, const std::string& line);
    void emit_key(long long ts_us, const char* verb, int code); // "kd(KEY_A)" etc
    void flush_motion(); // emits pending motion at the time it actually happened

    TranscribeOptions opts_;
    Emit emit_;
    CursorQuery query_;
    std::string scratch_; // reused by begin_line()/end_line()
    long long last_us_ = 0;
    long long tick_us_;
    long long next_tick_us_ = 0;
    int acc_dx_ = 0, acc_dy_ = 0;
    long long acc_ts_ = 0; // kernel time of the latest accumulated motion
    long long total_dx_ = 0, total_dy_ = 0;
    bool tracking_ = false;
    long long pos_x_ = 0, pos_y_ = 0;
    long long next_resync_us_ = 0;
    bool dropping_kb_ = false, dropping_mouse_ = false;
    bool abort_requested_ = false;

    // Alt+Tab filtering: an Alt press is held back (not yet emitted)
    // until we know whether Tab follows before Alt comes back up.
    // tab_held_ additionally survives Alt's own release: people don't
    // always let go of Alt and Tab in a fixed order, and if Alt comes up
    // first, Tab's own eventual release must still be suppressed rather
    // than falling through to normal handling once alt_pending_ is gone.
    bool alt_pending_ = false, alt_tab_seen_ = false, tab_held_ = false;
    int alt_pending_code_ = 0;
    long long alt_pending_ts_ = 0;

    // "Ignore Puppetry" focus muting.
    bool puppetry_focused_ = false;
    long long focus_paused_started_us_ = 0;
};

} // namespace puppetry
