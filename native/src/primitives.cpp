#include "primitives.hpp"
#include <algorithm>
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

namespace puppetry {

static UinputDevice& device_for_code(Runtime& rt, int code) {
    return is_button_code(code) ? rt.ui_mouse : rt.ui_keyboard;
}

void kd(Runtime& rt, int code) {
    device_for_code(rt, code).write_key(code, 1);
    device_for_code(rt, code).syn();
    std::lock_guard<std::mutex> lock(rt.synth_held_mutex);
    rt.synth_held.insert(code);
}

void ku(Runtime& rt, int code) {
    device_for_code(rt, code).write_key(code, 0);
    device_for_code(rt, code).syn();
    std::lock_guard<std::mutex> lock(rt.synth_held_mutex);
    rt.synth_held.erase(code);
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
    rt.ui_mouse.write_rel(REL_WHEEL, amount);
    rt.ui_mouse.syn();
}

void wait_fn(Runtime& rt, double time_, bool precise) {
    time_ = time_ * Runtime::speed_multiplier();
    if (!precise) {
        double remaining = time_;
        const double chunk = 0.03;
        while (remaining > 0) {
            rt.check_abort();
            double this_chunk = remaining > chunk ? chunk : remaining;
            std::this_thread::sleep_for(std::chrono::duration<double>(this_chunk));
            remaining -= this_chunk;
        }
        rt.check_abort();
        return;
    }
    auto start = std::chrono::steady_clock::now();
    while (std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count() < time_) {
        rt.check_abort();
    }
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
            bool is_button = is_button_code(code);
            if ((kind == "keyboard" && !is_button) || (kind == "mouse" && is_button)) {
                stuck.push_back(code);
            }
        }
        for (int code : stuck) rt.held.erase(code);
    }
    UinputDevice& out = (kind == "keyboard") ? rt.ui_keyboard : rt.ui_mouse;
    for (int code : stuck) {
        out.write_key(code, 0);
        out.syn();
    }
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
    if (dx) rt.ui_mouse.write_rel(REL_X, dx);
    if (dy) rt.ui_mouse.write_rel(REL_Y, dy);
    rt.ui_mouse.syn();
}

static double ease(double t, const std::string& style) {
    if (style == "linear") return t;
    if (style == "in") return t * t;
    if (style == "out") return 1 - (1 - t) * (1 - t);
    if (t < 0.5) return 2 * t * t;
    return 1 - std::pow(-2 * t + 2, 2) / 2;
}

// Asks KWin for the cursor position via kdotool, same approach as the
// Python version's _get_cursor_pos_kde(). Returns false (leaving x/y
// untouched) if kdotool isn't installed, times out, or its output can't
// be parsed -- callers fall back to the corner-anchored move, same as
// the original.
static bool get_cursor_pos_kde(int& x, int& y, double timeout_s = 1.0) {
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
        execlp("kdotool", "kdotool", "getmouselocation", "--shell", (char*)nullptr);
        _exit(127);
    }
    close(out_pipe[1]);

    struct pollfd pfd{out_pipe[0], POLLIN, 0};
    std::string output;
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
    if (timed_out || !WIFEXITED(status) || WEXITSTATUS(status) != 0) return false;

    std::istringstream iss(output);
    std::string line;
    bool have_x = false, have_y = false;
    while (std::getline(iss, line)) {
        if (line.rfind("X=", 0) == 0) { x = std::atoi(line.c_str() + 2); have_x = true; }
        else if (line.rfind("Y=", 0) == 0) { y = std::atoi(line.c_str() + 2); have_y = true; }
    }
    return have_x && have_y;
}

void move_mouse_fn(Runtime& rt, int x_pixels, int y_pixels, double time_,
                    const std::string& easing, bool move_to) {
    double scaled_time = time_ * Runtime::speed_multiplier();
    int dx = x_pixels, dy = y_pixels;
    bool had_position = false;

    if (move_to) {
        int cx, cy;
        if (get_cursor_pos_kde(cx, cy)) {
            had_position = true;
            dx = x_pixels - cx;
            dy = y_pixels - cy;
        } else {
            move_rel_step(rt, -100000, -100000);
        }
    }

    if (easing == "none" || scaled_time <= 0) {
        move_rel_step(rt, dx, dy);
    } else {
        int steps = std::max(1, (int)(scaled_time * 120));
        double prev = 0.0;
        for (int i = 1; i <= steps; ++i) {
            rt.check_abort();
            double t = (double)i / steps;
            double cur = ease(t, easing);
            move_rel_step(rt, (int)std::lround(dx * (cur - prev)), (int)std::lround(dy * (cur - prev)));
            prev = cur;
            std::this_thread::sleep_for(std::chrono::duration<double>(scaled_time / steps));
        }
    }

    if (move_to && had_position) {
        for (int i = 0; i < 3; ++i) {
            int cx, cy;
            if (!get_cursor_pos_kde(cx, cy)) break;
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
    pid_t pid = fork();
    if (pid < 0) return;
    if (pid == 0) {
        // Detach fully: new session, stdin from /dev/null, stdout/stderr
        // inherited from the daemon (so failures show up in
        // `journalctl --user -u macro-daemon`, per the original's
        // rationale) -- never DEVNULL'd.
        setsid();
        int devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) { dup2(devnull, STDIN_FILENO); close(devnull); }

        std::string path = std::getenv("PATH") ? std::getenv("PATH") : "";
        const char* user = std::getenv("USER");
        std::string extra = "/run/current-system/sw/bin:";
        if (user && *user) extra += std::string("/etc/profiles/per-user/") + user + "/bin:";
        const char* home = std::getenv("HOME");
        if (home) extra += std::string(home) + "/.nix-profile/bin:";
        setenv("PATH", (extra + path).c_str(), 1);

        execl("/bin/sh", "sh", "-c", cmd.c_str(), (char*)nullptr);
        _exit(127);
    }
    // Fire-and-forget: don't wait, don't check the exit code. A
    // background reaper thread (see daemon.cpp) periodically waitpid()s
    // with WNOHANG so these don't accumulate as zombies.
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
        int shift_code = key_name_to_code().at("KEY_LEFTSHIFT");
        if (needs_shift) kd(rt, shift_code);
        tap(rt, code, time_per_letter);
        if (needs_shift) ku(rt, shift_code);
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
        std::vector<int> stuck;
        {
            std::lock_guard<std::mutex> lock(rt.synth_held_mutex);
            stuck.assign(rt.synth_held.begin(), rt.synth_held.end());
            rt.synth_held.clear();
        }
        for (int code : stuck) {
            UinputDevice& dev = device_for_code(rt, code);
            dev.write_key(code, 0);
            dev.syn();
        }
        rt.abort_flag.store(false);
    }).detach();
}

} // namespace puppetry
