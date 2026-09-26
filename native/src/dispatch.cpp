#include "dispatch.hpp"
#include "keycodes.hpp"
#include "primitives.hpp"
#include <algorithm>
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
    return true;
}

void watch_device(Runtime& rt, MacroRegistry& registry, std::vector<std::unique_ptr<Macro>>& macros,
                   InputDevice& dev, const std::string& kind, int abort_code) {
    (void)registry;
    if (kind == "keyboard") {
        std::lock_guard<std::mutex> lock(rt.grab_mutex);
        rt.watched_keyboard = &dev;
    } else {
        std::lock_guard<std::mutex> lock(rt.grab_mutex);
        rt.watched_mouse = &dev;
    }
    std::fprintf(stderr, "Watching %s -- read-only, not grabbed\n", dev.path().c_str());

    RawEvent ev;
    while (dev.read_event(ev)) {
        if (ev.type == EV_KEY) {
            bool original_wants_forward = apply_act_as(rt, ev.code, ev.value);

            handle_key_event(rt, macros, abort_code, ev.code, ev.value, [&](Macro& m) {
                trigger_macro(rt, registry, m);
            });

            bool grabbed = (kind == "keyboard") ? rt.keyboard_grabbed : rt.mouse_grabbed;
            if (grabbed) {
                // Mirrors watch_device()'s forwarding rules exactly: a
                // real key-up is ALWAYS forwarded regardless of
                // suppression (the stuck-key safety net), a real
                // key-down is forwarded only if nothing is currently
                // suppressing it.
                bool forward = (ev.value == 0) || should_forward(rt, kind, ev.code, original_wants_forward);
                if (forward) {
                    UinputDevice& out = is_button_code(ev.code) ? rt.ui_mouse : rt.ui_keyboard;
                    out.write_key(ev.code, ev.value);
                    out.syn();
                }
            }
        } else if (ev.type == EV_REL && kind == "mouse") {
            bool movement_ignored;
            {
                std::lock_guard<std::mutex> lock(rt.ignore_mutex);
                movement_ignored = rt.ignore_mouse_movement;
            }
            if (rt.mouse_grabbed && !movement_ignored) {
                rt.ui_mouse.write_rel(ev.code, ev.value);
                rt.ui_mouse.syn();
            }
        }
    }
}

} // namespace puppetry
