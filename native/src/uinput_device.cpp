#include "uinput_device.hpp"
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <linux/uinput.h>
#include <stdexcept>
#include <sys/ioctl.h>
#include <unistd.h>

namespace puppetry {

UinputDevice::~UinputDevice() {
    if (fd_ >= 0) {
        if (!is_sink_) ioctl(fd_, UI_DEV_DESTROY);
        close(fd_);
    }
}

void UinputDevice::open_sink(const std::string& path) {
    fd_ = open(path.c_str(), O_WRONLY);
    is_sink_ = true;
}

void UinputDevice::create(const std::string& name, const std::vector<int>& key_codes, bool with_rel) {
    fd_ = open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd_ < 0) {
        throw std::runtime_error("failed to open /dev/uinput: " + std::string(strerror(errno)));
    }
    if (ioctl(fd_, UI_SET_EVBIT, EV_KEY) < 0) {
        throw std::runtime_error("UI_SET_EVBIT EV_KEY failed: " + std::string(strerror(errno)));
    }
    for (int code : key_codes) {
        ioctl(fd_, UI_SET_KEYBIT, code); // a rejected code shouldn't take the whole device down
    }
    if (with_rel) {
        if (ioctl(fd_, UI_SET_EVBIT, EV_REL) < 0) {
            throw std::runtime_error("UI_SET_EVBIT EV_REL failed: " + std::string(strerror(errno)));
        }
        ioctl(fd_, UI_SET_RELBIT, REL_X);
        ioctl(fd_, UI_SET_RELBIT, REL_Y);
        ioctl(fd_, UI_SET_RELBIT, REL_WHEEL);
    }

    struct uinput_setup usetup;
    memset(&usetup, 0, sizeof(usetup));
    usetup.id.bustype = BUS_VIRTUAL;
    usetup.id.vendor = 0x1234;
    usetup.id.product = 0x5678;
    strncpy(usetup.name, name.c_str(), UINPUT_MAX_NAME_SIZE - 1);

    if (ioctl(fd_, UI_DEV_SETUP, &usetup) < 0) {
        throw std::runtime_error("UI_DEV_SETUP failed: " + std::string(strerror(errno)));
    }
    if (ioctl(fd_, UI_DEV_CREATE) < 0) {
        throw std::runtime_error("UI_DEV_CREATE failed: " + std::string(strerror(errno)));
    }
}

void UinputDevice::write_all(const struct input_event* ev, size_t n) {
    if (fd_ < 0) return;
    // Best-effort, never throws: a full buffer or a mid-teardown race
    // must never take a macro thread down. The kernel stamps each
    // event itself (uinput ignores the timestamp we pass), so it's left
    // zeroed.
    ssize_t r = ::write(fd_, ev, n * sizeof(*ev));
    (void)r;
    ++frames_;
}

void UinputDevice::key_frame(int code, int value) {
    struct input_event ev[2] = {};
    ev[0].type = EV_KEY; ev[0].code = (unsigned short)code; ev[0].value = value;
    ev[1].type = EV_SYN; ev[1].code = SYN_REPORT;
    write_all(ev, 2);
}

void UinputDevice::rel_frame(int dx, int dy) {
    struct input_event ev[3] = {};
    size_t n = 0;
    if (dx) { ev[n].type = EV_REL; ev[n].code = REL_X; ev[n].value = dx; ++n; }
    if (dy) { ev[n].type = EV_REL; ev[n].code = REL_Y; ev[n].value = dy; ++n; }
    if (n == 0) return;
    ev[n].type = EV_SYN; ev[n].code = SYN_REPORT; ++n;
    write_all(ev, n);
}

void UinputDevice::wheel_frame(int amount) {
    struct input_event ev[2] = {};
    ev[0].type = EV_REL; ev[0].code = REL_WHEEL; ev[0].value = amount;
    ev[1].type = EV_SYN; ev[1].code = SYN_REPORT;
    write_all(ev, 2);
}

void UinputDevice::frame(const struct input_event* events, size_t n, bool add_syn) {
    if (n == 0) return;
    if (!add_syn) { write_all(events, n); return; }
    struct input_event buf[64];
    if (n + 1 > 64) { // pathological (a real frame is a handful of events) -- split, never truncate
        write_all(events, n);
        struct input_event syn = {};
        syn.type = EV_SYN; syn.code = SYN_REPORT;
        write_all(&syn, 1);
        return;
    }
    memcpy(buf, events, n * sizeof(*events));
    memset(&buf[n], 0, sizeof(buf[n]));
    buf[n].type = EV_SYN; buf[n].code = SYN_REPORT;
    write_all(buf, n + 1);
}

} // namespace puppetry
