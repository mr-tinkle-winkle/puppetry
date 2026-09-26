// Standalone logic tests for the pieces that don't need real hardware:
// combo matching + superset suppression, hold/toggle/trigger_edge
// dispatch, actAs's held-key-transition + toggle-by-repetition rules,
// and arguments(...) extraction. No /dev/input, no /dev/uinput, no
// CPython -- just the pure C++ logic in dispatch.cpp/macro.cpp/
// primitives.cpp's actAs table. Run with: ./test_core (see CMakeLists).
#include <cassert>
#include <chrono>
#include <cstdio>
#include <memory>
#include <thread>
#include "dispatch.hpp"
#include "macro.hpp"
#include "primitives.hpp"

using namespace puppetry;

static int test_count = 0;
#define CHECK(cond) do { \
    ++test_count; \
    if (!(cond)) { \
        std::fprintf(stderr, "FAILED: %s (line %d)\n", #cond, __LINE__); \
        std::exit(1); \
    } \
} while (0)

static std::unique_ptr<Macro> make_macro(const std::string& name, std::vector<int> combo,
                                          RepeatMode mode = RepeatMode::None,
                                          TriggerEdge edge = TriggerEdge::Down) {
    auto m = std::make_unique<Macro>();
    m->id = name;
    m->name = name;
    m->enabled = true;
    m->combo = std::move(combo);
    m->repeat_mode = mode;
    m->trigger_edge = edge;
    return m;
}

static void test_superset_suppression() {
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(make_macro("ctrl_f", {29 /*LEFTCTRL*/, 33 /*F*/}));
    macros.push_back(make_macro("ctrl_shift_f", {29, 42 /*LEFTSHIFT*/, 33}));

    // Holding Ctrl+Shift+F: both combos are satisfied, but only the
    // more specific (superset) one should fire.
    std::vector<int> held = {29, 42, 33};
    auto maximal = maximal_matching_macros(macros, 33, held);
    CHECK(maximal.size() == 1);
    CHECK(maximal[0]->name == "ctrl_shift_f");

    // Holding only Ctrl+F: only the plain combo is even satisfied.
    std::vector<int> held2 = {29, 33};
    auto maximal2 = maximal_matching_macros(macros, 33, held2);
    CHECK(maximal2.size() == 1);
    CHECK(maximal2[0]->name == "ctrl_f");
}

static void test_hold_stops_on_release() {
    Runtime rt;
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(make_macro("hold_macro", {30, 31}, RepeatMode::Hold));
    int trigger_count = 0;
    auto on_trigger = [&](Macro& m) { ++trigger_count; m.runtime->active_hold.store(true); };

    handle_key_event(rt, macros, /*abort_code=*/999, 30, 1, on_trigger); // partial press
    CHECK(trigger_count == 0);
    handle_key_event(rt, macros, 999, 31, 1, on_trigger); // combo complete
    CHECK(trigger_count == 1);
    CHECK(macros[0]->runtime->active_hold.load());
    // (macro_is_looping() itself also requires a joinable real macro
    // thread, which this test deliberately never spins up -- it's
    // exercised for real by start_loop()/trigger_macro() instead, not
    // by this pure dispatch-logic test.)

    handle_key_event(rt, macros, 999, 30, 0, on_trigger); // release breaks the combo
    CHECK(macros[0]->runtime->stop_flag.load());
}

static void test_toggle_edge() {
    Runtime rt;
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(make_macro("toggle_macro", {30}, RepeatMode::Toggle));
    int trigger_count = 0;
    auto on_trigger = [&](Macro& m) {
        ++trigger_count;
        if (!m.runtime->active_hold.load()) m.runtime->active_hold.store(true);
        else { m.runtime->active_hold.store(false); m.runtime->stop_flag.store(true); }
    };

    handle_key_event(rt, macros, 999, 30, 1, on_trigger);
    CHECK(trigger_count == 1);
    handle_key_event(rt, macros, 999, 30, 0, on_trigger);
    handle_key_event(rt, macros, 999, 30, 1, on_trigger);
    CHECK(trigger_count == 2); // second full press toggles it back off
}

static void test_trigger_edge_up() {
    Runtime rt;
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(make_macro("up_macro", {30, 31}, RepeatMode::None, TriggerEdge::Up));
    int trigger_count = 0;
    auto on_trigger = [&](Macro&) { ++trigger_count; };

    handle_key_event(rt, macros, 999, 30, 1, on_trigger);
    handle_key_event(rt, macros, 999, 31, 1, on_trigger); // combo completes -- arms, doesn't fire
    CHECK(trigger_count == 0);
    CHECK(macros[0]->runtime->armed_up.load());

    handle_key_event(rt, macros, 999, 30, 0, on_trigger); // one key still held -- not yet
    CHECK(trigger_count == 0);
    handle_key_event(rt, macros, 999, 31, 0, on_trigger); // fully released -- fires now
    CHECK(trigger_count == 1);
}

static void test_abort_hotkey_bypasses_combo() {
    Runtime rt;
    std::vector<std::unique_ptr<Macro>> macros;
    macros.push_back(make_macro("abort_combo_macro", {999})); // deliberately shares the abort code
    int trigger_count = 0;
    auto on_trigger = [&](Macro&) { ++trigger_count; };
    handle_key_event(rt, macros, 999, 999, 1, on_trigger);
    CHECK(trigger_count == 0); // abort hotkey never participates in combo matching
    CHECK(rt.held.empty());    // and is never added to `held` either
    // Pressing the abort code runs abort_all(), which spawns a detached
    // cleanup thread holding a reference to `rt` for ~300ms -- let it
    // finish before this local Runtime is destroyed (see the identical
    // note on test_act_as_abort_clears_everything). In the real daemon
    // Runtime is a process-lifetime singleton, so this is a test-only
    // concern, not a daemon bug -- but worth flagging in the handoff:
    // abort_all()'s cleanup thread assumes its Runtime outlives it.
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
}

static void test_act_as_toggle_by_repetition() {
    Runtime rt;
    // Not currently held -- applies immediately.
    act_as_fn(rt, /*lmb=*/272, /*ignore_original=*/false, {16 /*Q*/, 18 /*E*/});
    CHECK(rt.act_as_active.count(272) == 1);

    // Exact same triple again -- clears it.
    act_as_fn(rt, 272, false, {16, 18});
    CHECK(rt.act_as_active.count(272) == 0);
}

static void test_act_as_held_key_transition() {
    Runtime rt;
    { std::lock_guard<std::mutex> lock(rt.held_mutex); rt.held.insert(272); } // lmb physically down

    act_as_fn(rt, 272, false, {16, 18});
    // Currently held -- must NOT take effect immediately; queued as pending.
    CHECK(rt.act_as_active.count(272) == 0);
    CHECK(rt.act_as_pending.count(272) == 1);

    // Release: apply_act_as() commits the pending change on the "up".
    apply_act_as(rt, 272, 0);
    CHECK(rt.act_as_active.count(272) == 1);
    CHECK(rt.act_as_pending.count(272) == 0);

    // Next press now uses the new mapping.
    { std::lock_guard<std::mutex> lock(rt.held_mutex); rt.held.insert(272); }
    bool forward = apply_act_as(rt, 272, 1);
    CHECK(forward); // ignore_original was false -- original still passes through
}

static void test_act_as_abort_clears_everything() {
    Runtime rt;
    act_as_fn(rt, 272, true, {16});
    CHECK(!rt.act_as_active.empty());
    abort_all(rt);
    CHECK(rt.act_as_active.empty());
    CHECK(rt.act_as_pending.empty());
    // abort_all() spawns a detached cleanup thread that touches `rt`
    // ~300ms later (the held-key release grace period) -- give it a
    // chance to finish before this local Runtime goes out of scope.
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
}

static void test_arguments_extraction() {
    auto extracted = extract_arguments_signature("arguments(hits=3, key=KEY_A)\ntap(key)\n");
    CHECK(extracted.has_arguments_decl);
    CHECK(extracted.params.size() == 2);
    CHECK(extracted.params[0].name == "hits" && extracted.params[0].default_source == "3");
    CHECK(extracted.params[1].name == "key" && extracted.params[1].default_source == "KEY_A");
    CHECK(extracted.body == "tap(key)\n");

    bool threw = false;
    try {
        extract_arguments_signature("arguments(hits)\ntap(KEY_A)\n"); // missing default
    } catch (const MacroCompileError&) { threw = true; }
    CHECK(threw);

    threw = false;
    try {
        extract_arguments_signature("tap(KEY_A)\narguments(hits=3)\n"); // not on line 1
    } catch (const MacroCompileError&) { threw = true; }
    CHECK(threw);
}

int main() {
    test_superset_suppression();
    test_hold_stops_on_release();
    test_toggle_edge();
    test_trigger_edge_up();
    test_abort_hotkey_bypasses_combo();
    test_act_as_toggle_by_repetition();
    test_act_as_held_key_transition();
    test_act_as_abort_clears_everything();
    test_arguments_extraction();
    std::printf("All %d checks passed.\n", test_count);
    return 0;
}
