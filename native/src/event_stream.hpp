#pragma once
// Live input event stream: a Unix socket that broadcasts every real input
// event the daemon reads (and, marked separately, every event a macro
// writes), for the overlay helper (puppetry-overlay: OBS pages + the
// layered replay-buffer file).
//
// Cost when nobody is connected: one relaxed atomic load per event.
//
// Wire format -- one line per event, space separated:
//     <src> <time_us> <type> <a> <b>
//   src     r = real device, m = macro output
//   time_us CLOCK_REALTIME microseconds (the kernel's own event timestamp
//           for real input; clock_gettime at write time for macro output)
//   type    k = key/button   a = code, b = 1 down / 0 up
//           m = motion       a = dx, b = dy (one line per hardware frame)
//           w = wheel        a = vertical notches, b = horizontal notches
//           a = controller axis  a = ABS_* code, b = value x 10000, normalized
//                            (-10000..10000 sticks and d-pad, 0..10000 triggers)
// Controller buttons arrive as ordinary k lines (BTN_SOUTH etc.).
// On connect the client first receives
//     h <time_us> <code> <code> ...
// listing every real key/button held at that moment, then an `a` line for
// every controller axis that isn't at rest.
//
// A client that stops reading loses lines (sends are non-blocking); it is
// never allowed to stall an input thread.
#include <atomic>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace puppetry {

struct Runtime;

class EventStream {
public:
    ~EventStream();
    // Binds `path` (replacing a stale socket) and accepts on a background
    // thread. Returns false (and logs) on failure -- the daemon keeps running.
    bool start(const std::string& path, Runtime* rt);
    void stop();

    bool active() const { return clients_.load(std::memory_order_relaxed) > 0; }
    void publish(char src, long long time_us, char type, int a, int b);

private:
    void accept_loop();
    void send_all(const char* buf, size_t n);

    Runtime* rt_ = nullptr;
    int listen_fd_ = -1;
    std::string path_;
    std::thread thread_;
    std::atomic<bool> running_{false};
    std::atomic<int> clients_{0};
    std::mutex mutex_;
    std::vector<int> fds_;
};

// The daemon's one stream (nullptr in tests/tools that never start it).
extern std::atomic<EventStream*> g_event_stream;

long long realtime_us();

// Macro output hook, called from UinputDevice's emit functions.
inline void stream_output(char type, int a, int b) {
    EventStream* s = g_event_stream.load(std::memory_order_relaxed);
    if (s && s->active()) s->publish('m', realtime_us(), type, a, b);
}

} // namespace puppetry
