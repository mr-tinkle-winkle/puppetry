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

namespace fs = std::filesystem;

namespace puppetry {

static const std::set<std::string> kOurVirtualDeviceNames = {
    "macro-daemon-virtual-keyboard",
    "macro-daemon-virtual-mouse",
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

std::optional<DeviceInfo> find_best_keyboard(const std::optional<std::string>& preferred_name) {
    std::vector<int> alpha_codes;
    for (int c = KEY_A; c <= KEY_Z; ++c) alpha_codes.push_back(c);

    struct Candidate { DeviceInfo info; int score; };
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
        for (int c : alpha_codes) if (has_bit(keybits, c)) ++score;
        if (score >= 20) candidates.push_back({dev, score});
    }

    if (candidates.empty()) return std::nullopt;

    if (preferred_name) {
        for (auto& c : candidates) {
            if (c.info.name == *preferred_name) return c.info;
        }
    }

    auto best = std::max_element(candidates.begin(), candidates.end(),
                                  [](const Candidate& a, const Candidate& b) { return a.score < b.score; });
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
        for (auto& c : candidates) {
            if (c.info.name == *preferred_name) return c.info;
        }
    }

    for (auto& c : candidates) {
        if (c.has_rel) return c.info; // real relative-motion mouse -- best match
    }
    return candidates.front().info;
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
        bool current_ok = !current_name.empty() && !is_our_virtual_device_name(current_name);
        if (current_ok && (!saved_name || current_name == *saved_name)) {
            return {*saved_path, current_name, ResolveHow::Remembered};
        }
    }

    auto found = (kind == "keyboard") ? find_best_keyboard(saved_name) : find_best_mouse(saved_name);
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
    return true;
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
