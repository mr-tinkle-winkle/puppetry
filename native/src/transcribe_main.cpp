// puppetry-transcribe -- the editor's "Start Transcribing" backend.
//
// Streams generated macro code to stdout, one or more complete lines per
// write, until stdin is closed (the GUI closing its end = "Stop") or it
// gets SIGTERM/SIGINT. Status/errors go to stderr. Exit codes: 0 ok,
// 2 couldn't open any requested device, 64 bad arguments.
//
// STDIN also carries commands, one per line:
//   focus 1 / focus 0   Puppetry's own window gained/lost focus.
// Only meaningful with --ignore-puppetry, and it exists because the GUI
// already knows this for free -- Qt tells it the moment its window
// activates. Asking KWin instead means spawning kdotool twice per poll,
// which at a useful polling rate is dozens of subprocesses a second for
// something the caller could just say. The kdotool poll is still there as
// a fallback and switches itself off for good as soon as a focus command
// arrives, so running this by hand from a terminal still works.
//
//   puppetry-transcribe --keyboard PATH --mouse PATH
//       [--transcribe-keyboard] [--transcribe-mouse] [--raw]
//       [--set-positions] [--same-start] [--raw-hz N] [--precise]
//       [--ping-key KEY_NAME] [--abort-key KEY_NAME] [--hotkey-key KEY_NAME]
//       [--restart-key KEY_NAME] [--checkpoint-key KEY_NAME]
//       [--ignore-alt-tab] [--ignore-puppetry]
//
// Device I/O only; all transcription logic is TranscriberCore
// (transcriber.cpp), which is unit-tested with synthetic events.
//
// Exit code 3 means the abort key ended the session (as opposed to the
// GUI closing our stdin, or a normal SIGTERM) -- the GUI checks for this
// specifically so it can report "abort key pressed" instead of a generic
// stop, same distinction it already makes for a disconnected device.
#include <algorithm>
#include <atomic>
#include <csignal>
#include <cstdio>
#include <cerrno>
#include <cstring>
#include <ctime>
#include <memory>
#include <poll.h>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>
#include "evdev_device.hpp"
#include "keycodes.hpp"
#include "primitives.hpp"
#include "transcriber.hpp"

using namespace puppetry;

static volatile sig_atomic_t g_stop = 0;
// static: the detached resync/focus helpers may still be finishing when main() returns.
static std::atomic<bool> resync_busy{false};
static std::atomic<bool> focus_busy{false};
static void on_signal(int) { g_stop = 1; }

// Only used until the GUI reports focus itself (see the stdin commands in
// the header comment). 50ms while it lasts: transcription can afford it,
// and for a standalone run this poll is the whole mechanism.
static constexpr long long kFocusCheckIntervalUs = 50000;

static long long now_monotonic_us() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}

int main(int argc, char** argv) {
    std::string kb_path, mouse_path;
    TranscribeOptions opts;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", a.c_str()); std::exit(64); }
            return argv[++i];
        };
        if (a == "--keyboard") kb_path = next();
        else if (a == "--mouse") mouse_path = next();
        else if (a == "--transcribe-keyboard") opts.keyboard = true;
        else if (a == "--transcribe-mouse") opts.mouse = true;
        else if (a == "--raw") opts.raw = true;
        else if (a == "--set-positions") opts.set_positions = true;
        else if (a == "--same-start") opts.same_start = true;
        else if (a == "--precise") opts.precise = true;
        else if (a == "--raw-hz") opts.raw_hz = std::atof(next().c_str());
        else if (a == "--ping-key") {
            std::string name = next();
            int code;
            if (!resolve_key_name(name, code)) { std::fprintf(stderr, "unknown ping key %s\n", name.c_str()); return 64; }
            opts.ping_code = code;
        } else if (a == "--abort-key") {
            std::string name = next();
            int code;
            if (!resolve_key_name(name, code)) { std::fprintf(stderr, "unknown abort key %s\n", name.c_str()); return 64; }
            opts.abort_code = code;
        } else if (a == "--hotkey-key") {
            std::string name = next();
            int code;
            if (!resolve_key_name(name, code)) { std::fprintf(stderr, "unknown hotkey %s\n", name.c_str()); return 64; }
            opts.hotkey_code = code;
        } else if (a == "--restart-key") {
            std::string name = next();
            int code;
            if (!resolve_key_name(name, code)) { std::fprintf(stderr, "unknown restart key %s\n", name.c_str()); return 64; }
            opts.restart_code = code;
        } else if (a == "--checkpoint-key") {
            std::string name = next();
            int code;
            if (!resolve_key_name(name, code)) { std::fprintf(stderr, "unknown checkpoint key %s\n", name.c_str()); return 64; }
            opts.checkpoint_code = code;
        } else if (a == "--ignore-alt-tab") opts.ignore_alt_tab = true;
        else if (a == "--ignore-puppetry") opts.ignore_puppetry = true;
        else {
            std::fprintf(stderr, "unknown argument %s\n", a.c_str());
            return 64;
        }
    }

    struct Dev { std::unique_ptr<InputDevice> dev; Source role; };
    std::vector<Dev> devs;
    // The keyboard is opened whenever anything is transcribed -- it also
    // carries the ping key. Same device for both roles -> opened once.
    bool want_kb = (opts.keyboard || opts.mouse) && !kb_path.empty();
    bool want_mouse = opts.mouse && !mouse_path.empty();
    if (want_kb && want_mouse && kb_path == mouse_path) {
        devs.push_back({std::make_unique<InputDevice>(), Source::Both});
        if (!devs.back().dev->open(kb_path)) devs.pop_back();
    } else {
        if (want_kb) {
            devs.push_back({std::make_unique<InputDevice>(), Source::Keyboard});
            if (!devs.back().dev->open(kb_path)) devs.pop_back();
        }
        if (want_mouse) {
            devs.push_back({std::make_unique<InputDevice>(), Source::Mouse});
            if (!devs.back().dev->open(mouse_path)) devs.pop_back();
        }
    }
    if (devs.empty()) {
        std::fprintf(stderr, "no_devices\n");
        return 2;
    }
    for (auto& d : devs) d.dev->use_monotonic_clock(); // one shared timeline, never jumps

    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);
    std::signal(SIGPIPE, SIG_IGN);

    std::string out_buf;
    auto emit = [&](const std::string& s) { out_buf += s; };
    auto query = [](int& x, int& y) { return get_cursor_pos_kde(x, y, 0.5); };
    TranscriberCore core(opts, emit, query);
    core.start(now_monotonic_us());

    // set_positions drift-correction queries run on a helper thread so a
    // slow kdotool round-trip never stalls event reading (a stalled
    // reader is exactly how the kernel buffer overflows).
    int resync_pipe[2];
    if (pipe(resync_pipe) != 0) return 1;
    struct ResyncResult { int ok, x, y; long long sx, sy; };

    // "Ignore Puppetry" focus polling -- same detached-thread-plus-pipe
    // shape as the resync helper above, and for the same reason: a
    // kdotool round-trip must never stall reading the real devices.
    int focus_pipe[2];
    if (opts.ignore_puppetry && pipe(focus_pipe) != 0) return 1;
    struct FocusResult { int focused; long long checked_at_us; };
    long long next_focus_check_us = 0;

    std::vector<struct pollfd> pfds;
    pfds.push_back({STDIN_FILENO, POLLIN, 0});
    pfds.push_back({resync_pipe[0], POLLIN, 0});
    pfds.push_back({opts.ignore_puppetry ? focus_pipe[0] : -1, POLLIN, 0}); // fd -1: poll() ignores it
    for (auto& d : devs) pfds.push_back({d.dev->fd(), POLLIN, 0});

    EventReorderQueue queue; // merges the two devices into one ordered stream
    auto feed = [&](Source role, const RawEvent& ev) { core.feed(role, ev); };
    RawEvent buf[64];
    bool abort_hit = false;

    // Set once the GUI has told us about focus; from then on the kdotool
    // poll is dead weight and stays off.
    bool focus_reported = false;
    std::string stdin_buf;
    bool stdin_closed = false;

    while (!g_stop) {
        long long now = now_monotonic_us();
        if (core.wants_resync(now) && !resync_busy.exchange(true)) {
            long long sx, sy;
            core.mark_resync_started(now, sx, sy);
            int wfd = resync_pipe[1];
            std::thread([wfd, sx, sy] {
                ResyncResult r{0, 0, 0, sx, sy};
                r.ok = get_cursor_pos_kde(r.x, r.y, 0.5) ? 1 : 0;
                ssize_t w = write(wfd, &r, sizeof(r));
                (void)w;
                resync_busy = false;
            }).detach();
        }
        if (opts.ignore_puppetry && !focus_reported && now >= next_focus_check_us && !focus_busy.exchange(true)) {
            next_focus_check_us = now + kFocusCheckIntervalUs;
            int wfd = focus_pipe[1];
            std::thread([wfd] {
                FocusResult r{active_window_is_puppetry(0.5) ? 1 : 0, now_monotonic_us()};
                ssize_t w = write(wfd, &r, sizeof(r));
                (void)w;
                focus_busy = false;
            }).detach();
        }

        // Don't sleep past the moment the oldest held-back event is due to
        // be released (see EventReorderQueue) -- otherwise a keystroke
        // could sit in the queue for the full poll timeout before being
        // written out.
        int timeout_ms = 250;
        if (!queue.empty()) {
            long long due_us = queue.oldest_us() + EventReorderQueue::kWindowUs - now;
            timeout_ms = due_us <= 0 ? 0 : (int)std::min<long long>(250, (due_us + 999) / 1000);
        }
        int rc = poll(pfds.data(), pfds.size(), timeout_ms);
        if (rc < 0) { if (errno == EINTR) continue; break; }

        if (pfds[0].revents & (POLLIN | POLLHUP)) {
            char c[256];
            ssize_t n = read(STDIN_FILENO, c, sizeof(c));
            if (n <= 0) { stdin_closed = true; } // GUI closed our stdin: stop
            else {
                stdin_buf.append(c, (size_t)n);
                // Whole lines only; a partial command waits for the rest.
                size_t start = 0, nl;
                while ((nl = stdin_buf.find('\n', start)) != std::string::npos) {
                    std::string line = stdin_buf.substr(start, nl - start);
                    start = nl + 1;
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    if (line == "focus 1" || line == "focus 0") {
                        focus_reported = true; // the poll's job is over
                        if (opts.ignore_puppetry) {
                            core.set_puppetry_focused(line.back() == '1', now_monotonic_us());
                        }
                    }
                    // Anything else is ignored on purpose: an older GUI
                    // talking to a newer helper, or vice versa, should
                    // keep transcribing rather than fall over.
                }
                stdin_buf.erase(0, start);
                if (stdin_buf.size() > 4096) stdin_buf.clear(); // no unbounded growth on garbage
            }
        }
        if (pfds[1].revents & POLLIN) {
            ResyncResult r;
            if (read(resync_pipe[0], &r, sizeof(r)) == (ssize_t)sizeof(r) && r.ok) core.apply_resync(r.x, r.y, r.sx, r.sy);
        }
        if (opts.ignore_puppetry && (pfds[2].revents & POLLIN)) {
            FocusResult r;
            if (read(focus_pipe[0], &r, sizeof(r)) == (ssize_t)sizeof(r)) core.set_puppetry_focused(r.focused != 0, r.checked_at_us);
        }

        // Drain every ready device completely (the fds are non-blocking, so
        // a short read just means empty), queue it all, then release
        // whatever has aged past the reorder window in true timestamp
        // order. Draining fully also keeps a burst from being split across
        // wakeups, which is half of what made the streams interleave badly.
        bool device_gone = false;
        for (size_t i = 3; i < pfds.size(); ++i) {
            if (!(pfds[i].revents & (POLLIN | POLLERR | POLLHUP))) continue;
            for (int round = 0; round < 32; ++round) { // cap: don't starve the rest of the loop
                int n = devs[i - 3].dev->read_events(buf, 64);
                if (n < 0) { device_gone = true; break; }
                for (int k = 0; k < n; ++k) queue.push(devs[i - 3].role, buf[k]);
                if (n < 64) break; // that was everything
            }
        }
        queue.drain_until(now_monotonic_us() - EventReorderQueue::kWindowUs, feed);

        if (!out_buf.empty()) {
            fwrite(out_buf.data(), 1, out_buf.size(), stdout);
            fflush(stdout);
            out_buf.clear();
        }
        if (device_gone) { std::fprintf(stderr, "device disconnected\n"); break; }
        if (core.abort_requested()) { std::fprintf(stderr, "abort_key_pressed\n"); abort_hit = true; break; }
        // Checked last, after this round's events have been fed and
        // written out, so a "stop" can't drop input that was already
        // sitting in the device buffer alongside it.
        if (stdin_closed) break;
    }

    // Whatever is still inside the reorder window belongs in the
    // transcript too -- the last keystroke of a session is usually the one
    // that ended it.
    queue.drain_all(feed);
    if (!out_buf.empty()) {
        fwrite(out_buf.data(), 1, out_buf.size(), stdout);
        fflush(stdout);
        out_buf.clear();
    }
    return abort_hit ? 3 : 0;
}
