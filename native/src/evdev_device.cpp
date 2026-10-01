#include "evdev_device.hpp"
#include <algorithm>
#include <cerrno>
#include <ctime>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <linux/input.h>
#include <set>
#include <stdexcept>
#include <sys/ioctl.h>
#include <unistd.h>
#include <vector>
#include "uinput_device.hpp" // kVirtual*Name -- one definition of our own devices' names

namespace fs = std::filesystem;

namespace puppetry {

static const std::set<std::string> kOurVirtualDeviceNames = {
    kVirtualKeyboardName,
    kVirtualMouseName,
    kVirtualGamepadName,
};

bool is_our_virtual_device_name(const std::string& name) {
    return kOurVirtualDeviceNames.count(name) > 0;
}

std::vector<DeviceInfo> list_input_devices() {
    std::vector<DeviceInfo> result;
    std::vector<fs::path> paths;
    const fs::path input_dir = "/dev/input";
    if (!fs::exists(input_dir)) return result;
    for (const auto& entry : fs::directory_iterator(input_dir)) {
        auto name = entry.path().filename().string();
        if (name.rfind("event", 0) == 0) paths.push_back(entry.path());
    }
    std::sort(paths.begin(), paths.end(), [](const fs::path& a, const fs::path& b) {
        // Numeric sort by the trailing digits so event2 < event10.
        auto num = [](const fs::path& p) {
            auto s = p.filename().string().substr(5);
            return s.empty() ? 0 : std::stoi(s);
        };
        return num(a) < num(b);
    });
    for (const auto& p : paths) {
        std::string name = device_name(p.string());
        if (!name.empty()) result.push_back({p.string(), name});
    }
    return result;
}

std::vector<std::pair<std::string, std::string>> unreadable_input_devices() {
    std::vector<std::pair<std::string, std::string>> out;
    const fs::path input_dir = "/dev/input";
    std::error_code ec;
    if (!fs::exists(input_dir, ec)) return out;
    for (const auto& entry : fs::directory_iterator(input_dir, ec)) {
        auto name = entry.path().filename().string();
        if (name.rfind("event", 0) != 0) continue;
        int fd = ::open(entry.path().c_str(), O_RDONLY | O_NONBLOCK);
        if (fd < 0) out.push_back({entry.path().string(), strerror(errno)});
        else ::close(fd);
    }
    std::sort(out.begin(), out.end());
    return out;
}

std::string device_name(const std::string& path) {
    int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK);
    if (fd < 0) return "";
    char buf[256] = {0};
    if (ioctl(fd, EVIOCGNAME(sizeof(buf) - 1), buf) < 0) {
        ::close(fd);
        return "";
    }
    ::close(fd);
    return std::string(buf);
}

static bool has_bit(const unsigned char* bits, int bit) {
    return (bits[bit / 8] >> (bit % 8)) & 1;
}

bool device_has_key(const std::string& path, int code) {
    int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK);
    if (fd < 0) return false;
    unsigned char keybits[(KEY_MAX / 8) + 1] = {0};
    bool ok = ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keybits)), keybits) >= 0;
    ::close(fd);
    return ok && has_bit(keybits, code);
}

bool device_has_rel(const std::string& path, int code) {
    int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK);
    if (fd < 0) return false;
    unsigned char relbits[(REL_MAX / 8) + 1] = {0};
    bool ok = ioctl(fd, EVIOCGBIT(EV_REL, sizeof(relbits)), relbits) >= 0;
    ::close(fd);
    return ok && has_bit(relbits, code);
}

const std::vector<int>& letter_key_codes() {
    // The 26 letters. NOT the range KEY_A..KEY_Z: evdev numbers keys along the
    // QWERTY rows (Q..P = 16..25, A..L = 30..38, Z..M = 44..50), and that range
    // holds only 10 letters -- a "20 letters" test over it can never pass.
    static const std::vector<int> codes = {
        KEY_A, KEY_B, KEY_C, KEY_D, KEY_E, KEY_F, KEY_G, KEY_H, KEY_I, KEY_J, KEY_K, KEY_L, KEY_M,
        KEY_N, KEY_O, KEY_P, KEY_Q, KEY_R, KEY_S, KEY_T, KEY_U, KEY_V, KEY_W, KEY_X, KEY_Y, KEY_Z};
    return codes;
}

static int letter_score(const unsigned char* keybits) {
    int n = 0;
    for (int c : letter_key_codes()) if (has_bit(keybits, c)) ++n;
    return n;
}

int device_bustype(const std::string& path) {
    int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK);
    if (fd < 0) return -1;
    struct input_id id = {};
    bool ok = ioctl(fd, EVIOCGID, &id) >= 0;
    ::close(fd);
    return ok ? id.bustype : -1;
}

int device_rank_penalty(const std::string& path) {
    // Auto-detect prefers real hardware: a Steam Controller (Valve, 0x28de)
    // presents its own keyboard and mouse in desktop mode, and other software
    // (StreamController, remappers) creates virtual ones -- both often declare
    // every key, so on letter count alone they'd tie with the real keyboard.
    if (device_vendor(path) == 0x28de) return 2;
    if (device_bustype(path) == BUS_VIRTUAL) return 1;
    return 0;
}

int device_vendor(const std::string& path) {
    int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK);
    if (fd < 0) return -1;
    struct input_id id = {};
    bool ok = ioctl(fd, EVIOCGID, &id) >= 0;
    ::close(fd);
    return ok ? id.vendor : -1;
}

std::string describe_device(const std::string& path) {
    std::string kinds;
    auto add = [&](const char* k) { if (!kinds.empty()) kinds += ","; kinds += k; };
    int letters = 0;
    for (int c : letter_key_codes()) letters += device_has_key(path, c);
    if (letters >= 20) add("keyboard");
    else if (device_has_key(path, KEY_ENTER) || device_has_key(path, KEY_ESC)) add("keys");
    if (device_has_key(path, BTN_LEFT) && device_has_rel(path, REL_X)) add("mouse");
    if (device_has_key(path, BTN_SOUTH) && device_has_abs(path, ABS_X)) add("gamepad");
    if (device_has_key(path, BTN_TOUCH)) add("touch");
    return kinds.empty() ? "other" : kinds;
}

bool device_has_abs(const std::string& path, int code) {
    int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK);
    if (fd < 0) return false;
    unsigned char absbits[(ABS_MAX / 8) + 1] = {0};
    bool ok = ioctl(fd, EVIOCGBIT(EV_ABS, sizeof(absbits)), absbits) >= 0;
    ::close(fd);
    return ok && has_bit(absbits, code);
}

std::optional<DeviceInfo> find_best_controller(const std::optional<std::string>& preferred_name) {
    std::vector<DeviceInfo> candidates;
    for (const auto& dev : list_input_devices()) {
        if (is_our_virtual_device_name(dev.name)) continue;
        // a gamepad: face buttons + a stick (motion sensors / touchpads of
        // the same controller show up as separate nodes without BTN_SOUTH)
        if (device_has_key(dev.path, BTN_SOUTH) && device_has_abs(dev.path, ABS_X)) candidates.push_back(dev);
    }
    if (candidates.empty()) return std::nullopt;
    if (preferred_name) {
        for (auto& c : candidates) if (c.name == *preferred_name) return c;
    }
    return candidates.front();
}

std::optional<DeviceInfo> find_best_keyboard(const std::optional<std::string>& preferred_name) {
    struct Candidate { DeviceInfo info; int score; int penalty; };
    std::vector<Candidate> candidates;

    for (const auto& dev : list_input_devices()) {
        if (is_our_virtual_device_name(dev.name)) continue;
        int fd = ::open(dev.path.c_str(), O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;
        unsigned char keybits[(KEY_MAX / 8) + 1] = {0};
        bool ok = ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keybits)), keybits) >= 0;
        ::close(fd);
        if (!ok) continue;
        int score = 0;
        score = letter_score(keybits);
        if (score >= 20) candidates.push_back({dev, score, device_rank_penalty(dev.path)});
    }

    if (candidates.empty()) return std::nullopt;

    if (preferred_name) {
        // several nodes can share a name (a keyboard's main node + its media-keys
        // node): the one with the most letter keys is the keyboard
        const Candidate* pick = nullptr;
        for (auto& c : candidates)
            if (c.info.name == *preferred_name && (!pick || c.score > pick->score)) pick = &c;
        if (pick) return pick->info;
    }

    // real hardware first, then the most letter keys; ties keep the lowest eventN
    auto best = std::max_element(candidates.begin(), candidates.end(), [](const Candidate& a, const Candidate& b) {
        if (a.penalty != b.penalty) return a.penalty > b.penalty;
        return a.score < b.score;
    });
    return best->info;
}

std::optional<DeviceInfo> find_best_mouse(const std::optional<std::string>& preferred_name) {
    struct Candidate { DeviceInfo info; bool has_rel; };
    std::vector<Candidate> candidates;

    for (const auto& dev : list_input_devices()) {
        if (is_our_virtual_device_name(dev.name)) continue;
        if (!device_has_key(dev.path, BTN_LEFT)) continue;
        bool has_rel = device_has_rel(dev.path, REL_X) && device_has_rel(dev.path, REL_Y);
        candidates.push_back({dev, has_rel});
    }

    if (candidates.empty()) return std::nullopt;

    if (preferred_name) {
        // same-name nodes: prefer the one that actually moves (REL_X/REL_Y)
        const Candidate* pick = nullptr;
        for (auto& c : candidates)
            if (c.info.name == *preferred_name && (!pick || (c.has_rel && !pick->has_rel))) pick = &c;
        if (pick) return pick->info;
    }

    for (int penalty = 0; penalty <= 2; ++penalty)        // real hardware before Steam / virtual mice
        for (auto& c : candidates)
            if (c.has_rel && device_rank_penalty(c.info.path) == penalty) return c.info;
    return candidates.front().info;
}

bool device_fits(const std::string& kind, const std::string& path) {
    if (kind == "keyboard") {
        int fd = ::open(path.c_str(), O_RDONLY | O_NONBLOCK);
        if (fd < 0) return false;
        unsigned char keybits[(KEY_MAX / 8) + 1] = {0};
        bool ok = ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keybits)), keybits) >= 0;
        ::close(fd);
        if (!ok) return false;
        return letter_score(keybits) >= 20;
    }
    if (kind == "mouse") {
        return device_has_key(path, BTN_LEFT) && device_has_rel(path, REL_X) && device_has_rel(path, REL_Y);
    }
    if (kind == "controller") return device_has_key(path, BTN_SOUTH) && device_has_abs(path, ABS_X);
    return true;
}

ResolvedDevice resolve_device(const std::string& kind,
                               const std::optional<std::string>& saved_path_in,
                               const std::optional<std::string>& saved_name_in) {
    std::optional<std::string> saved_path = saved_path_in;
    std::optional<std::string> saved_name = saved_name_in;

    if (saved_name && is_our_virtual_device_name(*saved_name)) {
        saved_path.reset();
        saved_name.reset();
    }

    if (saved_path && fs::exists(*saved_path)) {
        std::string current_name = device_name(*saved_path);
        bool current_ok = !current_name.empty() && !is_our_virtual_device_name(current_name)
                          && device_fits(kind, *saved_path);   // after a reboot the same path can be another node
        if (current_ok && (!saved_name || current_name == *saved_name)) {
            return {*saved_path, current_name, ResolveHow::Remembered};
        }
    }

    auto found = (kind == "keyboard") ? find_best_keyboard(saved_name)
               : (kind == "controller") ? find_best_controller(saved_name) : find_best_mouse(saved_name);
    if (!found) return {"", "", ResolveHow::NotFound};

    ResolveHow how = (saved_name && found->name == *saved_name) ? ResolveHow::Renumbered
                                                                  : ResolveHow::AutoDetected;
    return {found->path, found->name, how};
}

InputDevice::~InputDevice() { close(); }

bool InputDevice::open(const std::string& path) {
    fd_ = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd_ < 0) return false;
    path_ = path;
    for (int c = 0; c < ABS_CNT; ++c) { abs_min_[c] = 0; abs_max_[c] = 0; }
    unsigned char absbits[(ABS_MAX / 8) + 1] = {0};
    if (ioctl(fd_, EVIOCGBIT(EV_ABS, sizeof(absbits)), absbits) >= 0) {
        for (int c = 0; c < ABS_CNT; ++c) {
            if (!has_bit(absbits, c)) continue;
            struct input_absinfo ai;
            if (ioctl(fd_, EVIOCGABS(c), &ai) >= 0) { abs_min_[c] = ai.minimum; abs_max_[c] = ai.maximum; }
        }
    }
    return true;
}

double InputDevice::normalize_abs(int code, int value) const {
    if (code < 0 || code >= ABS_CNT) return 0.0;
    return normalize_abs_range(abs_min_[code], abs_max_[code], value);
}

double normalize_abs_range(int mn, int mx, int value) {
    if (mx <= mn) return 0.0;
    if (mn == -1 && mx == 1) return (double)value;                 // d-pad hat
    if (mn < 0) {                                                   // centered stick: -1..1
        double v = (2.0 * (value - mn) / (double)(mx - mn)) - 1.0;
        return v < -1 ? -1 : (v > 1 ? 1 : v);
    }
    double v = (value - mn) / (double)(mx - mn);                    // trigger (or unsigned stick): 0..1
    return v < 0 ? 0 : (v > 1 ? 1 : v);
}

void InputDevice::close() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

bool InputDevice::read_event(RawEvent& out) {
    return read_events(&out, 1) == 1;
}

int InputDevice::read_events(RawEvent* out, int max) {
    if (fd_ < 0) return -1;
    struct input_event evs[64];
    if (max > 64) max = 64;
    ssize_t n;
    do {
        n = ::read(fd_, evs, sizeof(evs[0]) * max);
    } while (n < 0 && errno == EINTR);
    if (n <= 0) return -1;
    int count = (int)(n / (ssize_t)sizeof(evs[0]));
    for (int i = 0; i < count; ++i) {
        out[i].type = evs[i].type;
        out[i].code = evs[i].code;
        out[i].value = evs[i].value;
        out[i].time_us = (long long)evs[i].input_event_sec * 1000000LL + evs[i].input_event_usec;
    }
    return count;
}

bool InputDevice::use_monotonic_clock() {
    int clk = CLOCK_MONOTONIC;
    return fd_ >= 0 && ioctl(fd_, EVIOCSCLOCKID, &clk) == 0;
}

void InputDevice::grab() {
    if (fd_ < 0) throw std::runtime_error("grab() on a closed device");
    if (ioctl(fd_, EVIOCGRAB, 1) < 0) {
        throw std::runtime_error("EVIOCGRAB failed: " + std::string(strerror(errno)));
    }
}

void InputDevice::ungrab() {
    if (fd_ < 0) return;
    if (ioctl(fd_, EVIOCGRAB, 0) < 0) {
        throw std::runtime_error("EVIOCGRAB(release) failed: " + std::string(strerror(errno)));
    }
}

} // namespace puppetry
