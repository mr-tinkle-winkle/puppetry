#include "dispatch.hpp"
#include "event_stream.hpp"
#include "keycodes.hpp"
#include "primitives.hpp"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <linux/input.h>

namespace puppetry {

static bool contains(const std::vector<int>& v, int x) {
    return std::find(v.begin(), v.end(), x) != v.end();
}

static bool is_subset(const std::vector<int>& a, const std::vector<int>& b) {
    for (int x : a) if (!contains(b, x)) return false;
    return true;
}

static bool is_strict_subset(const std::vector<int>& a, const std::vector<int>& b) {
    return a.size() < b.size() && is_subset(a, b);
}

std::vector<Macro*> maximal_matching_macros(const std::vector<std::unique_ptr<Macro>>& macros,
                                             int just_pressed_code,
                                             const std::vector<int>& held_codes) {
    std::vector<Macro*> matching;
    for (const auto& m : macros) {
        if (!m->enabled || m->combo.empty()) continue;
        if (!contains(m->combo, just_pressed_code)) continue;
        if (!is_subset(m->combo, held_codes)) continue;
        matching.push_back(m.get());
    }
    std::vector<Macro*> maximal;
    for (Macro* m : matching) {
        bool superseded = false;
        for (Macro* other : matching) {
            if (other != m && is_strict_subset(m->combo, other->combo)) { superseded = true; break; }
        }
        if (!superseded) maximal.push_back(m);
    }
    return maximal;
}

void handle_key_event(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros, int abort_code,
                       int code, int value, const std::function<void(Macro&)>& on_trigger) {
    if (value == 1 && code == abort_code) {
        abort_all(rt);
        return;
    }

    if (value == 1) { // fresh key down (autorepeat is value == 2, ignored below)
        rt.notify_press(code); // waitForPress()
        std::vector<int> current;
        {
            std::lock_guard<std::mutex> lock(rt.held_mutex);
            rt.held.insert(code);
            current.assign(rt.held.begin(), rt.held.end());
        }
        if (rt.external_pause.load()) return;

        for (Macro* m : maximal_matching_macros(macros, code, current)) {
            if (m->trigger_edge == TriggerEdge::Up) {
                m->runtime->armed_up.store(true);
            } else {
                on_trigger(*m);
            }
        }
    } else if (value == 0) { // key up
        std::vector<int> current;
        {
            std::lock_guard<std::mutex> lock(rt.held_mutex);
            rt.held.erase(code);
            current.assign(rt.held.begin(), rt.held.end());
        }

        for (const auto& mp : macros) {
            Macro& m = *mp;
            if (m.repeat_mode == RepeatMode::Hold && m.runtime->active_hold.load()) {
                if (contains(m.combo, code) && !is_subset(m.combo, current)) {
                    stop_macro_loop(m);
                }
            }
            if (m.trigger_edge == TriggerEdge::Up && m.runtime->armed_up.load()) {
                bool combo_still_held = false;
                for (int c : m.combo) if (contains(current, c)) { combo_still_held = true; break; }
                if (contains(m.combo, code) && !combo_still_held) {
                    m.runtime->armed_up.store(false);
                    on_trigger(m);
                }
            }
        }
    }
    // value == 2 (autorepeat): ignored, same as the Python version.
}

bool apply_act_as(Runtime& rt, int code, int value) {
    std::optional<ActAsMapping> mapping_to_use;
    bool suppress_original = false;

    {
        std::lock_guard<std::mutex> lock(rt.act_as_mutex);
        auto active_it = rt.act_as_active.find(code);
        if (active_it != rt.act_as_active.end()) {
            mapping_to_use = active_it->second;
            suppress_original = active_it->second.ignore_original;
        }

        if (value == 0) {
            // Release: finishing this cycle under the mapping captured
            // above (if any). If a change was queued while this key was
            // held, commit it now -- it governs the NEXT press, not
            // this release.
            auto pending_it = rt.act_as_pending.find(code);
            if (pending_it != rt.act_as_pending.end()) {
                if (pending_it->second) rt.act_as_active[code] = *pending_it->second;
                else rt.act_as_active.erase(code);
                rt.act_as_pending.erase(pending_it);
            }
        }
    }

    if (!mapping_to_use) return true; // not a remapped key -- forward normally

    if (value == 1 || value == 0) {
        for (int acting_code : mapping_to_use->acting_keys) {
            if (value == 1) kd(rt, acting_code);
            else ku(rt, acting_code);
        }
    }
    return !suppress_original;
}

static bool should_forward(Runtime& rt, const std::string& kind, int code, bool original_wants_forward) {
    if (!original_wants_forward) return false;
    std::lock_guard<std::mutex> lock(rt.ignore_mutex);
    if (kind == "keyboard" && rt.ignore_keyboard) return false;
    if (kind == "mouse" && rt.ignore_mouse_buttons && is_button_code(code)) return false;
    if (rt.ignored_keys.count(code)) return false;
    if (rt.repress_codes.count(code)) return false;
    if (rt.repress_any > 0) return false;
    return true;
}

void watch_device(Runtime& rt, MacroRegistry& registry, std::vector<std::unique_ptr<Macro>>& macros,
                   InputDevice& dev, const std::string& kind, int abort_code) {
    // "controller", "extra" and "extra_pad" devices are only read: never
    // grabbed, never forwarded (their own events already reach the system).
    const bool primary = (kind == "keyboard" || kind == "mouse");
    if (primary) {
        std::lock_guard<std::mutex> lock(rt.grab_mutex);
        (kind == "keyboard" ? rt.watched_keyboard : rt.watched_mouse) = &dev;
    }
    std::fprintf(stderr, "Watching %s -- read-only, not grabbed\n", dev.path().c_str());

    auto on_trigger = [&](Macro& m) { trigger_macro(rt, registry, m); };

    // While the real device is grabbed (ignore()/actAs suppression), we
    // re-emit whatever isn't suppressed. Events are collected per
    // hardware frame and written as ONE frame on the real SYN_REPORT --
    // the first C++ version wrote each event + its own SYN separately,
    // which split a diagonal mouse movement (REL_X + REL_Y) into two
    // frames and cost a syscall per event.
    std::vector<struct input_event> out_kb, out_mouse;
    out_kb.reserve(16);
    out_mouse.reserve(16);
    auto push = [](std::vector<struct input_event>& v, unsigned short type, unsigned short code, int value) {
        struct input_event e = {};
        e.type = type; e.code = code; e.value = value;
        v.push_back(e);
    };

    RawEvent evs[64];
    int frame_dx = 0, frame_dy = 0, frame_wheel = 0, frame_hwheel = 0; // for the event stream
    long long frame_t = 0;
    while (true) {
        int n = dev.read_events(evs, 64);
        if (n < 0) break;
        for (int i = 0; i < n; ++i) {
            const RawEvent& ev = evs[i];
            if (ev.type == EV_KEY) {
                if (ev.value != 2) {
                    EventStream* es = g_event_stream.load(std::memory_order_relaxed);
                    if (es && es->active()) es->publish('r', ev.time_us, 'k', ev.code, ev.value);
                }
                bool original_wants_forward = apply_act_as(rt, ev.code, ev.value);
                // Decide forwarding BEFORE handling the event: handling it
                // can wake a waitForPress()/waitForReactivation(repress)
                // thread that immediately drops its repress codes, and the
                // press it was waiting for must still be swallowed.
                // (a controller is never grabbed: its presses aren't forwarded, just seen)
                bool grabbed = (kind == "keyboard") ? rt.keyboard_grabbed.load()
                             : (kind == "mouse") ? rt.mouse_grabbed.load() : false;
                // A real key-UP is ALWAYS forwarded (stuck-key safety
                // net), a key-DOWN only if nothing suppresses it.
                bool forward = grabbed &&
                               ((ev.value == 0) || should_forward(rt, kind, ev.code, original_wants_forward));
                handle_key_event(rt, macros, abort_code, ev.code, ev.value, on_trigger);
                if (forward) push(is_mouse_button(ev.code) ? out_mouse : out_kb, EV_KEY, ev.code, ev.value);
            } else if (ev.type == EV_REL && (kind == "mouse" || kind == "extra" || kind == "extra_pad")) {
                // The user just moved the real mouse, so any position
                // move_mouse(move_to=True) had cached is now wrong. One
                // relaxed atomic store; safe to do even when the motion
                // is being suppressed below (it only costs a later query).
                if (ev.code == REL_X || ev.code == REL_Y) rt.cursor.invalidate();
                switch (ev.code) {                    // summed per hardware frame for the stream
                    case REL_X: frame_dx += ev.value; break;
                    case REL_Y: frame_dy += ev.value; break;
                    case REL_WHEEL: frame_wheel += ev.value; break;
                    case REL_HWHEEL: frame_hwheel += ev.value; break;
                    default: break;
                }
                frame_t = ev.time_us;
                if (kind == "mouse" && rt.mouse_grabbed.load()) {
                    bool movement_ignored;
                    {
                        std::lock_guard<std::mutex> lock(rt.ignore_mutex);
                        movement_ignored = rt.ignore_mouse_movement;
                    }
                    bool is_motion = ev.code == REL_X || ev.code == REL_Y;
                    if (!(is_motion && movement_ignored)) push(out_mouse, EV_REL, ev.code, ev.value);
                }
            } else if (ev.type == EV_ABS && (kind == "controller" || kind == "extra_pad")) {
                double v = dev.normalize_abs(ev.code, ev.value);
                double prev;
                {
                    std::lock_guard<std::mutex> lock(rt.axes_mutex);
                    prev = rt.axes.count(ev.code) ? rt.axes[ev.code] : 0.0;
                    rt.axes[ev.code] = v;
                }
                // sticks jitter at ~1 kHz: only stream visible changes
                if (v != prev && (std::abs(v - prev) >= 0.004 || v == 0.0 || v == 1.0 || v == -1.0)) {
                    EventStream* es = g_event_stream.load(std::memory_order_relaxed);
                    if (es && es->active()) es->publish('r', ev.time_us, 'a', ev.code, (int)std::lround(v * 10000));
                }
            } else if (ev.type == EV_SYN && ev.code == SYN_REPORT && !primary) {
                if (frame_dx | frame_dy | frame_wheel | frame_hwheel) {
                    EventStream* es = g_event_stream.load(std::memory_order_relaxed);
                    if (es && es->active()) {
                        if (frame_dx | frame_dy) es->publish('r', frame_t, 'm', frame_dx, frame_dy);
                        if (frame_wheel | frame_hwheel) es->publish('r', frame_t, 'w', frame_wheel, frame_hwheel);
                    }
                    frame_dx = frame_dy = frame_wheel = frame_hwheel = 0;
                }
            } else if (ev.type == EV_SYN && ev.code == SYN_REPORT) {
                if (frame_dx | frame_dy | frame_wheel | frame_hwheel) {
                    EventStream* es = g_event_stream.load(std::memory_order_relaxed);
                    if (es && es->active()) {
                        if (frame_dx | frame_dy) es->publish('r', frame_t, 'm', frame_dx, frame_dy);
                        if (frame_wheel | frame_hwheel) es->publish('r', frame_t, 'w', frame_wheel, frame_hwheel);
                    }
                    frame_dx = frame_dy = frame_wheel = frame_hwheel = 0;
                }
                if (!out_kb.empty()) { rt.ui_keyboard.frame(out_kb.data(), out_kb.size(), true); out_kb.clear(); }
                if (!out_mouse.empty()) { rt.ui_mouse.frame(out_mouse.data(), out_mouse.size(), true); out_mouse.clear(); }
            }
        }
    }
}

} // namespace puppetry
