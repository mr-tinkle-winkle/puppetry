// Differential test for the native interpreter (Session 13): every program
// below runs through BOTH backends -- embedded CPython (python_on) and the
// native interpreter (python_off) -- and each one reports what it computed
// by calling a probe "macro" that records its (stringified) arguments.
// The two logs must match exactly, so the native path can't quietly drift
// from Python semantics. A second table checks that clearly-unsupported
// code is rejected at COMPILE time (so Save catches it), and a third runs
// the new wait primitives against simulated key events.
#include <atomic>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>
#include "dispatch.hpp"
#include "keycodes.hpp"
#include "macro.hpp"
#include "native_vm.hpp"
#include "primitives.hpp"
#include "python_embed.hpp"

using namespace puppetry;

static int test_count = 0, failures = 0;
#define CHECK(cond, what) do { \
    ++test_count; \
    if (!(cond)) { ++failures; std::fprintf(stderr, "FAILED: %s (line %d)\n", std::string(what).c_str(), __LINE__); } \
} while (0)

struct Probe : CompiledMacro {
    std::vector<std::string> log;
    void run(Runtime&, MacroRegistry&, const std::vector<std::string>& args) override {
        std::string line;
        for (size_t i = 0; i < args.size(); ++i) { if (i) line += "|"; line += args[i]; }
        log.push_back(line);
    }
};

static std::string join(const std::vector<std::string>& v) {
    std::string o;
    for (const auto& s : v) o += s + "\n";
    return o;
}

struct Case { const char* name; const char* code; };

static const Case kCases[] = {
    {"arith", "Probe(1 + 2, 7 - 10, 3 * 4, 7 / 2, 7 // 2, -7 // 2, 7 % 3, -7 % 3, 2 ** 10, 2 ** -1, 1.5 + 2)\n"},
    {"float repr", "Probe(0.1, 0.1 + 0.2, 1e-7, 2.0, 1/3, 100.0 * 3, 1e16, 1.5e-5, 123456.789, -0.0005, 2.5e20)\n"},
    {"strings", "s = 'ab' + \"cd\"\nProbe(s, s * 2, len(s), s[0], s[-1], 'b' in s, 'z' not in s, str(12) + 'x')\n"},
    {"bool logic", "Probe(True and 3, 0 or 'x', not 0, not [1], 1 < 2 < 3, 3 > 2 > 5, 1 == 1.0, True == 1)\n"},
    {"compare ops", "x = 5\nProbe(x == 5, x != 5, x > 4, x < 4, x >= 5, x <= 4, x is None, x is not None)\n"},
    {"vars + aug", "x = 1\nx += 4\nx *= 3\nx -= 1\nx //= 2\ny = x\ny /= 4\nProbe(x, y)\n"},
    {"if/elif/else", "for v in [1, 2, 3, 4]:\n    if v == 1:\n        Probe('one')\n    elif v == 2 or v == 3:\n        Probe('two/three', v)\n    else:\n        Probe('else', v)\n"},
    {"while/break/continue", "i = 0\nwhile True:\n    i += 1\n    if i % 2 == 0:\n        continue\n    if i > 7:\n        break\n    Probe(i)\n"},
    {"for range forms", "for i in range(3):\n    Probe(i)\nfor i in range(2, 8, 3):\n    Probe(i)\nfor i in range(5, 0, -2):\n    Probe(i)\n"},
    {"for list/str/tuple unpack", "for a, b in [(1, 2), (3, 4)]:\n    Probe(a + b)\nfor ch in 'hi':\n    Probe(ch)\n"},
    {"nested loops", "t = 0\nfor i in range(4):\n    for j in range(4):\n        if j > i:\n            break\n        t += j\nProbe(t)\n"},
    {"lists", "l = [1, 2, 3]\nl2 = l + [4]\nProbe(l, l2, len(l2), l2[-1], 3 in l, [0] * 3, list('ab'), ())\n"},
    {"tuple unpack", "a, b = 1, 2\na, b = b, a\nProbe(a, b)\n(c, d) = [5, 6]\nProbe(c * d)\n"},
    {"functions", "def add(a, b=10):\n    return a + b\ndef fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)\nProbe(add(1), add(1, 2), add(b=3, a=4), fact(6))\n"},
    {"function reads + nonlocal", "count = 0\ndef bump(k):\n    nonlocal count\n    count += k\ndef peek():\n    return count * 2\nbump(3)\nbump(4)\nProbe(count, peek())\n"},
    {"early return", "def f(x):\n    for i in range(10):\n        if i == x:\n            return i * 100\n    return -1\nProbe(f(3), f(99))\n"},
    {"top-level return", "Probe('a')\nif True:\n    return\nProbe('never')\n"},
    {"builtins", "Probe(int('42'), int(3.9), int(-3.9), float('2.5'), str(1.0), bool(''), abs(-4), abs(-2.5), round(2.567, 2), round(3.5), min(4, 2, 8), max([1, 9, 3]), len([]))\n"},
    {"ternary", "for v in range(3):\n    Probe('big' if v > 1 else 'small')\n"},
    {"key names", "Probe(KEY_A, BTN_LEFT, KEY_A == 30)\n"},
    {"pass + comments", "# comment\nif True:\n    pass  # nothing\n\nProbe('ok')\n"},
    {"single-line suite", "if 1 < 2: Probe('inline')\nfor i in range(2): Probe(i)\n"},
    {"multiline call + triple string", "Probe(\n    1,\n    2,\n)\nProbe('''a\nb''')\n"},
    {"none", "x = None\nProbe(x, x is None, x == None)\n"},
    {"string escapes", "Probe('a\\tb', \"q\\\"q\", r'r\\n')\n"},
    {"args default", "arguments(n=3, key=KEY_B)\nfor i in range(n):\n    Probe(i, key)\n"},
    {"primitives", "kd(KEY_A)\nku(KEY_A)\ntap(KEY_B, time_=0)\ncombo(KEY_LEFTCTRL, KEY_C, time_=0)\nwait(0)\nspeed(2)\ncheckpoint()\nProbe('done')\n"},
    {"primitive with expression args", "k = KEY_A\nt = 0.0\nfor i in range(2):\n    tap(k, time_=t * i)\nProbe(len(getButtonsHeld()))\n"},
};

static const Case kCompileErrors[] = {
    {"unknown name", "Probe(nope)\n"},
    {"break outside loop", "break\n"},
    {"import", "import os\n"},
    {"attribute", "x = 'a'.upper()\n"},
    {"bad indent", "if True:\nProbe(1)\n"},
    {"calling a variable", "x = 1\nx()\n"},
    {"unknown kwarg", "tap(KEY_A, time=1)\n"},
    {"f-string", "x = f'{1}'\n"},
    {"nested def", "def a():\n    def b():\n        pass\n"},
};

int main() {
    Runtime rt;
    python_embed_init(rt);
    MacroRegistry registry;
    auto probe = std::make_shared<Probe>();
    registry.set("Probe", probe);

    for (const auto& c : kCases) {
        json def = {{"id", "t"}, {"name", "T"}, {"code", c.code}};
        std::string native_log, py_log, native_err, py_err;
        try {
            auto m = compile_native_macro(def, registry);
            probe->log.clear();
            m->run(rt, registry, {});
            native_log = join(probe->log);
        } catch (const std::exception& e) { native_err = e.what(); }
        try {
            auto m = compile_python_macro(def, registry, {"Probe"});
            probe->log.clear();
            m->run(rt, registry, {});
            py_log = join(probe->log);
        } catch (const std::exception& e) { py_err = e.what(); }
        bool ok = native_err.empty() && py_err.empty() && native_log == py_log && !native_log.empty();
        if (!ok) {
            std::fprintf(stderr, "--- %s\nnative%s:\n%s\npython%s:\n%s\n", c.name,
                         native_err.empty() ? "" : (" ERROR " + native_err).c_str(), native_log.c_str(),
                         py_err.empty() ? "" : (" ERROR " + py_err).c_str(), py_log.c_str());
        }
        CHECK(ok, std::string("native matches Python: ") + c.name);
    }
    CHECK(rt.synth_held.empty(), "nothing left held after the primitive cases");

    for (const auto& c : kCompileErrors) {
        json def = {{"id", "t"}, {"name", "T"}, {"code", c.code}};
        bool threw = false;
        try { compile_native_macro(def, registry); } catch (const MacroCompileError&) { threw = true; }
        CHECK(threw, std::string("rejected at compile time: ") + c.name);
    }

    // Runtime errors carry the line number.
    {
        json def = {{"id", "t"}, {"name", "T"}, {"code", "x = 1\ny = x / 0\n"}};
        auto m = compile_native_macro(def, registry);
        std::string msg;
        try { m->run(rt, registry, {}); } catch (const std::exception& e) { msg = e.what(); }
        CHECK(msg.find("line 2") != std::string::npos && msg.find("ZeroDivision") != std::string::npos,
              "runtime errors name the line: " + msg);
    }

    // A long loop is abortable (check_abort every iteration).
    {
        json def = {{"id", "t"}, {"name", "T"}, {"code", "while True:\n    pass\n"}};
        auto m = compile_native_macro(def, registry);
        std::thread stopper([&] {
            std::this_thread::sleep_for(std::chrono::milliseconds(30));
            rt.abort_flag.store(true);
        });
        bool aborted = false;
        try { m->run(rt, registry, {}); } catch (const MacroAborted&) { aborted = true; }
        stopper.join();
        rt.abort_flag.store(false);
        CHECK(aborted, "while True is abortable");
    }

    // range() doesn't build a list: a huge range starts instantly.
    {
        json def = {{"id", "t"}, {"name", "T"}, {"code", "for i in range(10000000000):\n    if i == 3:\n        break\nProbe(i)\n"}};
        probe->log.clear();
        compile_native_macro(def, registry)->run(rt, registry, {});
        CHECK(probe->log.size() == 1 && probe->log[0] == "3", "lazy range");
    }

    // String arguments are converted to their default's type, on both paths.
    for (int backend = 0; backend < 2; ++backend) {
        json def = {{"id", "a"}, {"name", "A"}, {"code", "arguments(n=1, f=1.5, on=True, key=KEY_A, s='x')\nProbe(n + 1, f * 2, on, key == KEY_B, s)\n"}};
        auto m = backend ? compile_python_macro(def, registry, {"Probe"}) : compile_native_macro(def, registry);
        probe->log.clear();
        m->run(rt, registry, {"4", "2", "False", "KEY_B", "hello"});
        CHECK(probe->log.size() == 1 && probe->log[0] == "5|4.0|False|True|hello",
              std::string("string args take their default's type: ") + (probe->log.empty() ? "" : probe->log[0]));
    }

    // ---- waitForPress: woken by the dispatch loop's notify ----
    for (int backend = 0; backend < 2; ++backend) {
        json def = {{"id", "w"}, {"name", "W"}, {"code", "waitForPress(KEY_F7)\nProbe('pressed')\n"}};
        auto m = backend ? compile_python_macro(def, registry, {"Probe"}) : compile_native_macro(def, registry);
        probe->log.clear();
        std::atomic<bool> done{false};
        std::thread t([&] { m->run(rt, registry, {}); done = true; });
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        CHECK(!done.load(), "waitForPress blocks until the press");
        rt.notify_press(KEY_F6); // wrong key
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        CHECK(!done.load(), "waitForPress ignores other keys");
        rt.notify_press(KEY_F7);
        t.join();
        CHECK(done.load() && probe->log.size() == 1, std::string("waitForPress wakes on its key: ") + (backend ? "python" : "native"));
    }

    // waitForPress() with no button (or "any"/"all") returns whichever key was pressed.
    for (int backend = 0; backend < 4; ++backend) {
        const char* forms[] = {"b = waitForPress()\n", "b = waitForPress('any')\n", "b = waitForPress(\"all\", repress=True)\n",
                               "b = waitForPress(None)\n"};
        std::string code = std::string(forms[backend]) + "tap(b, time_=0)\nProbe(b, b == KEY_G)\n";
        json def = {{"id", "w"}, {"name", "W"}, {"code", code}};
        for (int py = 0; py < 2; ++py) {
            auto m = py ? compile_python_macro(def, registry, {"Probe"}) : compile_native_macro(def, registry);
            probe->log.clear();
            std::thread t([&] { m->run(rt, registry, {}); });
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            int any_during;
            { std::lock_guard<std::mutex> l(rt.ignore_mutex); any_during = rt.repress_any; }
            rt.notify_press(KEY_G);
            t.join();
            int any_after;
            { std::lock_guard<std::mutex> l(rt.ignore_mutex); any_after = rt.repress_any; }
            bool ok = probe->log.size() == 1 && probe->log[0] == std::to_string(KEY_G) + "|True" &&
                      any_during == (backend == 2 ? 1 : 0) && any_after == 0;
            CHECK(ok, std::string("waitForPress(any) returns the key: ") + forms[backend] + (py ? " python" : " native"));
        }
    }

    // repress=True: the code is in repress_codes while waiting, gone after.
    {
        json def = {{"id", "w"}, {"name", "W"}, {"code", "waitForPress(KEY_F8, repress=True)\n"}};
        auto m = compile_native_macro(def, registry);
        std::thread t([&] { m->run(rt, registry, {}); });
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        size_t during;
        { std::lock_guard<std::mutex> l(rt.ignore_mutex); during = rt.repress_codes.count(KEY_F8); }
        rt.notify_press(KEY_F8);
        t.join();
        size_t after;
        { std::lock_guard<std::mutex> l(rt.ignore_mutex); after = rt.repress_codes.count(KEY_F8); }
        CHECK(during == 1 && after == 0, "repress codes held only while waiting");
    }

    // ---- waitForReactivation: the next trigger wakes it instead of re-running ----
    {
        Macro mac;
        mac.name = "R";
        mac.combo = {KEY_F9};
        json def = {{"id", "r"}, {"name", "R"}, {"code", "Probe('first')\nwaitForReactivation(repress=True)\nProbe('second')\n"}};
        mac.func = compile_native_macro(def, registry);
        probe->log.clear();
        std::atomic<bool> done{false};
        std::thread t([&] {
            set_current_macro(&mac);
            mac.func->run(rt, registry, {});
            set_current_macro(nullptr);
            done = true;
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        CHECK(!done.load() && probe->log.size() == 1, "waitForReactivation blocks");
        size_t repressed;
        { std::lock_guard<std::mutex> l(rt.ignore_mutex); repressed = rt.repress_codes.count(KEY_F9); }
        CHECK(repressed == 1, "combo keys repressed while waiting");
        trigger_macro(rt, registry, mac); // what the dispatch loop does on the combo
        t.join();
        CHECK(done.load() && probe->log.size() == 2 && probe->log[1] == "second", "trigger reactivates instead of re-running");
        CHECK(mac.runtime->reactivation_waiters.load() == 0, "waiter count back to zero");
    }

    // aborting a wait unwinds cleanly
    {
        json def = {{"id", "w"}, {"name", "W"}, {"code", "waitForPress(KEY_F10, repress=True)\n"}};
        auto m = compile_native_macro(def, registry);
        bool aborted = false;
        std::thread t([&] { try { m->run(rt, registry, {}); } catch (const MacroAborted&) { aborted = true; } });
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        rt.abort_flag.store(true);
        t.join();
        rt.abort_flag.store(false);
        size_t left;
        { std::lock_guard<std::mutex> l(rt.ignore_mutex); left = rt.repress_codes.size(); }
        size_t waiters;
        { std::lock_guard<std::mutex> l(rt.press_waiters_mutex); waiters = rt.press_waiters.size(); }
        CHECK(aborted && left == 0 && waiters == 0, "abort during waitForPress cleans up");
    }

    // getButtonsHeld reflects the real held set (both backends agree)
    {
        { std::lock_guard<std::mutex> l(rt.held_mutex); rt.held = {KEY_A, BTN_LEFT}; }
        json def = {{"id", "h"}, {"name", "H"}, {"code", "h = getButtonsHeld()\nProbe(h, KEY_A in h, KEY_B in h)\n"}};
        probe->log.clear();
        compile_native_macro(def, registry)->run(rt, registry, {});
        compile_python_macro(def, registry, {"Probe"})->run(rt, registry, {});
        CHECK(probe->log.size() == 2 && probe->log[0] == probe->log[1] && probe->log[0].find("True|False") != std::string::npos,
              "getButtonsHeld: " + (probe->log.empty() ? std::string() : probe->log[0]));
        { std::lock_guard<std::mutex> l(rt.held_mutex); rt.held.clear(); }
    }

    // MousePosition: a tuple with .x/.y; getMousePosition.x works without the
    // call; move_mouse(pos) accepts a saved position.
    {
        rt.ui_mouse.open_sink("/dev/null");
        rt.ui_keyboard.open_sink("/dev/null");
        rt.cursor.store(40, 50, std::chrono::steady_clock::now());
        json def = {{"id", "p"}, {"name", "P"}, {"code",
            "p = getMousePosition()\nProbe(p.x, p.y, p[0], p, getMousePosition.x, getMousePosition.y)\n"
            "a, b = p\nProbe(a + b)\nmove_mouse(p, time_=0, move_to=False)\nmove_mouse(p[0], p[1], time_=0)\n"}};
        probe->log.clear();
        compile_native_macro(def, registry)->run(rt, registry, {});
        rt.cursor.store(40, 50, std::chrono::steady_clock::now()); // the relative move dropped the cache
        compile_python_macro(def, registry, {"Probe"})->run(rt, registry, {});
        CHECK(probe->log.size() == 4 && probe->log[0] == "40|50|40|(40, 50)|40|50" && probe->log[0] == probe->log[2] &&
              probe->log[1] == "90" && probe->log[3] == "90",
              "MousePosition .x/.y: " + (probe->log.empty() ? std::string() : probe->log[0]));
        json bad = {{"id", "p"}, {"name", "P"}, {"code", "p = (1, 2)\nProbe(p.x)\n"}};
        std::string msg;
        try { compile_native_macro(bad, registry)->run(rt, registry, {}); } catch (const std::exception& e) { msg = e.what(); }
        CHECK(msg.find("AttributeError") != std::string::npos, ".x on a plain tuple is an AttributeError");
    }

    // getMousePosition uses the cursor cache when it's fresh (no kdotool here)
    {
        rt.cursor.store(123, 456, std::chrono::steady_clock::now());
        json def = {{"id", "p"}, {"name", "P"}, {"code", "x, y = getMousePosition()\nProbe(x, y, getMousePosition())\n"}};
        probe->log.clear();
        compile_native_macro(def, registry)->run(rt, registry, {});
        compile_python_macro(def, registry, {"Probe"})->run(rt, registry, {});
        CHECK(probe->log.size() == 2 && probe->log[0] == "123|456|(123, 456)" && probe->log[1] == probe->log[0],
              "getMousePosition: " + (probe->log.empty() ? std::string() : probe->log[0]));
    }

    registry.clear();
    probe.reset();
    python_embed_shutdown();
    std::printf("%d/%d checks passed.\n", test_count - failures, test_count);
    return failures ? 1 : 0;
}
