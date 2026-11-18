// Session 30 pieces: immediate actAs held-key transitions, pause
// conditions (keys / app focus, toggle / while) and what pausing does
// (parked thread, suspended actAs, getMouseMotion offsets), saved
// variables (file store + both macro backends), getMouseMotion.
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <memory>
#include <string>
#include <thread>
#include <vector>
#include <linux/input.h>
#include <unistd.h>
#include "dispatch.hpp"
#include "focus.hpp"
#include "macro.hpp"
#include "native_vm.hpp"
#include "pause.hpp"
#include "primitives.hpp"
#include "python_embed.hpp"
#include "saved_vars.hpp"

using namespace puppetry;

static int test_count = 0, failures = 0;
#define CHECK(cond, what) do { \
    ++test_count; \
    if (!(cond)) { ++failures; std::fprintf(stderr, "FAILED: %s (line %d)\n", std::string(what).c_str(), __LINE__); } \
} while (0)

static void hold(Runtime& rt, int code, bool down) {
    std::lock_guard<std::mutex> lock(rt.held_mutex);
    if (down) rt.held.insert(code); else rt.held.erase(code);
}

static std::vector<int> held_now(Runtime& rt) {
    std::lock_guard<std::mutex> lock(rt.held_mutex);
    return std::vector<int>(rt.held.begin(), rt.held.end());
}

static void test_immediate_act_as() {
    Runtime rt;
    hold(rt, KEY_W, true);
    // W held, flipped to act as S right now
    act_as_fn(rt, KEY_W, true, {KEY_S}, /*immediate=*/true);
    CHECK(rt.act_as_active.count(KEY_W) == 1 && rt.act_as_pending.empty(), "immediate: active at once, nothing queued");
    CHECK(rt.synth_held.has(KEY_S), "immediate: S pressed while W is held");
    // flipped back (same call toggles off): S released, W itself pressed on the virtual keyboard
    act_as_fn(rt, KEY_W, true, {KEY_S}, true);
    CHECK(rt.act_as_active.count(KEY_W) == 0, "immediate: toggled off");
    CHECK(!rt.synth_held.has(KEY_S), "immediate: S released on the flip back");
    CHECK(rt.synth_held.has(KEY_W) && rt.act_as_synth_originals.count(KEY_W), "immediate: W held as itself");
    // the real release lets go of the virtual W
    hold(rt, KEY_W, false);
    apply_act_as(rt, KEY_W, 0);
    CHECK(!rt.synth_held.has(KEY_W) && rt.act_as_synth_originals.empty(), "real release releases the virtual W");

    // identity mapping (W acts as W), then swapped while held: never lets go of the grab
    Runtime rt2;
    act_as_fn(rt2, KEY_W, true, {KEY_W});
    hold(rt2, KEY_W, true);
    CHECK(!apply_act_as(rt2, KEY_W, 1) && rt2.synth_held.has(KEY_W), "W->W: W out through the virtual keyboard");
    act_as_fn(rt2, KEY_W, true, {KEY_W}, true);      // off
    act_as_fn(rt2, KEY_W, true, {KEY_S}, true);      // W->S
    CHECK(!rt2.synth_held.has(KEY_W) && rt2.synth_held.has(KEY_S), "W->W then W->S while held: W up, S down");
    hold(rt2, KEY_W, false);
    apply_act_as(rt2, KEY_W, 0);
    CHECK(!rt2.synth_held.has(KEY_S) && !rt2.synth_held.has(KEY_W), "release under W->S releases S");

    // not immediate: the old queued behavior is unchanged
    Runtime rt3;
    hold(rt3, KEY_A, true);
    act_as_fn(rt3, KEY_A, true, {KEY_D});
    CHECK(rt3.act_as_pending.count(KEY_A) == 1 && !rt3.synth_held.has(KEY_D), "default actAs still waits for the next press");
}

static std::unique_ptr<Macro> macro_with(json conds) {
    auto m = std::make_unique<Macro>();
    m->id = "m1";
    m->name = "M";
    m->enabled = true;
    m->pause_conditions = parse_pause_conditions(json{{"pause_conditions", conds}});
    return m;
}

static void test_parse() {
    json conds = json::array({
        {{"kind", "keys"}, {"keys", {"KEY_LEFTCTRL", "KEY_P"}}, {"mode", "toggle"}},
        {{"kind", "app"}, {"match", "class"}, {"value", " Sober "}, {"state", "unfocused"}, {"mode", "while"}},
        {{"kind", "keys"}, {"keys", json::array()}},                 // dropped: no keys
        {{"kind", "app"}, {"value", ""}},                            // dropped: no app
        "junk",
    });
    auto m = macro_with(conds);
    CHECK(m->pause_conditions.size() == 2, "parse keeps the two valid conditions");
    CHECK(m->pause_conditions[0].toggle && m->pause_conditions[0].keys.size() == 2, "parse: keys toggle");
    CHECK(!m->pause_conditions[1].toggle && m->pause_conditions[1].value == "sober" && !m->pause_conditions[1].focused,
          "parse: app while, lowercased + trimmed");
}

static void test_key_conditions() {
    Runtime rt;
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(macro_with(json::array({
        {{"kind", "keys"}, {"keys", {"KEY_LEFTCTRL", "KEY_P"}}, {"mode", "toggle"}},
        {{"kind", "keys"}, {"keys", {"KEY_F8"}}, {"mode", "while"}},
    })));
    Macro& m = *macros[0];
    auto press = [&](int c) { hold(rt, c, true); pause_on_key(rt, macros, c, 1, held_now(rt)); };
    auto release = [&](int c) { hold(rt, c, false); pause_on_key(rt, macros, c, 0, held_now(rt)); };

    press(KEY_P);
    CHECK(!m.runtime->paused, "half the combo does nothing");
    release(KEY_P);
    press(KEY_LEFTCTRL); press(KEY_P);
    CHECK(m.runtime->paused, "toggle combo pauses");
    press(KEY_P);  // autorepeat-free re-press without release does nothing new
    release(KEY_P); release(KEY_LEFTCTRL);
    CHECK(m.runtime->paused, "toggle stays paused after release");
    press(KEY_LEFTCTRL); press(KEY_P); release(KEY_P); release(KEY_LEFTCTRL);
    CHECK(!m.runtime->paused, "toggle combo again resumes");

    press(KEY_F8);
    CHECK(m.runtime->paused, "while key: paused while held");
    release(KEY_F8);
    CHECK(!m.runtime->paused, "while key: resumes on release");

    // toggle-paused AND while: stays paused until both are off
    press(KEY_LEFTCTRL); press(KEY_P); release(KEY_P); release(KEY_LEFTCTRL);
    press(KEY_F8); release(KEY_F8);
    CHECK(m.runtime->paused, "while ending doesn't undo a toggle pause");
    press(KEY_LEFTCTRL); press(KEY_P); release(KEY_P); release(KEY_LEFTCTRL);
    CHECK(!m.runtime->paused, "resumed");
}

static void test_focus_conditions() {
    Runtime rt;
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(macro_with(json::array({
        {{"kind", "app"}, {"match", "class"}, {"value", "sober"}, {"state", "unfocused"}, {"mode", "while"}},
    })));
    macros.push_back(macro_with(json::array({
        {{"kind", "app"}, {"match", "title"}, {"value", "Menu"}, {"state", "focused"}, {"mode", "toggle"}},
    })));
    Macro& a = *macros[0];
    Macro& b = *macros[1];
    pause_on_focus(rt, macros, true, "org.vinegarhq.Sober", "Roblox");
    CHECK(!a.runtime->paused && !b.runtime->paused, "game focused: running");
    pause_on_focus(rt, macros, true, "firefox", "Main Menu - Firefox");
    CHECK(a.runtime->paused, "unfocused (while): paused");
    CHECK(b.runtime->paused, "title focused (toggle): flips to paused");
    pause_on_focus(rt, macros, true, "firefox", "Main Menu - Firefox");
    CHECK(b.runtime->paused, "staying focused doesn't flip again");
    pause_on_focus(rt, macros, true, "org.vinegarhq.Sober", "Roblox");
    CHECK(!a.runtime->paused && b.runtime->paused, "back to the game: while resumes, toggle stays");
    pause_on_focus(rt, macros, true, "x", "menu");
    CHECK(!b.runtime->paused, "focused again: toggle flips back");
    pause_on_focus(rt, macros, false, "", "");
    CHECK(!a.runtime->paused, "can't read the focus: never paused by it");
}

static void test_pause_effects() {
    Runtime rt;
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(macro_with(json::array({{{"kind", "keys"}, {"keys", {"KEY_F8"}}, {"mode", "while"}}})));
    Macro& m = *macros[0];

    // actAs made by the macro: suspended while paused, held key handed over both ways
    set_current_macro(&m);
    act_as_fn(rt, KEY_W, true, {KEY_S}, false, current_macro_paused_flag());
    set_current_macro(nullptr);
    hold(rt, KEY_W, true);
    apply_act_as(rt, KEY_W, 1);
    CHECK(rt.synth_held.has(KEY_S), "mapping active: W acts as S");
    hold(rt, KEY_F8, true);
    pause_on_key(rt, macros, KEY_F8, 1, held_now(rt));
    CHECK(m.runtime->paused, "paused");
    CHECK(!rt.synth_held.has(KEY_S) && rt.synth_held.has(KEY_W), "pausing hands the held W back to W");
    CHECK(apply_act_as(rt, KEY_A, 1), "unrelated key unaffected");
    hold(rt, KEY_F8, false);
    pause_on_key(rt, macros, KEY_F8, 0, held_now(rt));
    CHECK(!m.runtime->paused, "resumed");
    CHECK(rt.synth_held.has(KEY_S) && !rt.synth_held.has(KEY_W), "resuming hands the held W back to S");
    hold(rt, KEY_W, false);
    apply_act_as(rt, KEY_W, 0);
    CHECK(!rt.synth_held.has(KEY_S), "release lets go of S");

    // press while paused: plain key
    hold(rt, KEY_F8, true);
    pause_on_key(rt, macros, KEY_F8, 1, held_now(rt));
    hold(rt, KEY_W, true);
    CHECK(apply_act_as(rt, KEY_W, 1) && !rt.synth_held.has(KEY_S), "pressed while paused: forwarded as W");
    hold(rt, KEY_W, false);
    apply_act_as(rt, KEY_W, 0);

    // motion while paused doesn't count
    long long ox, oy, x, y;
    rt.motion_y.fetch_add(500);   // while paused
    hold(rt, KEY_F8, false);
    pause_on_key(rt, macros, KEY_F8, 0, held_now(rt));
    rt.motion_y.fetch_add(40);    // while running
    ox = m.runtime->motion_off_x; oy = m.runtime->motion_off_y;
    get_mouse_motion_fn(rt, ox, oy, x, y);
    CHECK(x == 0 && y == 40, "getMouseMotion leaves out paused motion: " + std::to_string(y));

    // a running macro thread parks while paused, carries on after
    std::atomic<int> step{0};
    std::thread t([&] {
        set_current_macro(&m);
        try {
            rt.check_abort();
            step = 1;
            rt.check_abort();   // parks here while paused
            step = 2;
        } catch (...) { step = -1; }
        set_current_macro(nullptr);
    });
    while (step.load() == 0) std::this_thread::sleep_for(std::chrono::milliseconds(1));
    hold(rt, KEY_F8, true);
    pause_on_key(rt, macros, KEY_F8, 1, held_now(rt));
    // (the thread may already have passed its second check; only assert it didn't fail)
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    hold(rt, KEY_F8, false);
    pause_on_key(rt, macros, KEY_F8, 0, held_now(rt));
    t.join();
    CHECK(step.load() == 2, "thread finished after resume");

    // parked thread really waits
    hold(rt, KEY_F8, true);
    pause_on_key(rt, macros, KEY_F8, 1, held_now(rt));
    std::atomic<bool> done{false};
    std::thread t2([&] {
        set_current_macro(&m);
        try { rt.check_abort(); } catch (...) {}
        done = true;
        set_current_macro(nullptr);
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    CHECK(!done.load(), "parked while paused");
    hold(rt, KEY_F8, false);
    pause_on_key(rt, macros, KEY_F8, 0, held_now(rt));
    t2.join();
    CHECK(done.load(), "unparked on resume");

    // a stop request reaches a parked loop
    hold(rt, KEY_F8, true);
    pause_on_key(rt, macros, KEY_F8, 1, held_now(rt));
    std::atomic<int> how{0};
    std::thread t3([&] {
        set_current_macro(&m);
        try { rt.check_abort(); how = 1; } catch (const MacroAborted&) { how = 2; }
        set_current_macro(nullptr);
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    m.runtime->stop_flag = true;
    t3.join();
    CHECK(how.load() == 2, "stopping a paused loop unwinds it");
    m.runtime->stop_flag = false;
    hold(rt, KEY_F8, false);
    pause_on_key(rt, macros, KEY_F8, 0, held_now(rt));
}

struct Probe : CompiledMacro {
    std::vector<std::string> log;
    void run(Runtime&, MacroRegistry&, const std::vector<std::string>& args) override {
        std::string line;
        for (size_t i = 0; i < args.size(); ++i) { if (i) line += "|"; line += args[i]; }
        log.push_back(line);
    }
};

static void test_saved_vars(Runtime& rt) {
    namespace fs = std::filesystem;
    fs::path dir = fs::temp_directory_path() / ("puppetry_vars_" + std::to_string(::getpid()));
    fs::create_directories(dir);
    fs::path file = dir / "saved_variables.json";
    saved_vars_set_path_for_tests(file.string());

    // store
    saved_var_set("a", "n", 5);
    saved_var_set("b", "n", "other");
    json j;
    CHECK(saved_var_get("a", "n", j) && j == 5, "per-owner value");
    CHECK(saved_var_get("b", "n", j) && j == "other", "same name, other macro");
    CHECK(!saved_var_get("a", "missing", j), "missing name");
    {   // the GUI edits the file: picked up
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        std::ofstream f(file);
        f << R"({"a": {"n": 9, "list": [1, 2.5, "x", true, null]}})";
    }
    CHECK(saved_var_get("a", "n", j) && j == 9, "re-read after an outside edit");
    CHECK(!saved_var_get("b", "n", j), "outside edit removed b");
    CHECK(saved_var_clear("a", "n") && !saved_var_clear("a", "n"), "clear once");

    // both backends round-trip the same values (no current macro -> "_global")
    MacroRegistry registry;
    auto probe = std::make_shared<Probe>();
    registry.set("Probe", probe);
    const char* code =
        "saveVar('up', 1234)\n"
        "saveVar('f', 2.5)\n"
        "saveVar('s', 'hi')\n"
        "saveVar('l', [1, 'two', [3.5, False], None])\n"
        "Probe(loadVar('up'), loadVar('f'), loadVar('s'), loadVar('l'), loadVar('nope', 7), loadVar('nope'))\n"
        "Probe(clearVar('s'), clearVar('s'), loadVar('s', 'gone'))\n"
        "m = getMouseMotion()\n"
        "Probe(m.x, m.y, m[0] == m.x, getMouseMotion.y)\n";
    json def = {{"id", "t"}, {"name", "T"}, {"code", code}};
    rt.motion_x = 3;
    rt.motion_y = -8;
    std::string native_log, py_log;
    try {
        auto m = compile_native_macro(def, registry);
        probe->log.clear();
        m->run(rt, registry, {});
        for (auto& l : probe->log) native_log += l + "\n";
    } catch (const std::exception& e) { native_log = std::string("ERROR ") + e.what(); }
    try {
        auto m = compile_python_macro(def, registry, {"Probe"});
        probe->log.clear();
        m->run(rt, registry, {});
        for (auto& l : probe->log) py_log += l + "\n";
    } catch (const std::exception& e) { py_log = std::string("ERROR ") + e.what(); }
    CHECK(native_log == py_log && native_log.find("1234|2.5|hi|[1, 'two', [3.5, False], None]|7|None") == 0,
          "saveVar/loadVar/getMouseMotion: native == Python\nnative:\n" + native_log + "python:\n" + py_log);
    CHECK(native_log.find("3|-8|True|-8") != std::string::npos, "getMouseMotion values: " + native_log);
    CHECK(saved_var_get("_global", "up", j) && j == 1234, "macro wrote the store");

    // a macro's own set is its id
    Macro owner;
    owner.id = "abc";
    set_current_macro(&owner);
    CHECK(saved_var_owner() == "abc", "owner = current macro id");
    set_current_macro(nullptr);
    CHECK(saved_var_owner() == "_global", "owner off a macro thread");

    // remapProfile on both paths: pairs (both) and a dict (Python)
    {
        const char* native_code =
            "remapProfile('p', [(KEY_W, KEY_S), (KEY_A, [KEY_D, KEY_LEFTSHIFT]), (KEY_Q, None)],"
            " intercept=[((KEY_LEFTCTRL, KEY_C), (KEY_LEFTCTRL, KEY_INSERT)), (KEY_F1, KEY_F2)])\n";
        const char* py_code =
            "remapProfile('p', {KEY_W: KEY_S, KEY_A: (KEY_D, KEY_LEFTSHIFT), 'KEY_Q': None},"
            " intercept={(KEY_LEFTCTRL, KEY_C): (KEY_LEFTCTRL, KEY_INSERT), KEY_F1: KEY_F2})\n";
        auto shape = [&] {
            std::lock_guard<std::mutex> lock(rt.act_as_mutex);
            if (rt.remap_profiles.size() != 1) return std::string("count");
            const auto& p = rt.remap_profiles[0];
            bool ok = p.name == "p" && p.keys.size() == 3 && p.keys.at(KEY_W) == std::vector<int>{KEY_S} &&
                      p.keys.at(KEY_A) == (std::vector<int>{KEY_D, KEY_LEFTSHIFT}) && p.keys.at(KEY_Q).empty() &&
                      p.intercepts.size() == 2 && p.intercepts[1].first == std::vector<int>{KEY_F1};
            return std::string(ok ? "ok" : "bad");
        };
        std::string n_err, p_err, n_shape, p_shape;
        try { compile_native_macro(json{{"id", "t"}, {"name", "T"}, {"code", native_code}}, registry)->run(rt, registry, {}); }
        catch (const std::exception& e) { n_err = e.what(); }
        n_shape = shape();
        remap_profile_fn(rt, "p", {}, {});
        try { compile_python_macro(json{{"id", "t"}, {"name", "T"}, {"code", py_code}}, registry, {})->run(rt, registry, {}); }
        catch (const std::exception& e) { p_err = e.what(); }
        p_shape = shape();
        remap_profile_fn(rt, "p", {}, {});
        CHECK(n_err.empty() && n_shape == "ok", "remapProfile native: " + n_err + n_shape);
        CHECK(p_err.empty() && p_shape == "ok", "remapProfile python: " + p_err + p_shape);
        // (a Python macro's error is printed to the log, not thrown)
        compile_python_macro(json{{"id", "t"}, {"name", "T"}, {"code", "remapProfile('p', 'oops')\n"}}, registry, {})->run(rt, registry, {});
        CHECK(!remap_profile_active(rt, "p"), "bad tables are refused");
    }

    // actAs(..., immediate=True) parses on both paths
    hold(rt, KEY_Q, true);
    json def2 = {{"id", "t"}, {"name", "T"}, {"code", "actAs(KEY_Q, True, KEY_E, immediate=True)\nactAs(KEY_Q, True, KEY_E, immediate=True)\n"}};
    bool ok = true;
    try { compile_native_macro(def2, registry)->run(rt, registry, {}); } catch (...) { ok = false; }
    try { compile_python_macro(def2, registry, {})->run(rt, registry, {}); } catch (...) { ok = false; }
    CHECK(ok && rt.act_as_active.count(KEY_Q) == 0, "actAs immediate kwarg on both paths");
    hold(rt, KEY_Q, false);
    apply_act_as(rt, KEY_Q, 0);

    // the shipped example macro, driven end to end (already calibrated)
    {
        std::ifstream f(std::string(PUPPETRY_SOURCE_DIR) + "/../docs/examples/roblox_upside_down_wasd.py");
        std::string code((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
        // The Nix package builds from native/ alone, where docs/ doesn't exist: skip, don't fail.
        if (code.empty()) std::printf("(example macro not in this source tree -- its end-to-end check is skipped)\n");
    }
    if (std::ifstream(std::string(PUPPETRY_SOURCE_DIR) + "/../docs/examples/roblox_upside_down_wasd.py").good()) {
        std::ifstream f(std::string(PUPPETRY_SOURCE_DIR) + "/../docs/examples/roblox_upside_down_wasd.py");
        std::string code((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
        Macro mac;
        mac.id = "wasd";
        mac.name = "Upside-down WASD";
        mac.func = compile_python_macro(json{{"id", "wasd"}, {"name", mac.name}, {"code", code}}, registry, {});
        saved_var_set("wasd", "up_flip", -800);
        saved_var_set("wasd", "up_max", -1200);
        saved_var_set("wasd", "down_flip", 800);
        saved_var_set("wasd", "down_max", 1200);
        auto run_once = [&] {
            set_current_macro(&mac);
            try { mac.func->run(rt, registry, {}); } catch (...) {}
            set_current_macro(nullptr);
        };
        std::thread t(run_once);
        auto settle = [] { std::this_thread::sleep_for(std::chrono::milliseconds(60)); };
        settle();
        bool ign;
        { std::lock_guard<std::mutex> lock(rt.ignore_mutex); ign = rt.ignored_keys.count(KEY_F7) && rt.ignored_keys.count(KEY_F9); }
        CHECK(ign, "example: LEVEL / CALIBRATE swallowed while running");
        rt.motion_y -= 900;    // looked up past the flip point
        settle();
        CHECK(remap_profile_active(rt, "upside down"), "example: upside down -> swapped");
        rt.motion_y -= 5000;   // way past the top: clamped at -1200
        settle();
        rt.motion_y += 450;    // -750: back the right way up
        settle();
        CHECK(!remap_profile_active(rt, "upside down"), "example: clamped at the top, back up -> normal");
        rt.motion_y += 1650;   // +900: upside down the other way
        settle();
        CHECK(remap_profile_active(rt, "upside down"), "example: looking down past the flip point -> swapped");
        hold(rt, KEY_F7, true);     // LEVEL
        settle();
        hold(rt, KEY_F7, false);
        settle();
        CHECK(!remap_profile_active(rt, "upside down"), "example: LEVEL re-zeroes");
        std::thread second(run_once);  // the combo again: stops it
        second.join();
        t.join();
        { std::lock_guard<std::mutex> lock(rt.ignore_mutex); ign = rt.ignored_keys.empty(); }
        CHECK(ign && !remap_profile_active(rt, "upside down"), "example: stopping cleans up");
    }

    saved_vars_set_path_for_tests("");
    fs::remove_all(dir);
}

static void test_remap_profiles() {
    Runtime rt;
    remap_profile_fn(rt, "base", {{KEY_W, {KEY_W}}, {KEY_S, {KEY_S}}}, {});
    hold(rt, KEY_W, true);
    CHECK(!apply_act_as(rt, KEY_W, 1) && rt.synth_held.has(KEY_W), "profile: W acts as W through us");
    remap_profile_fn(rt, "flip", {{KEY_W, {KEY_S}}, {KEY_S, {KEY_W}}}, {});
    CHECK(rt.synth_held.has(KEY_S) && !rt.synth_held.has(KEY_W), "profile on top: held W switches to S at once");
    CHECK(remap_profile_active(rt, "flip"), "active");
    remap_profile_fn(rt, "flip", {}, {});
    CHECK(!remap_profile_active(rt, "flip") && rt.synth_held.has(KEY_W) && !rt.synth_held.has(KEY_S),
          "profile removed: falls back to the one below");
    hold(rt, KEY_W, false);
    apply_act_as(rt, KEY_W, 0);
    CHECK(!rt.synth_held.has(KEY_W), "released");
    remap_profile_fn(rt, "flip", {{KEY_W, {KEY_S}}}, {});
    remap_profile_fn(rt, "base", {{KEY_W, {KEY_A}}}, {});     // replaced in place: still below "flip"
    hold(rt, KEY_W, true);
    apply_act_as(rt, KEY_W, 1);
    CHECK(rt.synth_held.has(KEY_S) && !rt.synth_held.has(KEY_A), "a replaced profile keeps its layer");
    hold(rt, KEY_W, false);
    apply_act_as(rt, KEY_W, 0);
    remap_profile_fn(rt, "flip", {}, {});
    remap_profile_fn(rt, "base", {{KEY_Q, {}}}, {});
    hold(rt, KEY_Q, true);
    CHECK(!apply_act_as(rt, KEY_Q, 1) && rt.synth_held.empty(), "a key mapped to nothing does nothing");
    hold(rt, KEY_Q, false);
    apply_act_as(rt, KEY_Q, 0);
    remap_profile_fn(rt, "base", {}, {});

    // intercepts
    remap_profile_fn(rt, "ic", {}, {{{KEY_LEFTCTRL, KEY_C}, {KEY_LEFTCTRL, KEY_INSERT}},
                                    {{KEY_LEFTCTRL, KEY_V}, {KEY_LEFTSHIFT, KEY_INSERT}},
                                    {{KEY_F1}, {KEY_LEFTALT, KEY_F4}}});
    auto press = [&](int c) { bool f = apply_intercepts(rt, c, 1); hold(rt, c, true); if (f) kd(rt, c); return f; };
    auto release = [&](int c) { hold(rt, c, false); bool f = apply_intercepts(rt, c, 0); if (f) ku(rt, c); return f; };
    CHECK(press(KEY_C) && rt.synth_held.has(KEY_C), "half a trigger: passes through");
    release(KEY_C);
    press(KEY_LEFTCTRL);
    CHECK(!press(KEY_C), "Ctrl+C intercepted: C swallowed");
    CHECK(rt.synth_held.has(KEY_LEFTCTRL) && rt.synth_held.has(KEY_INSERT) && !rt.synth_held.has(KEY_C),
          "...out comes Ctrl+Insert (Ctrl stays down)");
    CHECK(!apply_intercepts(rt, KEY_C, 2), "its autorepeat is swallowed too");
    CHECK(!release(KEY_C) && !rt.synth_held.has(KEY_INSERT) && rt.synth_held.has(KEY_LEFTCTRL),
          "releasing C ends it, Ctrl still held");
    CHECK(!press(KEY_V), "Ctrl+V intercepted");
    CHECK(!rt.synth_held.has(KEY_LEFTCTRL) && rt.synth_held.has(KEY_LEFTSHIFT) && rt.synth_held.has(KEY_INSERT),
          "...out comes Shift+Insert, Ctrl lifted for it");
    release(KEY_V);
    CHECK(rt.synth_held.has(KEY_LEFTCTRL) && !rt.synth_held.has(KEY_LEFTSHIFT) && !rt.synth_held.has(KEY_INSERT),
          "after: output released, Ctrl (still held) back down");
    press(KEY_V);
    CHECK(release(KEY_LEFTCTRL) && !rt.synth_held.has(KEY_LEFTSHIFT) && !rt.synth_held.has(KEY_LEFTCTRL),
          "letting go of Ctrl first ends it as well");
    CHECK(!release(KEY_V) && rt.active_intercepts.empty(), "...and V's release stays swallowed");
    CHECK(!press(KEY_F1) && rt.synth_held.has(KEY_LEFTALT) && rt.synth_held.has(KEY_F4), "one-key trigger");
    remap_profile_fn(rt, "ic", {}, {});
    CHECK(rt.active_intercepts.empty() && !rt.synth_held.has(KEY_F4), "removing the profile ends its intercept");
    hold(rt, KEY_F1, false);
    CHECK(!apply_intercepts(rt, KEY_F1, 0) && rt.intercept_swallowed.empty(), "F1 never went out: its release stays swallowed, once");

    // owned by a macro: pause suspends it
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(macro_with(json::array({{{"kind", "keys"}, {"keys", {"KEY_F8"}}, {"mode", "while"}}})));
    Macro& m = *macros[0];
    set_current_macro(&m);
    remap_profile_fn(rt, "owned", {{KEY_W, {KEY_S}}}, {{{KEY_F2}, {KEY_F3}}}, current_macro_paused_flag());
    set_current_macro(nullptr);
    hold(rt, KEY_W, true);
    apply_act_as(rt, KEY_W, 1);
    press(KEY_F2);
    CHECK(rt.synth_held.has(KEY_S) && rt.synth_held.has(KEY_F3), "owned profile active");
    hold(rt, KEY_F8, true);
    pause_on_key(rt, macros, KEY_F8, 1, held_now(rt));
    CHECK(rt.synth_held.has(KEY_W) && !rt.synth_held.has(KEY_S) && !rt.synth_held.has(KEY_F3),
          "paused: W is W again, intercept ended");
    CHECK(apply_intercepts(rt, KEY_F2, 1), "paused: intercept doesn't fire");
    hold(rt, KEY_F8, false);
    pause_on_key(rt, macros, KEY_F8, 0, held_now(rt));
    CHECK(rt.synth_held.has(KEY_S) && !rt.synth_held.has(KEY_W), "resumed: W acts as S again");
    abort_all(rt);
    CHECK(rt.remap_profiles.empty() && rt.active_intercepts.empty(), "abort clears profiles");
    std::this_thread::sleep_for(std::chrono::milliseconds(400));
}

static void test_grab_handover() {
    Runtime rt;
    std::vector<std::string> log;
    auto inject = [&](int code, int value) { log.push_back("inject " + std::to_string(code) + "=" + std::to_string(value)); return true; };
    auto grab = [&] { log.push_back("grab"); };
    {   // W ignored, A free: both released on the real device before the grab, put back after
        std::lock_guard<std::mutex> lock(rt.ignore_mutex);
        rt.ignored_keys.insert(KEY_W);
    }
    grab_with_handover(rt, "keyboard", {KEY_W, KEY_A}, inject, grab);
    std::vector<std::string> want = {"inject 17=0", "inject 30=0", "grab", "inject 17=1", "inject 30=1"};
    CHECK(log == want, "handover order: release for everyone, grab, restore for us");
    CHECK(rt.forwarded_down.has(KEY_A) && !rt.forwarded_down.has(KEY_W),
          "a held key that may pass carries on through the virtual device; an ignored one doesn't");
    CHECK(rt.forwarded_down.snapshot() == std::vector<int>{KEY_A}, "snapshot lists the set");

    // the device can't be written: nothing injected, nothing pretended
    Runtime rt2;
    log.clear();
    grab_with_handover(rt2, "keyboard", {KEY_W}, [&](int, int) { return false; }, grab);
    CHECK(log == std::vector<std::string>{"grab"} && rt2.forwarded_down.empty(), "unwritable device: plain grab");

    // the grab fails: the keys are given back, the error passes through
    Runtime rt3;
    log.clear();
    bool threw = false;
    try {
        grab_with_handover(rt3, "keyboard", {KEY_W}, inject, [] { throw std::runtime_error("EBUSY"); });
    } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw && log == (std::vector<std::string>{"inject 17=0", "inject 17=1"}) && rt3.forwarded_down.empty(),
          "failed grab: held keys restored for everyone");

    // GUI pause lapses by itself
    Runtime rt4;
    rt4.set_external_pause(0.05);
    CHECK(rt4.external_paused(), "external pause on");
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    CHECK(!rt4.external_paused(), "external pause lapsed without a RESUME");
}

static std::string g_fake_class = "org.vinegarhq.Sober", g_fake_title = "Roblox";
static std::atomic<int> g_fake_calls{0};
static bool fake_kdotool(const std::vector<std::string>& args, std::string& out) {
    ++g_fake_calls;
    if (args.empty()) return false;
    if (args[0] == "getactivewindow") out = "{window-1}\n";
    else if (args[0] == "getwindowclassname") out = g_fake_class + "\n";
    else if (args[0] == "getwindowname") out = g_fake_title + "\n";
    else return false;
    return true;
}

static void test_new_functions(Runtime& rt) {
    focus_set_runner_for_tests(&fake_kdotool);
    // the focus cache: one id query, the class only once per window
    g_fake_calls = 0;
    FocusInfo a = focused_window(0.0, false);
    FocusInfo b = focused_window(10.0, false);
    CHECK(a.ok && a.cls == "org.vinegarhq.Sober" && b.cls == a.cls && g_fake_calls.load() == 2,
          "focus: id + class asked once, then cached: " + std::to_string(g_fake_calls.load()));
    focused_window(0.0, false);
    CHECK(g_fake_calls.load() == 3, "focus: same window again costs one query, not two");
    CHECK(focused_window(10.0, true).title == "Roblox", "focus: title on request");
    CHECK(icontains("org.vinegarhq.Sober", "SOBER") && !icontains("firefox", "sober"), "icontains");

    MacroRegistry registry;
    auto probe = std::make_shared<Probe>();
    registry.set("Probe", probe);
    hold(rt, KEY_Q, true);
    const char* code =
        "Probe(isHeld(KEY_Q), isHeld(KEY_Z), isHeld('KEY_Q'))\n"
        "Probe(getFocusedApp(), getFocusedTitle(), appFocused('sober'), appFocused(app='ROBLOX'), appFocused('firefox'))\n"
        "r = random()\n"
        "s = random(2, 3)\n"
        "n = randomInt(1, 6)\n"
        "Probe(r >= 0 and r <= 1, s >= 2 and s <= 3, randomInt(5, 5), n >= 1 and n <= 6)\n"
        "t = getTime()\n"
        "wait(0.02)\n"
        "Probe(getTime() - t >= 0.01, getTime() - t < 1)\n"
        "waitForRelease(KEY_Z)\n"
        "waitForRelease(KEY_Z, KEY_X)\n"
        "Probe('released')\n";
    json def = {{"id", "t"}, {"name", "T"}, {"code", code}};
    std::string native_log, py_log;
    try {
        auto m = compile_native_macro(def, registry);
        probe->log.clear();
        m->run(rt, registry, {});
        for (auto& l : probe->log) native_log += l + "\n";
    } catch (const std::exception& e) { native_log = std::string("ERROR ") + e.what(); }
    try {
        auto m = compile_python_macro(def, registry, {"Probe"});
        probe->log.clear();
        m->run(rt, registry, {});
        for (auto& l : probe->log) py_log += l + "\n";
    } catch (const std::exception& e) { py_log = std::string("ERROR ") + e.what(); }
    const std::string want =
        "True|False|True\norg.vinegarhq.Sober|Roblox|True|True|False\nTrue|True|5|True\nTrue|True\nreleased\n";
    CHECK(native_log == want, "new functions, native:\n" + native_log);
    CHECK(py_log == want, "new functions, python:\n" + py_log);

    // waitForRelease really waits
    std::atomic<bool> done{false};
    auto waiter = compile_native_macro(json{{"id", "w"}, {"name", "W"}, {"code", "waitForRelease(KEY_Q)\n"}}, registry);
    std::thread t([&] { try { waiter->run(rt, registry, {}); } catch (...) {} done = true; });
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    CHECK(!done.load(), "waitForRelease blocks while the key is held");
    hold(rt, KEY_Q, false);
    t.join();
    CHECK(done.load(), "...and returns on release");

    // pauseWhen(): pauses the calling macro right away when it already holds, on both paths
    for (int python = 0; python < 2; ++python) {
        std::vector<std::unique_ptr<Macro>> macros;
        macros.push_back(std::make_unique<Macro>());
        Macro& m = *macros[0];
        m.id = "pw";
        m.name = "PW";
        m.enabled = true;
        json d = {{"id", "pw"}, {"name", "PW"}, {"code", "pauseWhen(app='sober', focused=False)\npauseWhen(app='sober', focused=False)\nProbe('ran')\n"}};
        m.func = python ? compile_python_macro(d, registry, {"Probe"}) : compile_native_macro(d, registry);
        g_fake_class = "firefox";
        focus_set_runner_for_tests(&fake_kdotool);
        probe->log.clear();
        std::thread run([&] {
            set_current_macro(&m);
            try { m.func->run(rt, registry, {}); } catch (...) {}
            set_current_macro(nullptr);
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(60));
        const std::string which = python ? "python" : "native";
        CHECK(m.runtime->paused.load() && probe->log.empty(), "pauseWhen (" + which + "): Sober not focused -> paused, parked");
        size_t rules;
        { std::lock_guard<std::mutex> lock(m.runtime->pause_mutex); rules = m.pause_conditions.size(); }
        CHECK(rules == 1, "pauseWhen (" + which + "): the same rule twice is one rule");
        pause_on_focus(rt, macros, true, "org.vinegarhq.Sober", "Roblox");
        run.join();
        CHECK(!m.runtime->paused.load() && probe->log.size() == 1, "pauseWhen (" + which + "): focused again -> carries on");
        pause_on_focus(rt, macros, true, "firefox", "x");
        CHECK(m.runtime->paused.load(), "the rule stays after the macro ended");
        clear_runtime_pause_conditions(rt, macros);
        CHECK(!m.runtime->paused.load() && m.pause_conditions.empty(), "abort drops pauseWhen() rules and resumes");
    }
    g_fake_class = "org.vinegarhq.Sober";

    // stopLoop(): a toggle loop that ends itself
    {
        Macro m;
        m.id = "sl";
        m.name = "SL";
        m.enabled = true;
        m.repeat_mode = RepeatMode::Toggle;
        m.func = compile_native_macro(json{{"id", "sl"}, {"name", "SL"}, {"code", "Probe('pass')\nstopLoop()\n"}}, registry);
        probe->log.clear();
        trigger_macro(rt, registry, m);
        std::this_thread::sleep_for(std::chrono::milliseconds(80));
        CHECK(probe->log.size() == 1 && !macro_is_looping(m), "stopLoop(): one pass, then off");
        std::lock_guard<std::mutex> lock(m.runtime->loop_mutex);
        if (m.runtime->thread.joinable()) m.runtime->thread.join();
    }
    focus_set_runner_for_tests(nullptr);
}

struct SlowBody : CompiledMacro {
    std::atomic<int> running{0}, max_running{0}, passes{0};
    void run(Runtime& rt, MacroRegistry&, const std::vector<std::string>&) override {
        int now = ++running;
        int seen = max_running.load();
        while (now > seen && !max_running.compare_exchange_weak(seen, now)) {}
        for (int i = 0; i < 6; ++i) { rt.check_abort(); std::this_thread::sleep_for(std::chrono::milliseconds(10)); }
        ++passes;
        --running;
    }
};

static void test_loop_restart() {
    // A toggle loop switched off and straight back on while its last pass is
    // still running: exactly one loop afterwards, never two side by side,
    // and switching it off again really stops it.
    Runtime rt;
    MacroRegistry registry;
    Macro m;
    m.id = "loop";
    m.name = "Loop";
    m.enabled = true;
    m.repeat_mode = RepeatMode::Toggle;
    auto body = std::make_shared<SlowBody>();
    m.func = body;
    trigger_macro(rt, registry, m);           // on
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    trigger_macro(rt, registry, m);           // off (mid-pass)
    CHECK(!macro_is_looping(m), "stop request: no longer looping");
    trigger_macro(rt, registry, m);           // on again before that pass ended
    CHECK(macro_is_looping(m), "restarted during the old loop's last pass");
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
    CHECK(body->max_running.load() == 1, "old and new loop never ran side by side: " + std::to_string(body->max_running.load()));
    CHECK(macro_is_looping(m) && body->passes.load() >= 3, "the new loop is running");
    trigger_macro(rt, registry, m);           // off
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    int at_stop = body->passes.load();
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    CHECK(body->passes.load() == at_stop && body->running.load() == 0, "switched off: it stays off");
    {
        std::lock_guard<std::mutex> lock(m.runtime->loop_mutex);
        if (m.runtime->thread.joinable()) m.runtime->thread.join();
    }
}

int main() {
    test_grab_handover();
    test_loop_restart();
    test_immediate_act_as();
    test_parse();
    test_key_conditions();
    test_focus_conditions();
    test_pause_effects();
    test_remap_profiles();
    Runtime rt;
    python_embed_init(rt);
    test_saved_vars(rt);
    test_new_functions(rt);
    python_embed_shutdown();
    std::printf("%d checks, %d failed\n", test_count, failures);
    return failures ? 1 : 0;
}
