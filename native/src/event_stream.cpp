#include "event_stream.hpp"
#include "runtime.hpp"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace puppetry {

std::atomic<EventStream*> g_event_stream{nullptr};

long long realtime_us() {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}

EventStream::~EventStream() { stop(); }

bool EventStream::start(const std::string& path, Runtime* rt) {
    rt_ = rt;
    path_ = path;
    ::unlink(path.c_str());
    listen_fd_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listen_fd_ < 0) {
        std::fprintf(stderr, "event stream: socket() failed: %s\n", strerror(errno));
        return false;
    }
    struct sockaddr_un addr = {};
    addr.sun_family = AF_UNIX;
    if (path.size() >= sizeof(addr.sun_path)) {
        std::fprintf(stderr, "event stream: socket path too long\n");
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    if (::bind(listen_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0 || ::listen(listen_fd_, 8) < 0) {
        std::fprintf(stderr, "event stream: bind/listen %s failed: %s\n", path.c_str(), strerror(errno));
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    ::chmod(path.c_str(), 0600); // your keystrokes: owner only
    running_ = true;
    thread_ = std::thread(&EventStream::accept_loop, this);
    return true;
}

void EventStream::stop() {
    if (!running_.exchange(false)) return;
    if (listen_fd_ >= 0) { ::shutdown(listen_fd_, SHUT_RDWR); ::close(listen_fd_); listen_fd_ = -1; }
    if (thread_.joinable()) thread_.join();
    std::lock_guard<std::mutex> lock(mutex_);
    for (int fd : fds_) ::close(fd);
    fds_.clear();
    clients_ = 0;
    ::unlink(path_.c_str());
}

void EventStream::accept_loop() {
    while (running_) {
        struct pollfd p = {listen_fd_, POLLIN, 0};
        int r = ::poll(&p, 1, 500);
        if (r <= 0) continue;
        int fd = ::accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (fd < 0) continue;
        // hello: what's held right now
        std::string hello = "h " + std::to_string(realtime_us());
        const bool paused_now = paused_.load();
        if (rt_ && !paused_now) {
            std::lock_guard<std::mutex> lock(rt_->held_mutex);
            for (int code : rt_->held) hello += " " + std::to_string(code);
        }
        hello += "\n";
        if (paused_now) hello += "s " + std::to_string(realtime_us()) + " p 1 0\n";
        if (rt_ && !paused_now) {                   // controller axes that aren't at rest
            std::lock_guard<std::mutex> lock(rt_->axes_mutex);
            long long now = realtime_us();
            for (auto& [code, v] : rt_->axes)
                if (v != 0.0) hello += "r " + std::to_string(now) + " a " + std::to_string(code) + " " +
                                       std::to_string((int)(v * 10000)) + "\n";
        }
        std::lock_guard<std::mutex> lock(mutex_);
        ssize_t w = ::send(fd, hello.data(), hello.size(), MSG_DONTWAIT | MSG_NOSIGNAL);
        (void)w;
        fds_.push_back(fd);
        clients_ = (int)fds_.size();
    }
}

void EventStream::send_all(const char* buf, size_t n) {
    std::lock_guard<std::mutex> lock(mutex_);
    for (size_t i = 0; i < fds_.size();) {
        ssize_t w = ::send(fds_[i], buf, n, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (w < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
            ::close(fds_[i]);                 // disconnected
            fds_.erase(fds_.begin() + (long)i);
            continue;
        }
        ++i;                                   // EAGAIN: slow reader loses this line
    }
    clients_ = (int)fds_.size();
}

void EventStream::set_paused(bool paused) {
    if (paused_.exchange(paused) == paused) return;
    char buf[64];
    int n = std::snprintf(buf, sizeof(buf), "s %lld p %d 0\n", realtime_us(), paused ? 1 : 0);
    if (n > 0) send_all(buf, (size_t)n);
}

void EventStream::publish(char src, long long time_us, char type, int a, int b) {
    if (paused_.load(std::memory_order_relaxed)) return;    // an ignored app: nothing leaves the daemon
    char buf[96];
    int n = std::snprintf(buf, sizeof(buf), "%c %lld %c %d %d\n", src, time_us, type, a, b);
    if (n > 0) send_all(buf, (size_t)n);
}

} // namespace puppetry
