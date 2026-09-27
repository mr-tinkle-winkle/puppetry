#include "transcriber.hpp"
#include <algorithm>
#include <cstdio>
#include <linux/input-event-codes.h>
#include "keycodes.hpp"

namespace puppetry {

static constexpr long long kResyncIntervalUs = 2000000; // set_positions drift correction

TranscriberCore::TranscriberCore(TranscribeOptions opts, Emit emit, CursorQuery query)
    : opts_(opts), emit_(std::move(emit)), query_(std::move(query)) {
    double hz = opts_.raw_hz > 0 ? opts_.raw_hz : 60.0;
    tick_us_ = std::max(1LL, (long long)(1e6 / hz));
}

std::string TranscriberCore::format_seconds(long long us) {
    if (us < 0) us = 0;
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%lld.%06lld", us / 1000000, us % 1000000);
    return buf;
}

std::string TranscriberCore::wait_line(long long gap_us) const {
    std::string s = "wait(" + format_seconds(gap_us);
    if (opts_.precise) s += ", precise=True";
    return s + ")";
}

void TranscriberCore::start(long long now_us) {
    last_us_ = now_us;
    next_tick_us_ = now_us + tick_us_;
    if (opts_.mouse && opts_.raw && (opts_.set_positions || opts_.same_start)) {
        int x, y;
        if (query_ && query_(x, y)) {
            if (opts_.same_start) {
                emit_("move_mouse(" + std::to_string(x) + ", " + std::to_string(y) + ", move_to=True, time_=0)\n");
            }
            if (opts_.set_positions) {
                tracking_ = true;
                pos_x_ = x;
                pos_y_ = y;
                next_resync_us_ = now_us + kResyncIntervalUs;
            }
        } else {
            emit_("# couldn't read starting cursor position -- falling back to relative deltas for this session\n");
        }
    }
}

void TranscriberCore::emit_timed(long long ts_us, const std::string& line) {
    long long gap = ts_us - last_us_;
    std::string text;
    if (gap > 0) text = wait_line(gap) + "\n";
    text += line + "\n";
    // Events from two devices are merged by timestamp before feeding
    // (see transcribe_main.cpp), but never let the clock run backwards.
    last_us_ = std::max(last_us_, ts_us);
    emit_(text);
}

void TranscriberCore::flush_motion() {
    if (acc_dx_ == 0 && acc_dy_ == 0) return;
    std::string line;
    if (tracking_) {
        pos_x_ += acc_dx_;
        pos_y_ += acc_dy_;
        line = "move_mouse(" + std::to_string(pos_x_) + ", " + std::to_string(pos_y_) + ", move_to=True, time_=0)";
    } else {
        line = "move_mouse(" + std::to_string(acc_dx_) + ", " + std::to_string(acc_dy_) + ", time_=0, easing=\"none\")";
    }
    acc_dx_ = acc_dy_ = 0;
    emit_timed(acc_ts_, line);
}

void TranscriberCore::set_puppetry_focused(bool on, long long now_us) {
    if (on == puppetry_focused_) return;
    if (on) {
        focus_paused_started_us_ = now_us;
    } else {
        long long paused = now_us - focus_paused_started_us_;
        if (paused > 0) {
            // Shift every clock this core tracks forward by the paused
            // span, so the next real gap/tick/resync doesn't count the
            // time spent focused on Puppetry itself.
            last_us_ += paused;
            next_tick_us_ += paused;
            next_resync_us_ += paused;
        }
    }
    puppetry_focused_ = on;
}

void TranscriberCore::feed(Source src, const RawEvent& ev) {
    if (puppetry_focused_) return; // "Ignore Puppetry": not listening right now

    const bool kb_role = src == Source::Keyboard || src == Source::Both;
    const bool mouse_role = src == Source::Mouse || src == Source::Both;
    bool& dropping = (src == Source::Mouse) ? dropping_mouse_ : dropping_kb_;

    // Kernel buffer overflow: per the evdev protocol, everything up to
    // the next SYN_REPORT is unreliable and must be discarded. Say so in
    // the script rather than silently recording a gap/jump.
    if (ev.type == EV_SYN && ev.code == SYN_DROPPED) {
        dropping = true;
        emit_("# kernel dropped input events here (buffer overflow) -- timing/motion around this point may be off\n");
        return;
    }
    if (dropping) {
        if (ev.type == EV_SYN && ev.code == SYN_REPORT) dropping = false;
        return;
    }

    if (ev.type == EV_KEY) {
        // Abort and the toggle hotkey are consumed here, never transcribed
        // -- same treatment as the ping key below. Abort additionally
        // flags the session to end, exactly like the daemon's own abort
        // key ends every running macro (see handle_key_event in
        // dispatch.cpp); the toggle hotkey needs no action here since the
        // GUI itself starts/stops this process.
        if (opts_.abort_code >= 0 && ev.code == opts_.abort_code) {
            if (ev.value == 1) abort_requested_ = true;
            return;
        }
        if (opts_.hotkey_code >= 0 && ev.code == opts_.hotkey_code) {
            return;
        }
        // Alt+Tab filtering: an Alt press is buffered (never emitted yet)
        // until Alt comes back up. If Tab arrives first, this was
        // Alt+Tab -- neither key gets transcribed. If Alt comes back up
        // with no Tab in between, it was just a normal Alt press, emitted
        // now (at its original timestamp) instead of immediately.
        if (opts_.ignore_alt_tab && (ev.code == KEY_LEFTALT || ev.code == KEY_RIGHTALT)) {
            if (ev.value == 1) {
                alt_pending_ = true;
                alt_tab_seen_ = false;
                alt_pending_code_ = ev.code;
                alt_pending_ts_ = ev.time_us;
                return;
            }
            if (ev.value == 0 && alt_pending_ && alt_pending_code_ == ev.code) {
                if (!alt_tab_seen_) {
                    flush_motion();
                    emit_timed(alt_pending_ts_, std::string("kd(") + key_code_name(alt_pending_code_) + ")");
                    emit_timed(ev.time_us, std::string("ku(") + key_code_name(ev.code) + ")");
                }
                alt_pending_ = false;
                alt_tab_seen_ = false;
                return;
            }
            // Autorepeat, or a release with nothing buffered (e.g. the
            // option was turned on mid-hold) -- neither needs handling.
        }
        if (opts_.ignore_alt_tab && ev.code == KEY_TAB && alt_pending_) {
            if (ev.value == 1) alt_tab_seen_ = true;
            return; // every Tab event while an Alt is pending belongs to that combo
        }
        if (ev.code == opts_.ping_code) {
            if (ev.value == 1 && opts_.mouse && !opts_.raw) {
                int x, y;
                long long gap = ev.time_us - last_us_;
                std::string text;
                if (query_ && query_(x, y)) {
                    // The gap IS the move's duration (no separate wait),
                    // exactly as before.
                    text = "move_mouse(" + std::to_string(x) + ", " + std::to_string(y) +
                           ", move_to=True, time_=" + format_seconds(gap) + ")\n";
                } else {
                    text = "# ping failed -- couldn't read cursor position (needs KDE/kdotool)\n";
                }
                last_us_ = std::max(last_us_, ev.time_us);
                emit_(text);
            }
            return; // the ping key itself is never transcribed as a keypress
        }
        if (ev.value != 0 && ev.value != 1) return; // autorepeat (2) isn't a real press
        const char* verb = ev.value == 1 ? "kd" : "ku";
        bool is_key = kb_role && opts_.keyboard && is_key_code(ev.code);
        bool is_btn = mouse_role && opts_.mouse && is_button_code(ev.code) && !is_key;
        if (!is_key && !is_btn) return;
        // Motion accumulated before this press must land BEFORE it --
        // the old tick-based transcriber could emit a click ahead of the
        // movement that preceded it (clicking at the wrong spot).
        flush_motion();
        emit_timed(ev.time_us, std::string(verb) + "(" + key_code_name(ev.code) + ")");
        return;
    }

    if (ev.type == EV_REL && mouse_role && opts_.mouse) {
        if (ev.code == REL_WHEEL) {
            flush_motion();
            emit_timed(ev.time_us, "wheel(" + std::to_string(ev.value) + ")");
        } else if (opts_.raw && (ev.code == REL_X || ev.code == REL_Y)) {
            (ev.code == REL_X ? acc_dx_ : acc_dy_) += ev.value;
            (ev.code == REL_X ? total_dx_ : total_dy_) += ev.value;
            acc_ts_ = ev.time_us;
        }
        return;
    }

    if (ev.type == EV_SYN && ev.code == SYN_REPORT && mouse_role && opts_.mouse && opts_.raw) {
        if (opts_.precise) {
            flush_motion(); // one line per hardware frame
        } else if (ev.time_us >= next_tick_us_) {
            flush_motion();
            next_tick_us_ += tick_us_;
            if (next_tick_us_ <= ev.time_us) next_tick_us_ = ev.time_us + tick_us_;
        }
    }
}

bool TranscriberCore::wants_resync(long long now_us) const {
    return tracking_ && now_us >= next_resync_us_;
}

void TranscriberCore::mark_resync_started(long long now_us, long long& snap_x, long long& snap_y) {
    next_resync_us_ = now_us + kResyncIntervalUs;
    snap_x = total_dx_;
    snap_y = total_dy_;
}

void TranscriberCore::apply_resync(int x, int y, long long snap_x, long long snap_y) {
    // The query reflects the cursor around when it STARTED; motion that
    // has arrived since is re-applied on top. Pending (unflushed) motion
    // is still in acc_ and gets added when it's flushed, so exclude it.
    pos_x_ = x + (total_dx_ - snap_x) - acc_dx_;
    pos_y_ = y + (total_dy_ - snap_y) - acc_dy_;
}

} // namespace puppetry
