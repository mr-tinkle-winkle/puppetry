#pragma once
// Real input device discovery + reading -- mirrors macro_daemon.py's
// find_best_keyboard/find_best_mouse/resolve_device and the InputDevice
// usage in watch_device(). Raw ioctl()s against /dev/input/eventN
// rather than linking libevdev, so the build has no dependency beyond
// kernel headers (linux/input.h, linux/uinput.h) that every Linux
// devel environment already has.
#include <optional>
#include <string>
#include <vector>

namespace puppetry {

struct DeviceInfo {
    std::string path;
    std::string name;
};

// Every readable /dev/input/eventN device, in numeric order.
std::vector<DeviceInfo> list_input_devices();

// True if this device declares EV_KEY for `code`.
bool device_has_key(const std::string& path, int code);
// True if this device declares EV_REL for `code`.
bool device_has_rel(const std::string& path, int code);

std::string device_name(const std::string& path);

// Names of our own uinput output devices -- auto-detect must never
// pick one of these as an input source (see the Python version's
// _OUR_VIRTUAL_DEVICE_NAMES comment for why: our virtual keyboard
// declares every KEY_* code, so it would otherwise win the A-Z
// coverage score outright).
bool is_our_virtual_device_name(const std::string& name);

// Picks the device most likely to be the main keyboard: whichever one
// covers the most of the standard A-Z range (>= 20 required, filtering
// out decoy sub-devices with a handful of keys). preferred_name, if
// given, wins outright over the capability score for a "sticky" match
// across reboots. Returns nullopt if nothing qualifies.
std::optional<DeviceInfo> find_best_keyboard(const std::optional<std::string>& preferred_name);

// Picks the device most likely to be a pointer: prefers BTN_LEFT +
// REL_X + REL_Y; falls back to any BTN_LEFT device (click-only/absolute
// touchpad nodes). Same preferred_name "sticky" behavior.
std::optional<DeviceInfo> find_best_mouse(const std::optional<std::string>& preferred_name);

enum class ResolveHow { Remembered, Renumbered, AutoDetected, NotFound };

struct ResolvedDevice {
    std::string path;
    std::string name;
    ResolveHow how;
};

// Three-tier priority for picking a device at startup -- mirrors
// resolve_device() exactly: (1) saved_path still exists and is still
// the same physical device by name, (2) a device matching saved_name
// exists somewhere else (renumbered), (3) fresh capability-based
// auto-detect. Never returns one of our own virtual output devices,
// even if state.json has one saved from a stale/bad detection.
ResolvedDevice resolve_device(const std::string& kind, // "keyboard" or "mouse"
                               const std::optional<std::string>& saved_path,
                               const std::optional<std::string>& saved_name);

// A raw input_event as read from an evdev device -- just the three
// fields the daemon actually needs (type/code/value); timestamps are
// read but discarded, same as the Python version never inspecting them.
struct RawEvent {
    unsigned short type;
    unsigned short code;
    int value;
};

class InputDevice {
public:
    InputDevice() = default;
    ~InputDevice();
    InputDevice(const InputDevice&) = delete;
    InputDevice& operator=(const InputDevice&) = delete;

    // Opens the device read-only, non-exclusive (never grab()s at open
    // time) -- same "watches read-only" posture as the Python version;
    // grab()/ungrab() below are separate, explicit calls.
    bool open(const std::string& path);
    void close();

    // Blocks until the next event, filling `out`. Returns false on EOF
    // or a fatal read error (device unplugged, etc.) -- caller should
    // stop watching this device.
    bool read_event(RawEvent& out);

    // EVIOCGRAB -- exclusive grab. Best-effort: throws on failure so
    // the caller can log it and treat the ignore() request as
    // unfulfilled, matching the Python version's try/except-and-print
    // around dev.grab().
    void grab();
    void ungrab();

    int fd() const { return fd_; }
    const std::string& path() const { return path_; }

private:
    int fd_ = -1;
    std::string path_;
};

} // namespace puppetry
