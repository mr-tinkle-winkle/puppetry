#pragma once
// The Steam Controller (2026, "Triton": wired 28de:1302, Bluetooth 28de:1303,
// and through its puck / dongle 28de:1304 or 28de:1305, interfaces 2-5) read
// straight from /dev/hidraw*.
//
// Why hidraw: the kernel has no gamepad driver for it, so outside Steam it only
// shows up as its own keyboard + mouse ("lizard mode"); its gamepad state comes
// as HID reports on the raw interface (KDE's Game Controller page reads it the
// same way, through SDL). Puppetry only READS: it never sends the controller a
// command, so lizard mode -- the controller working as a mouse and keyboard on
// the desktop -- stays exactly as it is. (SDL turns lizard mode off every 3 s
// while it holds the controller; that's what KDE's "Allow using as pointer and
// keyboard" switch is about.)
//
// Report layout from SDL (src/joystick/hidapi/steam/controller_structs.h and
// SDL_hidapi_steam_triton.c, zlib license): report IDs 0x42 / 0x45 (state) and
// 0x47 (state with a trackpad timestamp), little-endian, packed.
//
// Mapped onto the names Puppetry already uses for controllers, so combos,
// waitForPress(), getButtonsHeld(), getAxis() and the overlay all just work:
//   A B X Y -> BTN_SOUTH BTN_EAST BTN_WEST BTN_NORTH, bumpers BTN_TL BTN_TR,
//   trigger full-press clicks BTN_TL2 BTN_TR2, stick clicks BTN_THUMBL/R,
//   View / Menu / Steam -> BTN_SELECT / BTN_START / BTN_MODE (as SDL maps them),
//   d-pad BTN_DPAD_*, back grips L4 R4 L5 R5 -> BTN_TRIGGER_HAPPY1..4,
//   quick access (...) -> BTN_TRIGGER_HAPPY5, trackpad clicks L/R -> BTN_TRIGGER_HAPPY6/7.
//   Axes: LX LY RX RY (-1..1, right / down positive), LT RT (0..1), trackpads
//   LPAD_X LPAD_Y RPAD_X RPAD_Y (-1..1, ABS_HAT1X/Y, ABS_HAT2X/Y) and whether a
//   finger is on them LPAD_T RPAD_T (0/1, ABS_HAT3X/Y).
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "macro.hpp"

namespace puppetry {

struct Runtime;

struct TritonState {
    uint32_t buttons = 0;
    double lt = 0, rt = 0, lx = 0, ly = 0, rx = 0, ry = 0;
    double lpx = 0, lpy = 0, rpx = 0, rpy = 0;
    bool lpt = false, rpt = false;   // finger on the left / right trackpad
};

// One hidraw report (report ID first). False when it isn't a state report.
bool parse_triton_report(const uint8_t* data, size_t n, TritonState& out);

struct TritonButton { uint32_t bit; int code; };
const std::vector<TritonButton>& triton_buttons();
// The axes a state report carries, as (ABS code, value) pairs.
std::vector<std::pair<int, double>> triton_axes(const TritonState& s);

bool is_triton_device(uint16_t vendor, uint16_t product, int interface_number);

struct HidrawInfo {
    std::string path;        // /dev/hidrawN
    uint16_t vendor = 0, product = 0;
    int interface_number = -1;   // USB interface, -1 when not USB
    std::string name;
};
std::vector<HidrawInfo> list_hidraw();

// Reads `path` until it fails (unplugged / dongle gone). Wireless status reports
// mark the controller connected / disconnected; on the way out everything it
// held is released and its axes recentered.
void watch_steam_hid(Runtime& rt, MacroRegistry& registry, std::vector<std::unique_ptr<Macro>>& macros,
                     const std::string& path, int abort_code);

} // namespace puppetry
