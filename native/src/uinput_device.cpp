#include "uinput_device.hpp"
#include <cstring>
#include <fcntl.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <stdexcept>
#include <unistd.h>

namespace puppetry {

UinputDevice::~UinputDevice() {
    if (fd_ >= 0) {
        ioctl(fd_, UI_DEV_DESTROY);
        close(fd_);
    }
}

void UinputDevice::create(const std::string& name, const std::vector<int>& key_codes, bool with_rel) {
    fd_ = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd_ < 0) {
        throw std::runtime_error("failed to open /dev/uinput: " + std::string(strerror(errno)));
    }

    if (ioctl(fd_, UI_SET_EVBIT, EV_KEY) < 0) {
        throw std::runtime_error("UI_SET_EVBIT EV_KEY failed: " + std::string(strerror(errno)));
    }
    for (int code : key_codes) {
        if (ioctl(fd_, UI_SET_KEYBIT, code) < 0) {
            // Mirrors the Python daemon's comment: building from real,
            // kernel-valid key codes (not blind reflection) means this
            // should never actually hit EINVAL -- but don't let one bad
            // code take the whole device down if it does.
            continue;
        }
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

static void emit(int fd, int type, int code, int value) {
    struct input_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = type;
    ev.code = code;
    ev.value = value;
    // Best-effort: a full uinput write buffer or a mid-teardown race
    // should never crash the daemon or take a macro's thread down with
    // it -- same "keep going regardless" posture as the Python version
    // wrapping every emission in try/except around dev.write()/syn().
    (void)write(fd, &ev, sizeof(ev));
}

void UinputDevice::write_key(int code, int value) {
    if (fd_ >= 0) emit(fd_, EV_KEY, code, value);
}

void UinputDevice::write_rel(int rel_code, int amount) {
    if (fd_ >= 0) emit(fd_, EV_REL, rel_code, amount);
}

void UinputDevice::syn() {
    if (fd_ >= 0) emit(fd_, EV_SYN, SYN_REPORT, 0);
}

} // namespace puppetry
