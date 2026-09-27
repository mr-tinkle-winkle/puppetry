#include "primitives.hpp"
#include <algorithm>
#include <cerrno>
#include <ctime>
#include <chrono>
#include <cctype>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <linux/input.h>
#include <poll.h>
#include <sstream>
#include <stdexcept>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include "keycodes.hpp"

extern char** environ;

namespace puppetry {

using Clock = std::chrono::steady_clock;

static inline UinputDevice& device_for_code(Runtime& rt, int code) {
    return is_mouse_button(code) ? rt.ui_mouse : rt.ui_keyboard;
}

void kd(Runtime& rt, int code) {
    device_for_code(rt, code).key_frame(code, 1);
    rt.synth_held.set(code);
}

void ku(Runtime& rt, int code) {
    device_for_code(rt, code).key_frame(code, 0);
    rt.synth_held.clear(code);
}

void tap(Runtime& rt, int code, double time_) {
    kd(rt, code);
    wait_fn(rt, time_);
    ku(rt, code);
}

void combo_fn(Runtime& rt, const std::vector<int>& keys, double time_) {
    for (int k : keys) kd(rt, k);
    wait_fn(rt, time_);
    for (auto it = keys.rbegin(); it != keys.rend(); ++it) ku(rt, *it);
}

void wheel(Runtime& rt, int amount) {
    rt.ui_mouse.wheel_frame(amount);
}

// ---------------------------------------------------------------------
// Waiting.
//
// Why this isn't just sleep_for(): the kernel's sleep call has a floor
// of tens of microseconds (timer slack + scheduler wakeup) no matter how
// small the request, so every tiny wait() used to cost ~30-75us. That
// floor, paid twice per tap cycle (tap's hold + the loop's wait), is
// exactly what capped the autoclicker at ~16k/s. Now:
//   - waits shorter than a margin are spun on the (vDSO, ~20ns) clock
//     instead of slept -- sleeping could never have been that short;
//   - longer waits sleep to an ABSOLUTE deadline (no drift from
//     re-computing "now"), minus that margin, then spin the rest, so
//     they land within ~1us instead of overshooting by the wakeup
//     latency;
//   - precise=True just uses a bigger margin (1ms), so it also rides
//     out scheduler hiccups on a busy system -- same accuracy goal as
//     the old full busy-wait, a fraction of the CPU.
// main() also drops the daemon's timer slack to 1ns (PR_SET_TIMERSLACK),
// which shrinks the sleep-overshoot the margin has to cover.
// ---------------------------------------------------------------------

static constexpr auto kSpinMargin = std::chrono::microseconds(100);
static constexpr auto kPreciseMargin = std::chrono::milliseconds(1);
static constexpr auto kMaxSleepChunk = std::chrono::milliseconds(30); // abort responsiveness
static constexpr auto kAnchorWindow = std::chrono::milliseconds(2);
// ON THE SIZE OF THE MARGIN -- and why it's a constant rather than
// something that adapts to the machine.
//
// The margin decides how early we stop sleeping and start spinning, so it
// needs to cover however late clock_nanosleep actually wakes us. That
// suggests measuring the real overshoot and growing the margin to match
// (a deep-idle CPU can take a few hundred microseconds to wake), which
// would in theory shrink the late tail: waits land at a median of +0.1us
// but a p99 of ~150us, and that tail is what "playback is slightly
// inconsistent" means.
//
// It was implemented and MEASURED, and it made the tail WORSE -- p99 rose
// from ~120us to ~270us and the no-drift test started failing. The reason
// it backfires is that most of the tail isn't wakeup latency at all, it's
// the scheduler taking the CPU away; spinning earlier can't prevent that,
// and the extra spinning causes more preemption, which lengthens the very
// tail it was trying to cut. Filtering the outliers out of the estimate
// didn't rescue it either.
//
// So: fixed margins, and the lever for genuine multi-millisecond
// preemption is scheduling priority instead (the "realtime_priority"
// option -- see macro.cpp), which addresses the actual cause. Don't
// re-litigate this without a before/after tail measurement; test_timing
// prints one.

static inline void cpu_relax() {
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#endif
}

static void sleep_abs(Clock::time_point tp) {
    // libstdc++'s steady_clock IS CLOCK_MONOTONIC on Linux.
    auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(tp.time_since_epoch()).count();
    struct timespec ts;
    ts.tv_sec = ns / 1000000000LL;
    ts.tv_nsec = ns % 1000000000LL;
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr) == EINTR) {}
}

void wait_until(Runtime& rt, Clock::time_point target, bool precise) {
    const auto margin = precise ? Clock::duration(kPreciseMargin) : Clock::duration(kSpinMargin);
    while (true) {
        rt.check_abort();
        auto now = Clock::now();
        if (now >= target) return;
        if (target - now <= margin) {
            while (Clock::now() < target) {
                rt.check_abort();
                cpu_relax();
            }
            return;
        }
        sleep_abs(std::min(target - margin, now + Clock::duration(kMaxSleepChunk)));
    }
}

void wait_fn(Runtime& rt, double time_, bool precise) {
    double d = time_ * Runtime::speed_multiplier();
    auto now = Clock::now();
    auto dur = (d > 0) ? std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(d))
                       : Clock::duration::zero();

    // Timeline anchoring: if the previous wait() on this thread ended
    // very recently, measure this wait from THAT deadline instead of
    // from now, so the few microseconds each line in between costs
    // (a uinput write, the Python call) don't pile up as drift -- e.g.
    // a transcribed recording replays on its original timeline instead
    // of slowly falling behind. Lag is never carried forward: if the
    // overhead already exceeded this wait, it just doesn't wait, and
    // the timeline re-bases on "now".
    auto& anchor = Runtime::wait_anchor();
    Clock::time_point base = now;
    if (anchor && now >= *anchor && now - *anchor <= kAnchorWindow) base = *anchor;
    Clock::time_point target = base + dur;
    if (target <= now) {
        anchor = now;
        rt.check_abort();
        return;
    }
    anchor = target;
    wait_until(rt, target, precise);
}

bool wait_is_short(double time_) {
    double d = time_ * Runtime::speed_multiplier();
    return d < 0.0002;
}

void speed_fn(Runtime& rt, double multiplier) {
    (void)rt;
    Runtime::speed_multiplier() = multiplier;
}

static void apply_grab_state(Runtime& rt); // fwd decl, defined below

static void release_held_from(Runtime& rt, const std::string& kind) {
    std::vector<int> stuck;
    {
        std::lock_guard<std::mutex> lock(rt.held_mutex);
        for (int code : rt.held) {
            bool is_button = is_mouse_button(code);
            if ((kind == "keyboard" && !is_button) || (kind == "mouse" && is_button)) {
                stuck.push_back(code);
            }
        }
        for (int code : stuck) rt.held.erase(code);
    }
    UinputDevice& out = (kind == "keyboard") ? rt.ui_keyboard : rt.ui_mouse;
    for (int code : stuck) out.key_frame(code, 0);
}

static void apply_grab_state(Runtime& rt) {
    bool kb_needed, mouse_needed;
    {
        std::lock_guard<std::mutex> lock(rt.ignore_mutex);
        bool any_kb_key_ignored = false, any_mouse_key_ignored = false;
        for (int code : rt.ignored_keys) {
            (is_button_code(code) ? any_mouse_key_ignored : any_kb_key_ignored) = true;
        }
        kb_needed = rt.ignore_keyboard || any_kb_key_ignored;
        mouse_needed = rt.ignore_mouse_buttons || rt.ignore_mouse_movement || any_mouse_key_ignored;
    }
    {
        std::lock_guard<std::mutex> lock(rt.act_as_mutex);
        for (const auto& [pressing_code, mapping] : rt.act_as_active) {
            if (!mapping.ignore_original) continue;
            (is_button_code(pressing_code) ? mouse_needed : kb_needed) = true;
        }
    }

    std::lock_guard<std::mutex> lock(rt.grab_mutex);
    if (rt.watched_keyboard) {
        try {
            if (kb_needed && !rt.keyboard_grabbed) {
                rt.watched_keyboard->grab();
                rt.keyboard_grabbed = true;
                release_held_from(rt, "keyboard");
            } else if (!kb_needed && rt.keyboard_grabbed) {
                rt.watched_keyboard->ungrab();
                rt.keyboard_grabbed = false;
            }
        } catch (const std::exception&) {
            // Best-effort, same as the Python version -- never let a
            // grab/ungrab failure crash the daemon.
        }
    }
    if (rt.watched_mouse) {
        try {
            if (mouse_needed && !rt.mouse_grabbed) {
                rt.watched_mouse->grab();
                rt.mouse_grabbed = true;
                release_held_from(rt, "mouse");
            } else if (!mouse_needed && rt.mouse_grabbed) {
                rt.watched_mouse->ungrab();
                rt.mouse_grabbed = false;
            }
        } catch (const std::exception&) {
        }
    }
}

void ignore_fn(Runtime& rt, const std::string& what) {
    if (what == "mouse") {
        {
            std::lock_guard<std::mutex> lock(rt.ignore_mutex);
            rt.ignore_mouse_buttons = !rt.ignore_mouse_buttons;
            rt.ignore_mouse_movement = !rt.ignore_mouse_movement;
        }
        apply_grab_state(rt);
        return;
    }
    {
        std::lock_guard<std::mutex> lock(rt.ignore_mutex);
        if (what == "keyboard") rt.ignore_keyboard = !rt.ignore_keyboard;
        else if (what == "mouse_buttons") rt.ignore_mouse_buttons = !rt.ignore_mouse_buttons;
        else if (what == "mouse_movement") rt.ignore_mouse_movement = !rt.ignore_mouse_movement;
        else throw std::invalid_argument(
            "ignore: unknown target '" + what + "' -- expected 'keyboard', "
            "'mouse', 'mouse_buttons', or 'mouse_movement'");
    }
    apply_grab_state(rt);
}

void ignore_keys_fn(Runtime& rt, const std::vector<int>& codes) {
    {
        std::lock_guard<std::mutex> lock(rt.ignore_mutex);
        for (int code : codes) {
            auto it = rt.ignored_keys.find(code);
            if (it == rt.ignored_keys.end()) rt.ignored_keys.insert(code);
            else rt.ignored_keys.erase(it);
        }
    }
    apply_grab_state(rt);
}

void act_as_fn(Runtime& rt, int key_pressing, bool ignore_original, const std::vector<int>& acting_keys) {
    ActAsMapping candidate{ignore_original, acting_keys};

    bool currently_held;
    {
        std::lock_guard<std::mutex> lock(rt.held_mutex);
        currently_held = rt.held.count(key_pressing) > 0;
    }

    {
        // Scoped so act_as_mutex is released BEFORE apply_grab_state()
        // below, which takes the same (non-recursive) mutex itself --
        // holding it across that call would deadlock.
        std::lock_guard<std::mutex> lock(rt.act_as_mutex);

        // "the current standing intention" for this key: whatever's
        // already queued for next-press, else whatever's active now.
        std::optional<ActAsMapping> intended;
        auto pending_it = rt.act_as_pending.find(key_pressing);
        if (pending_it != rt.act_as_pending.end()) {
            intended = pending_it->second;
        } else {
            auto active_it = rt.act_as_active.find(key_pressing);
            if (active_it != rt.act_as_active.end()) intended = active_it->second;
        }

        // Toggle-by-repetition: the exact same (key, ignore, acting_keys)
        // triple requested again clears it back to normal instead of
        // reapplying it.
        std::optional<ActAsMapping> new_intent =
            (intended && *intended == candidate) ? std::nullopt : std::optional<ActAsMapping>(candidate);

        if (currently_held) {
            // Held-key transition: finish the current press/release cycle
            // acting as whatever it already is; only take effect starting
            // from the next press.
            rt.act_as_pending[key_pressing] = new_intent;
        } else {
            // Not currently down -- nothing to finish, apply immediately.
            if (new_intent) rt.act_as_active[key_pressing] = *new_intent;
            else rt.act_as_active.erase(key_pressing);
            rt.act_as_pending.erase(key_pressing);
        }
    }

    apply_grab_state(rt);
}

static void move_rel_step(Runtime& rt, int dx, int dy) {
    if (dx == 0 && dy == 0) return; // nothing to emit, and nothing to invalidate
    rt.ui_mouse.rel_frame(dx, dy); // X and Y in ONE frame -> a true diagonal, not two stair-steps
    // We just moved the cursor by an amount the compositor's pointer
    // acceleration gets the final say over, so any cached position is now
    // a guess. move_to's correction pass re-reads and re-caches.
    rt.cursor.invalidate();
}

// Resolved once per move instead of string-compared per step: an eased
// move runs this 120 times a second. Anything unrecognized means "inout",
// exactly as the chain of string compares here used to.
enum class Easing { Linear, In, Out, InOut };

static Easing parse_easing(const std::string& style) {
    if (style == "linear") return Easing::Linear;
    if (style == "in") return Easing::In;
    if (style == "out") return Easing::Out;
    return Easing::InOut;
}

static double ease(double t, Easing style) {
    switch (style) {
        case Easing::Linear: return t;
        case Easing::In: return t * t;
        case Easing::Out: return 1 - (1 - t) * (1 - t);
        case Easing::InOut: break;
    }
    if (t < 0.5) return 2 * t * t;
    double u = -2 * t + 2;
    return 1 - u * u / 2;
}

// Runs `kdotool <args...>`, collecting stdout (stderr discarded) with a
// deadline. False on any failure (not installed, timed out, nonzero
// exit) -- shared by every kdotool-backed query below, all of which fail
// open (treat "couldn't ask" the same as "no" rather than blocking).
static bool run_kdotool(const std::vector<std::string>& args, std::string& output, double timeout_s) {
    // argv is built BEFORE fork(): the daemon is multithreaded, and after
    // fork() only async-signal-safe calls are legal in the child. A
    // std::vector push_back there can block forever on the malloc lock if
    // another thread happened to hold it at fork time -- which would show
    // up as this query mysteriously timing out (and, for move_to, as the
    // cursor going somewhere else entirely). command_fn() below already
    // took this care; this path hadn't.
    std::vector<char*> argv;
    argv.reserve(args.size() + 2);
    argv.push_back(const_cast<char*>("kdotool"));
    for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
    argv.push_back(nullptr);

    int out_pipe[2];
    if (pipe(out_pipe) != 0) return false;
    pid_t pid = fork();
    if (pid < 0) {
        close(out_pipe[0]);
        close(out_pipe[1]);
        return false;
    }
    if (pid == 0) {
        close(out_pipe[0]);
        dup2(out_pipe[1], STDOUT_FILENO);
        close(out_pipe[1]);
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) { dup2(devnull, STDERR_FILENO); close(devnull); }
        execvp("kdotool", argv.data());
        _exit(127);
    }
    close(out_pipe[1]);

    struct pollfd pfd{out_pipe[0], POLLIN, 0};
    output.clear();
    auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(timeout_s);
    bool timed_out = false;
    while (true) {
        auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
        if (remaining <= 0) { timed_out = true; break; }
        int rc = poll(&pfd, 1, (int)remaining);
        if (rc <= 0) { timed_out = true; break; }
        char buf[256];
        ssize_t n = read(out_pipe[0], buf, sizeof(buf));
        if (n <= 0) break;
        output.append(buf, n);
    }
    close(out_pipe[0]);
    if (timed_out) kill(pid, SIGKILL);
    int status = 0;
    waitpid(pid, &status, 0);
    return !timed_out && WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

// Asks KWin for the cursor position via kdotool, same approach as the
// Python version's _get_cursor_pos_kde(). Returns false (leaving x/y
// untouched) if kdotool isn't installed, times out, or its output can't
// be parsed -- callers fall back to the corner-anchored move, same as
// the original.
bool get_cursor_pos_kde(int& x, int& y, double timeout_s) {
    std::string output;
    if (!run_kdotool({"getmouselocation", "--shell"}, output, timeout_s)) return false;

    std::istringstream iss(output);
    std::string line;
    bool have_x = false, have_y = false;
    while (std::getline(iss, line)) {
        if (line.rfind("X=", 0) == 0) { x = std::atoi(line.c_str() + 2); have_x = true; }
        else if (line.rfind("Y=", 0) == 0) { y = std::atoi(line.c_str() + 2); have_y = true; }
    }
    return have_x && have_y;
}

bool active_window_is_puppetry(double timeout_s) {
    std::string id_out;
    if (!run_kdotool({"getactivewindow"}, id_out, timeout_s)) return false;
    std::string id;
    for (char c : id_out) if (!std::isspace((unsigned char)c)) id += c;
    if (id.empty()) return false;

    std::string name_out;
    if (!run_kdotool({"getwindowname", id}, name_out, timeout_s)) return false;
    std::string lower;
    lower.reserve(name_out.size());
    for (char c : name_out) lower += (char)std::tolower((unsigned char)c);
    return lower.find("puppetry") != std::string::npos;
}

// Where the cursor is, from the cache if it can be trusted (see
// CursorCache) and from KWin otherwise -- one kdotool subprocess saved
// per absolute move in a run of them.
static bool cursor_pos_cached(Runtime& rt, int& x, int& y) {
    if (rt.cursor.get(x, y, Clock::now())) return true;
    if (!get_cursor_pos_kde(x, y)) return false;
    rt.cursor.store(x, y, Clock::now());
    return true;
}

void move_mouse_fn(Runtime& rt, int x_pixels, int y_pixels, double time_,
                    const std::string& easing_name, bool move_to) {
    double scaled_time = time_ * Runtime::speed_multiplier();
    const Easing easing = parse_easing(easing_name);
    int dx = x_pixels, dy = y_pixels;
    bool had_position = false;

    const bool instant = easing_name == "none" || scaled_time <= 0;

    if (move_to) {
        int cx, cy;
        if (cursor_pos_cached(rt, cx, cy)) {
            had_position = true;
            dx = x_pixels - cx;
            dy = y_pixels - cy;
            // Already there: nothing to emit and nothing for the
            // correction pass to correct, so skip both (a transcribed
            // recording is full of these -- the mouse sat still for a
            // frame). Only for an instant move: an EASED one is also
            // pacing the macro, and returning early would quietly make it
            // take no time at all.
            if (dx == 0 && dy == 0 && instant) return;
        } else {
            move_rel_step(rt, -100000, -100000);
        }
    }

    if (instant) {
        move_rel_step(rt, dx, dy);
    } else {
        // Paced against absolute per-step deadlines (start + i*step),
        // not "sleep step_time after each write", so write/wakeup costs
        // don't stretch the move. Positions are also rounded from the
        // CUMULATIVE eased target rather than per-step deltas, so
        // rounding error can't accumulate into a short/long move.
        int steps = std::max(1, (int)(scaled_time * 120));
        auto start = Clock::now();
        auto total = std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(scaled_time));
        long sent_x = 0, sent_y = 0;
        for (int i = 1; i <= steps; ++i) {
            rt.check_abort();
            double cur = ease((double)i / steps, easing);
            long want_x = std::lround(dx * cur), want_y = std::lround(dy * cur);
            move_rel_step(rt, (int)(want_x - sent_x), (int)(want_y - sent_y));
            sent_x = want_x;
            sent_y = want_y;
            wait_until(rt, start + total * i / steps, false);
        }
        Runtime::wait_anchor() = start + total; // a following wait() continues this timeline
    }

    if (move_to && had_position) {
        // Close the loop: pointer acceleration decides how far the deltas
        // above actually travelled, so read back and nudge. Each read
        // also re-caches, which is what lets the NEXT absolute move skip
        // its own query -- and a read that comes back exactly on target
        // leaves the cache holding a position we've verified.
        for (int i = 0; i < 3; ++i) {
            int cx, cy;
            if (!get_cursor_pos_kde(cx, cy)) break;
            rt.cursor.store(cx, cy, Clock::now());
            int err_x = x_pixels - cx, err_y = y_pixels - cy;
            if (err_x == 0 && err_y == 0) break;
            move_rel_step(rt, err_x, err_y);
        }
    }
}

std::string format_command(const std::string& cmd, const std::vector<std::string>& args) {
    // Minimal {0}, {1}, ... substitution -- matches the subset of
    // Python's str.format() the original command()/docstring actually
    // uses. Each arg is shell-quoted (wrapped in single quotes, with
    // embedded single quotes escaped as '\'') before substitution, same
    // safety property as Python's shlex.quote().
    std::string result;
    for (size_t i = 0; i < cmd.size(); ++i) {
        if (cmd[i] == '{') {
            size_t close = cmd.find('}', i);
            if (close != std::string::npos) {
                std::string idx_str = cmd.substr(i + 1, close - i - 1);
                try {
                    size_t idx = std::stoul(idx_str);
                    if (idx < args.size()) {
                        const std::string& raw = args[idx];
                        result += "'";
                        for (char c : raw) {
                            if (c == '\'') result += "'\\''";
                            else result += c;
                        }
                        result += "'";
                        i = close;
                        continue;
                    }
                } catch (...) {
                    // Not a plain integer placeholder -- fall through
                    // and copy the literal text.
                }
            }
        }
        result += cmd[i];
    }
    return result;
}

void command_fn(const std::string& cmd) {
    // Everything allocating is prepared BEFORE fork(): after fork() in a
    // multithreaded process only async-signal-safe calls are safe.
    std::string path = std::getenv("PATH") ? std::getenv("PATH") : "";
    std::string extra = "/run/current-system/sw/bin:";
    if (const char* user = std::getenv("USER"); user && *user)
        extra += std::string("/etc/profiles/per-user/") + user + "/bin:";
    if (const char* home = std::getenv("HOME")) extra += std::string(home) + "/.nix-profile/bin:";
    std::string path_var = "PATH=" + extra + path;

    std::vector<std::string> env_strings;
    for (char** e = environ; e && *e; ++e) {
        if (std::strncmp(*e, "PATH=", 5) != 0) env_strings.emplace_back(*e);
    }
    env_strings.push_back(path_var);
    std::vector<char*> envp;
    for (auto& str : env_strings) envp.push_back(str.data());
    envp.push_back(nullptr);

    // Double fork: the intermediate child exits immediately and is
    // reaped right here, so the real command is re-parented to init and
    // never becomes a zombie of the daemon. (The first C++ version's
    // comment promised a reaper thread that didn't exist -- every
    // command() leaked a zombie until the daemon restarted.)
    pid_t pid = fork();
    if (pid < 0) return;
    if (pid == 0) {
        pid_t grandchild = fork();
        if (grandchild != 0) _exit(0);
        setsid();
        int devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) { dup2(devnull, STDIN_FILENO); close(devnull); }
        // stdout/stderr stay inherited -> failures show up in
        // `journalctl --user -u macro-daemon`, same as the original.
        execle("/bin/sh", "sh", "-c", cmd.c_str(), (char*)nullptr, envp.data());
        _exit(127);
    }
    int status;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
}

// Character -> (code, needs_shift), US QWERTY, built once. Mirrors
// _CHAR_TO_KEY exactly, including the off-by-one fix already applied in
// the prior Python session (Shift+1 -> "!", ..., Shift+0 -> ")").
static const std::unordered_map<char, std::pair<int, bool>>& char_to_key() {
    static const std::unordered_map<char, std::pair<int, bool>> table = [] {
        std::unordered_map<char, std::pair<int, bool>> m;
        auto& names = key_name_to_code();
        auto code_of = [&](const std::string& n) -> int {
            auto it = names.find(n);
            return it == names.end() ? -1 : it->second;
        };
        for (char c = 'a'; c <= 'z'; ++c) {
            std::string upper = std::string("KEY_") + (char)std::toupper(c);
            int code = code_of(upper);
            if (code < 0) continue;
            m[c] = {code, false};
            m[(char)std::toupper(c)] = {code, true};
        }
        static const char* shift_digit_symbols = "!@#$%^&*()";
        for (int i = 0; i < 10; ++i) {
            int digit_code = code_of("KEY_" + std::to_string(i));
            if (digit_code >= 0) m[('0' + i)] = {digit_code, false};
            int shifted_digit = (i + 1) % 10;
            int shifted_code = code_of("KEY_" + std::to_string(shifted_digit));
            if (shifted_code >= 0) m[shift_digit_symbols[i]] = {shifted_code, true};
        }
        auto add = [&](char c, const std::string& name, bool shift) {
            int code = code_of(name);
            if (code >= 0) m[c] = {code, shift};
        };
        add(' ', "KEY_SPACE", false); add('\n', "KEY_ENTER", false); add('\t', "KEY_TAB", false);
        add('.', "KEY_DOT", false); add('>', "KEY_DOT", true);
        add(',', "KEY_COMMA", false); add('<', "KEY_COMMA", true);
        add('/', "KEY_SLASH", false); add('?', "KEY_SLASH", true);
        add(';', "KEY_SEMICOLON", false); add(':', "KEY_SEMICOLON", true);
        add('\'', "KEY_APOSTROPHE", false); add('"', "KEY_APOSTROPHE", true);
        add('-', "KEY_MINUS", false); add('_', "KEY_MINUS", true);
        add('=', "KEY_EQUAL", false); add('+', "KEY_EQUAL", true);
        add('[', "KEY_LEFTBRACE", false); add('{', "KEY_LEFTBRACE", true);
        add(']', "KEY_RIGHTBRACE", false); add('}', "KEY_RIGHTBRACE", true);
        add('\\', "KEY_BACKSLASH", false); add('|', "KEY_BACKSLASH", true);
        add('`', "KEY_GRAVE", false); add('~', "KEY_GRAVE", true);
        return m;
    }();
    return table;
}

void type_text_fn(Runtime& rt, const std::string& text, double time_per_letter) {
    for (char ch : text) {
        auto it = char_to_key().find(ch);
        if (it == char_to_key().end()) continue; // unsupported char -- silently skipped
        auto [code, needs_shift] = it->second;
        if (needs_shift) kd(rt, KEY_LEFTSHIFT);
        tap(rt, code, time_per_letter);
        if (needs_shift) ku(rt, KEY_LEFTSHIFT);
    }
}

void abort_all(Runtime& rt) {
    rt.abort_flag.store(true);

    {
        std::lock_guard<std::mutex> lock(rt.ignore_mutex);
        rt.ignore_keyboard = rt.ignore_mouse_buttons = rt.ignore_mouse_movement = false;
        rt.ignored_keys.clear();
    }
    {
        // actAs is fully cleared on abort, per the settled spec.
        std::lock_guard<std::mutex> lock(rt.act_as_mutex);
        rt.act_as_active.clear();
        rt.act_as_pending.clear();
    }
    {
        std::lock_guard<std::mutex> lock(rt.grab_mutex);
        if (rt.keyboard_grabbed && rt.watched_keyboard) {
            try { rt.watched_keyboard->ungrab(); } catch (...) {}
            rt.keyboard_grabbed = false;
        }
        if (rt.mouse_grabbed && rt.watched_mouse) {
            try { rt.watched_mouse->ungrab(); } catch (...) {}
            rt.mouse_grabbed = false;
        }
    }

    std::thread([&rt] {
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        for (int code : rt.synth_held.take_all()) device_for_code(rt, code).key_frame(code, 0);
        rt.abort_flag.store(false);
    }).detach();
}

} // namespace puppetry
