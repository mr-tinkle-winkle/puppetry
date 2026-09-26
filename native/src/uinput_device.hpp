#pragma once
// Virtual output devices -- mirrors macro_daemon.py's VIRTUAL OUTPUT
// DEVICES section. Two separate uinput devices (keyboard, mouse), not
// one combined device, for the same reason as the Python version: a
// single device advertising both the full keymap and relative-pointer
// capabilities can get misclassified by the compositor/libinput (not
// ID_INPUT_KEYBOARD + ID_INPUT_MOUSE the way two separate devices are).
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
    // failure (missing /dev/uinput, no permission, etc.) -- same
    // "crash loudly at startup" behavior as the Python version's UInput()
    // constructor, since there's nothing sensible to do without it.
    void create(const std::string& name, const std::vector<int>& key_codes, bool with_rel);

    void write_key(int code, int value); // 1 = down, 0 = up
    void write_rel(int rel_code, int amount);
    void syn();

    bool is_open() const { return fd_ >= 0; }

private:
    int fd_ = -1;
};

} // namespace puppetry
