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
#include <linux/input.h>
#include <string>
#include <vector>

namespace puppetry {

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

    // Events written since start (benchmark/diagnostics only).
    unsigned long long frames_written() const { return frames_; }

private:
    void write_all(const struct input_event* ev, size_t n);
    int fd_ = -1;
    bool is_sink_ = false;
    unsigned long long frames_ = 0;
};

} // namespace puppetry
