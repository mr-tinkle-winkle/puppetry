#pragma once
// Virtual output devices -- mirrors macro_daemon.py's VIRTUAL OUTPUT
// DEVICES section. Two separate uinput devices (keyboard, mouse), not
// one combined device, for the same reason as the Python version: a
// single device advertising both the full keymap and relative-pointer
// capabilities can get misclassified by the compositor/libinput (not
// ID_INPUT_KEYBOARD + ID_INPUT_MOUSE the way two separate devices are).
//
// HOT PATH: every public emit call is exactly ONE write() syscall,
// including its SYN_REPORT. The first C++ version did one syscall per
// event plus one for the SYN (so a tap() was 4 syscalls instead of 2).
#include <atomic>
#include <linux/input.h>
#include <string>
#include <vector>

namespace puppetry {

// Identity of our two virtual devices, in one place: the names are what
// evdev_device.cpp excludes from auto-detect and what KDE keys its
// per-device input settings on (see pointer_accel.hpp), and the
// vendor/product ids are half of that same key. Changing any of these
// changes the KDE settings group a user's saved preferences live under,
// so don't, casually.
inline constexpr const char* kVirtualKeyboardName = "macro-daemon-virtual-keyboard";
inline constexpr const char* kVirtualMouseName = "macro-daemon-virtual-mouse";
inline constexpr const char* kVirtualGamepadName = "macro-daemon-virtual-gamepad";
inline constexpr int kVirtualVendorId = 0x1234;
inline constexpr int kVirtualProductId = 0x5678;

class UinputDevice {
public:
    UinputDevice() = default;
    ~UinputDevice();
    UinputDevice(const UinputDevice&) = delete;
    UinputDevice& operator=(const UinputDevice&) = delete;

    // Opens /dev/uinput and creates a device named `name` that can emit
    // EV_KEY for every code in `key_codes`, and (if `with_rel` is true)
    // EV_REL for REL_X/REL_Y/REL_WHEEL. Throws std::runtime_error on
    // failure (missing /dev/uinput, no permission, etc.).
    void create(const std::string& name, const std::vector<int>& key_codes, bool with_rel);
    bool ok() const { return fd_ >= 0; }   // created

    // A controller: the Xbox-style button set, two sticks, two triggers
    // and a d-pad hat (the usual evdev layout games expect). Created only
    // when "virtual_controller" is on (an extra controller can shift player
    // numbers in some games).
    void create_gamepad(const std::string& name);
    // One absolute-axis value + SYN_REPORT. `norm` is -1..1 for sticks and
    // the d-pad, 0..1 for triggers; scaled to the device's range here.
    void abs_frame(int code, double norm);

    // One key/button transition + SYN_REPORT, in a single write().
    void key_frame(int code, int value);
    // Relative motion (either axis may be 0) + SYN_REPORT, single write().
    void rel_frame(int dx, int dy);
    // Wheel notches + SYN_REPORT, single write().
    void wheel_frame(int amount);
    // Arbitrary already-built events; appends a SYN_REPORT if
    // `add_syn`. Used to forward a grabbed real device's whole frame at
    // once (see dispatch.cpp) instead of splitting it.
    void frame(const struct input_event* events, size_t n, bool add_syn);

    bool is_open() const { return fd_ >= 0; }

    // Benchmarks/tests only: route writes to an arbitrary file (e.g.
    // /dev/null) instead of a real uinput device, so the write()
    // syscall cost is still measured on machines without /dev/uinput.
    void open_sink(const std::string& path);

    // Events written since start (benchmark/diagnostics only). Atomic
    // because several macro threads can emit through the same device at
    // once -- a plain ++ here was a data race (harmless in practice, but
    // it's also what makes -fsanitize=thread flag the hot path).
    unsigned long long frames_written() const { return frames_.load(std::memory_order_relaxed); }

private:
    void write_all(const struct input_event* ev, size_t n);
    int fd_ = -1;
    bool is_sink_ = false;
    std::atomic<unsigned long long> frames_{0};
    int abs_min_[ABS_CNT] = {};
    int abs_max_[ABS_CNT] = {};
};

} // namespace puppetry
