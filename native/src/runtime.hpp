#pragma once
// Runtime -- every piece of daemon-wide mutable state that used to be a
// bare module-level global in macro_daemon.py (held, _ignore_flags,
// _grab_state, _watched_devices, _synth_held, _abort_event,
// _external_pause), plus the new actAs remap table. One singleton,
// passed by reference to whatever needs it (dispatch, primitives, the
// control socket) rather than hidden as more bare globals, so it's at
// least constructible in a unit test without a real daemon around it.
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include "evdev_device.hpp"
#include "uinput_device.hpp"

namespace puppetry {

// Raised (via a thread-local flag checked at cooperative points, same
// idea as the Python version's _MacroAborted exception) to unwind a
// macro's execution when the abort hotkey fires mid-run.
struct MacroAborted {};

// actAs(keyPressing, ignore_original, acting_keys...) -- see the
// project handoff for the full settled spec. One entry per remapped
// "pressing" key code.
struct ActAsMapping {
    bool ignore_original = false;
    std::vector<int> acting_keys;

    bool operator==(const ActAsMapping& other) const {
        return ignore_original == other.ignore_original && acting_keys == other.acting_keys;
    }
};

class Runtime {
public:
    UinputDevice ui_keyboard;
    UinputDevice ui_mouse;

    // ---- held keys (for combo matching) ----
    std::mutex held_mutex;
    std::set<int> held;

    // ---- ignore()/grab machinery ----
    std::mutex ignore_mutex;
    bool ignore_keyboard = false;
    bool ignore_mouse_buttons = false;
    bool ignore_mouse_movement = false;
    // Per-key ignore, added this session: specific key/button codes
    // blocked from reaching the rest of the system regardless of the
    // whole-category flags above. Function-only (ignore_keys()/
    // unignore_keys() in macro code) -- deliberately not exposed as a
    // GUI checkbox, unlike the three whole-device flags.
    std::unordered_set<int> ignored_keys;

    std::mutex grab_mutex;
    bool keyboard_grabbed = false;
    bool mouse_grabbed = false;
    InputDevice* watched_keyboard = nullptr; // non-owning, set by the dispatch loop
    InputDevice* watched_mouse = nullptr;

    // ---- synthetic held-key tracking (for abort_all release) ----
    std::mutex synth_held_mutex;
    std::set<int> synth_held;

    // ---- abort ----
    std::atomic<bool> abort_flag{false};

    // ---- external pause (GUI recording a combo) ----
    std::atomic<bool> external_pause{false};

    // ---- actAs ----
    std::mutex act_as_mutex;
    std::unordered_map<int, ActAsMapping> act_as_active;
    // Queued change for a key that's currently physically held -- takes
    // effect on that key's next release (finishing the current
    // press/release cycle under the OLD mapping first). std::nullopt as
    // the mapped value means "clear back to normal" is queued.
    std::unordered_map<int, std::optional<ActAsMapping>> act_as_pending;

    // Per-thread duration multiplier for wait()/tap()/move_mouse() --
    // reset to 1.0 at the start of every top-level macro run/iteration,
    // exactly like the Python version's threading.local().
    static double& speed_multiplier() {
        thread_local double value = 1.0;
        return value;
    }

    void check_abort() {
        if (abort_flag.load()) throw MacroAborted{};
    }
};

} // namespace puppetry
