#include "steam_hid.hpp"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <linux/input.h>
#include <poll.h>
#include <unistd.h>

#include "dispatch.hpp"
#include "event_stream.hpp"
#include "runtime.hpp"

namespace fs = std::filesystem;

namespace puppetry {

namespace {
constexpr uint8_t kStateReport = 0x42, kStateReportBle = 0x45, kStateReportTs = 0x47;
constexpr uint8_t kWirelessStatus = 0x79, kWirelessStatusX = 0x46;
constexpr uint8_t kWirelessDisconnect = 1, kWirelessConnect = 2;
// sizes from SDL's TritonMTUNoQuat_t / TritonMTUNoQuat32TS_t (+1 for the report ID)
constexpr size_t kStateSize = 1 + 45, kStateTsSize = 1 + 45;

constexpr uint32_t T_A = 0x1, T_B = 0x2, T_X = 0x4, T_Y = 0x8, T_QAM = 0x10, T_R3 = 0x20, T_VIEW = 0x40,
                   T_R4 = 0x80, T_R5 = 0x100, T_R = 0x200, T_DOWN = 0x400, T_RIGHT = 0x800, T_LEFT = 0x1000,
                   T_UP = 0x2000, T_MENU = 0x4000, T_L3 = 0x8000, T_STEAM = 0x10000, T_L4 = 0x20000,
                   T_L5 = 0x40000, T_L = 0x80000, T_RPAD_TOUCH = 0x200000, T_RPAD_CLICK = 0x400000,
                   T_RT_CLICK = 0x800000, T_LPAD_TOUCH = 0x2000000, T_LPAD_CLICK = 0x4000000,
                   T_LT_CLICK = 0x8000000;

int16_t s16(const uint8_t* p) { return (int16_t)(uint16_t)(p[0] | (p[1] << 8)); }
uint32_t u32(const uint8_t* p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
double clamp(double v, double lo, double hi) { return v < lo ? lo : (v > hi ? hi : v); }

std::string read_first_line(const fs::path& p) {
    std::ifstream f(p);
    std::string s;
    std::getline(f, s);
    return s;
}
} // namespace

bool parse_triton_report(const uint8_t* d, size_t n, TritonState& out) {
    if (n < 1) return false;
    const uint8_t id = d[0];
    size_t pads;   // offset of sLeftPadX
    if (id == kStateReport || id == kStateReportBle) {
        if (n < kStateSize) return false;
        pads = 18;
    } else if (id == kStateReportTs) {
        if (n < kStateTsSize) return false;
        pads = 20;   // unTrackpadTimestamp sits before the pads
    } else {
        return false;
    }
    // d[1] = seq_num
    out.buttons = u32(d + 2);
    out.lt = clamp(s16(d + 6) / 32767.0, 0, 1);
    out.rt = clamp(s16(d + 8) / 32767.0, 0, 1);
    out.lx = clamp(s16(d + 10) / 32767.0, -1, 1);
    out.ly = clamp(-s16(d + 12) / 32767.0, -1, 1);    // report: up positive; Puppetry: down positive
    out.rx = clamp(s16(d + 14) / 32767.0, -1, 1);
    out.ry = clamp(-s16(d + 16) / 32767.0, -1, 1);
    out.lpt = (out.buttons & T_LPAD_TOUCH) != 0;
    out.rpt = (out.buttons & T_RPAD_TOUCH) != 0;
    out.lpx = out.lpt ? clamp(s16(d + pads) / 32768.0, -1, 1) : 0.0;
    out.lpy = out.lpt ? clamp(-s16(d + pads + 2) / 32768.0, -1, 1) : 0.0;
    out.rpx = out.rpt ? clamp(s16(d + pads + 6) / 32768.0, -1, 1) : 0.0;
    out.rpy = out.rpt ? clamp(-s16(d + pads + 8) / 32768.0, -1, 1) : 0.0;
    return true;
}

const std::vector<TritonButton>& triton_buttons() {
    static const std::vector<TritonButton> t = {
        {T_A, BTN_SOUTH}, {T_B, BTN_EAST}, {T_X, BTN_WEST}, {T_Y, BTN_NORTH},
        {T_L, BTN_TL}, {T_R, BTN_TR}, {T_LT_CLICK, BTN_TL2}, {T_RT_CLICK, BTN_TR2},
        {T_L3, BTN_THUMBL}, {T_R3, BTN_THUMBR},
        {T_MENU, BTN_SELECT}, {T_VIEW, BTN_START}, {T_STEAM, BTN_MODE},      // as SDL maps them
        {T_UP, BTN_DPAD_UP}, {T_DOWN, BTN_DPAD_DOWN}, {T_LEFT, BTN_DPAD_LEFT}, {T_RIGHT, BTN_DPAD_RIGHT},
        {T_L4, BTN_TRIGGER_HAPPY1}, {T_R4, BTN_TRIGGER_HAPPY2}, {T_L5, BTN_TRIGGER_HAPPY3},
        {T_R5, BTN_TRIGGER_HAPPY4}, {T_QAM, BTN_TRIGGER_HAPPY5},
        {T_LPAD_CLICK, BTN_TRIGGER_HAPPY6}, {T_RPAD_CLICK, BTN_TRIGGER_HAPPY7},
    };
    return t;
}

std::vector<std::pair<int, double>> triton_axes(const TritonState& s) {
    return {{ABS_X, s.lx}, {ABS_Y, s.ly}, {ABS_RX, s.rx}, {ABS_RY, s.ry}, {ABS_Z, s.lt}, {ABS_RZ, s.rt},
            {ABS_HAT1X, s.lpx}, {ABS_HAT1Y, s.lpy}, {ABS_HAT2X, s.rpx}, {ABS_HAT2Y, s.rpy},
            {ABS_HAT3X, s.lpt ? 1.0 : 0.0}, {ABS_HAT3Y, s.rpt ? 1.0 : 0.0}};
}

bool is_triton_device(uint16_t vendor, uint16_t product, int iface) {
    if (vendor != 0x28de) return false;
    if (product == 0x1302 || product == 0x1303) return true;                 // the controller itself
    if (product == 0x1304 || product == 0x1305) return iface >= 2 && iface <= 5;   // its puck / dongle
    return false;
}

std::vector<HidrawInfo> list_hidraw() {
    std::vector<HidrawInfo> out;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator("/sys/class/hidraw", ec)) {
        HidrawInfo h;
        h.path = "/dev/" + e.path().filename().string();
        fs::path dev = fs::canonical(e.path() / "device", ec);
        if (ec) { ec.clear(); continue; }
        std::ifstream f(dev / "uevent");
        std::string line;
        while (std::getline(f, line)) {
            if (line.rfind("HID_ID=", 0) == 0) {          // HID_ID=0003:000028DE:00001304
                unsigned bus = 0, ven = 0, prod = 0;
                if (std::sscanf(line.c_str() + 7, "%x:%x:%x", &bus, &ven, &prod) == 3) {
                    h.vendor = (uint16_t)ven;
                    h.product = (uint16_t)prod;
                }
            } else if (line.rfind("HID_NAME=", 0) == 0) {
                h.name = line.substr(9);
            }
        }
        std::string iface = read_first_line(dev.parent_path() / "bInterfaceNumber");   // USB only
        if (!iface.empty()) h.interface_number = (int)std::strtol(iface.c_str(), nullptr, 16);
        out.push_back(h);
    }
    std::sort(out.begin(), out.end(), [](const HidrawInfo& a, const HidrawInfo& b) { return a.path < b.path; });
    return out;
}

void watch_steam_hid(Runtime& rt, MacroRegistry& registry, std::vector<std::unique_ptr<Macro>>& macros,
                     const std::string& path, int abort_code) {
    int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        std::fprintf(stderr, "Steam Controller: can't open %s (%s) -- check its permissions\n", path.c_str(),
                     strerror(errno));
        return;
    }
    TritonState prev;
    bool connected = false;
    auto set_connected = [&](bool on) {
        if (on == connected) return;
        connected = on;
        controller_presence(rt, on ? +1 : -1);
        std::printf("Steam Controller %s (%s)\n", on ? "connected" : "disconnected", path.c_str());
        std::fflush(stdout);
    };
    auto release_all = [&] {
        long long t = realtime_us();
        for (const auto& b : triton_buttons())
            if (prev.buttons & b.bit) feed_controller_key(rt, registry, macros, abort_code, b.code, 0, t);
        for (const auto& [code, v] : triton_axes(prev))
            if (v != 0.0) feed_controller_axis(rt, code, 0.0, t);
        prev = TritonState{};
    };
    uint8_t buf[128];
    while (true) {
        struct pollfd p = {fd, POLLIN, 0};
        int r = ::poll(&p, 1, 1000);
        if (r < 0 && errno == EINTR) continue;
        if (r < 0 || (p.revents & (POLLERR | POLLHUP | POLLNVAL))) break;
        if (r == 0) continue;
        ssize_t n = ::read(fd, buf, sizeof(buf));
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) continue;
        if (n <= 0) break;
        if ((buf[0] == kWirelessStatus || buf[0] == kWirelessStatusX) && n >= 2) {
            if (buf[1] == kWirelessDisconnect) {
                release_all();
                set_connected(false);
            } else if (buf[1] == kWirelessConnect) {
                set_connected(true);
            }
            continue;
        }
        TritonState s;
        if (!parse_triton_report(buf, (size_t)n, s)) continue;
        set_connected(true);
        long long t = realtime_us();
        uint32_t changed = s.buttons ^ prev.buttons;
        if (changed)
            for (const auto& b : triton_buttons())
                if (changed & b.bit) feed_controller_key(rt, registry, macros, abort_code, b.code, (s.buttons & b.bit) ? 1 : 0, t);
        auto now_axes = triton_axes(s), old_axes = triton_axes(prev);
        for (size_t i = 0; i < now_axes.size(); ++i)
            if (now_axes[i].second != old_axes[i].second) feed_controller_axis(rt, now_axes[i].first, now_axes[i].second, t);
        prev = s;
    }
    ::close(fd);
    release_all();
    set_connected(false);
}

} // namespace puppetry
