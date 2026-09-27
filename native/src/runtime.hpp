#pragma once
// Runtime -- every piece of daemon-wide mutable state that used to be a
// bare module-level global in macro_daemon.py (held, _ignore_flags,
// _grab_state, _watched_devices, _synth_held, _abort_event,
// _external_pause), plus the actAs remap table. One object, passed by
// reference to whatever needs it, so it's constructible in a unit test
// without a real daemon around it.
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <linux/input-event-codes.h>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include "evdev_device.hpp"
#include "uinput_device.hpp"

namespace puppetry {

// Thrown (from check_abort()) to unwind a macro's execution when the
// abort hotkey fires mid-run -- same idea as the Python version's
// _MacroAborted. Never escapes a macro thread.
struct MacroAborted {};

// actAs(keyPressing, ignore_original, acting_keys...) -- see the project
// handoff for the settled spec. One entry per remapped key code.
struct ActAsMapping {
    bool ignore_original = false;
    std::vector<int> acting_keys;

    bool operator==(const ActAsMapping& other) const {
        return ignore_original == other.ignore_original && acting_keys == other.acting_keys;
    }
};

// Lock-free set of key codes, one bit per code. Used for the codes OUR
// virtual devices currently hold down (so abort can release them).
// Every kd()/ku() touches this, so it must not take a mutex -- the
// first C++ version used a mutex-guarded std::set (lock + tree
// insert/erase per event).
class AtomicKeySet {
public:
    void set(int code) {
        if (code < 0 || code >= KEY_CNT) return;
        words_[code >> 6].fetch_or(1ull << (code & 63), std::memory_order_relaxed);
    }
    void clear(int code) {
        if (code < 0 || code >= KEY_CNT) return;
        words_[code >> 6].fetch_and(~(1ull << (code & 63)), std::memory_order_relaxed);
    }
    bool empty() const {
        for (const auto& w : words_) if (w.load(std::memory_order_relaxed)) return false;
        return true;
    }
    // Atomically takes (and clears) every set code.
    std::vector<int> take_all() {
        std::vector<int> out;
        for (size_t i = 0; i < words_.size(); ++i) {
            uint64_t w = words_[i].exchange(0, std::memory_order_relaxed);
            while (w) {
                int bit = __builtin_ctzll(w);
                out.push_back((int)(i * 64 + bit));
                w &= w - 1;
            }
        }
        return out;
    }

private:
    std::array<std::atomic<uint64_t>, (KEY_CNT + 63) / 64> words_{};
};

// Last known cursor position, so move_mouse(move_to=True) doesn't have to
// ask KWin where the cursor is when it already knows.
//
// Asking costs a kdotool SUBPROCESS -- tens of milliseconds, by far the
// most expensive thing any primitive does, and a transcribed
// "set positions" recording is nothing but back-to-back absolute moves.
// A move_to already ends by reading back where it actually landed (the
// correction pass), so that read populates this, and the next move_to
// starts from it instead of spawning kdotool again: one round-trip per
// absolute move instead of two, none at all for a move that's already
// there.
//
// Trust rules, deliberately pessimistic -- a wrong cached position would
// send the cursor to the wrong place, so anything that could have moved
// it drops the cache: emitting relative motion ourselves, the user
// touching the real mouse (see dispatch.cpp), or simply time passing
// (another application can warp the pointer whenever it likes).
class CursorCache {
public:
    static constexpr std::chrono::milliseconds kTtl{250};

    void store(int x, int y, std::chrono::steady_clock::time_point at) {
        uint64_t packed = ((uint64_t)(uint32_t)x << 32) | (uint32_t)y;
        packed_.store(packed, std::memory_order_relaxed);
        // Released after the position, so a reader that sees this
        // timestamp is guaranteed to see the position that goes with it.
        at_us_.store(us_of(at), std::memory_order_release);
    }

    bool get(int& x, int& y, std::chrono::steady_clock::time_point now) const {
        long long at = at_us_.load(std::memory_order_acquire);
        if (at == 0) return false;
        long long age = us_of(now) - at;
        if (age < 0 || age > std::chrono::duration_cast<std::chrono::microseconds>(kTtl).count()) return false;
        uint64_t packed = packed_.load(std::memory_order_relaxed);
        x = (int)(uint32_t)(packed >> 32);
        y = (int)(uint32_t)(packed & 0xffffffffu);
        return true;
    }

    void invalidate() { at_us_.store(0, std::memory_order_relaxed); }

private:
    static long long us_of(std::chrono::steady_clock::time_point tp) {
        // +1 so a genuine zero timestamp can't read as "invalid".
        return std::chrono::duration_cast<std::chrono::microseconds>(tp.time_since_epoch()).count() + 1;
    }
    std::atomic<uint64_t> packed_{0};
    std::atomic<long long> at_us_{0};
};

class Runtime {
public:
    UinputDevice ui_keyboard;
    UinputDevice ui_mouse;

    // See CursorCache above. Lives here rather than in primitives.cpp so
    // the dispatch loop can invalidate it when the user moves the real
    // mouse, and so a test can drive it without a daemon.
    CursorCache cursor;

    // ---- held keys (real input, for combo matching) ----
    std::mutex held_mutex;
    std::set<int> held;

    // ---- ignore()/grab machinery ----
    std::mutex ignore_mutex;
    bool ignore_keyboard = false;
    bool ignore_mouse_buttons = false;
    bool ignore_mouse_movement = false;
    // Per-key ignore: specific key/button codes blocked regardless of
    // the whole-category flags above. Function-only (ignore_keys()).
    std::unordered_set<int> ignored_keys;

    std::mutex grab_mutex;
    std::atomic<bool> keyboard_grabbed{false};
    std::atomic<bool> mouse_grabbed{false};
    InputDevice* watched_keyboard = nullptr; // non-owning, set by the dispatch loop
    InputDevice* watched_mouse = nullptr;

    // ---- synthetic held-key tracking (for abort_all release) ----
    AtomicKeySet synth_held;

    // ---- abort ----
    std::atomic<bool> abort_flag{false};

    // ---- external pause (GUI recording a combo) ----
    std::atomic<bool> external_pause{false};

    // ---- actAs ----
    std::mutex act_as_mutex;
    std::unordered_map<int, ActAsMapping> act_as_active;
    // Queued change for a key that's currently physically held -- takes
    // effect on that key's next release. std::nullopt = "clear" queued.
    std::unordered_map<int, std::optional<ActAsMapping>> act_as_pending;

    // Per-thread duration multiplier for wait()/tap()/move_mouse() --
    // reset to 1.0 at the start of every top-level macro run/iteration,
    // exactly like the Python version's threading.local().
    static double& speed_multiplier() {
        thread_local double value = 1.0;
        return value;
    }

    // Per-thread "timeline anchor" for wait(): the deadline the previous
    // wait() on this thread was aiming for. See primitives.cpp's
    // wait_fn() for how it's used to stop tiny per-line overheads from
    // accumulating into drift.
    static std::optional<std::chrono::steady_clock::time_point>& wait_anchor() {
        thread_local std::optional<std::chrono::steady_clock::time_point> value;
        return value;
    }

    void check_abort() const {
        if (abort_flag.load(std::memory_order_relaxed)) throw MacroAborted{};
    }
};

} // namespace puppetry
