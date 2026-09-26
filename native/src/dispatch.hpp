#pragma once
// Combo matching, actAs remap application, and the per-device watch
// loop -- mirrors macro_daemon.py's EVENT LOOP section
// (_handle_key_event/watch_device), plus the new actAs injection logic
// that section never had.
#include <functional>
#include <vector>
#include "evdev_device.hpp"
#include "macro.hpp"
#include "runtime.hpp"

namespace puppetry {

// Pulled out of handle_key_event() so it's independently unit-testable
// without any real device or macro list: given the set of macros and
// the currently-held codes, which macros' combos are satisfied, with
// the superset-suppression rule already applied (a satisfied combo that
// is a strict subset of another currently-satisfied combo is dropped).
std::vector<Macro*> maximal_matching_macros(const std::vector<std::unique_ptr<Macro>>& macros,
                                             int just_pressed_code,
                                             const std::vector<int>& held_codes);

// One key/button event's worth of dispatch: combo matching + trigger,
// "up"-edge arming/firing, "hold" loop stop-on-release, all as one
// atomic step against Runtime::held -- mirrors _handle_key_event().
// `abort_code`: the dedicated panic-button hotkey; never enters combo
// matching at all. `on_trigger` is called for every macro that should
// actually fire (kept as a callback rather than calling trigger_macro()
// directly so unit tests can observe triggers without spinning up real
// macro threads).
void handle_key_event(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros, int abort_code,
                       int code, int value,
                       const std::function<void(Macro&)>& on_trigger);

// actAs application for ONE real key/button event, called from
// watch_device() before/around the ordinary combo-matching dispatch
// above. Returns true if the ORIGINAL real event should still be
// forwarded to the rest of the system (i.e. NOT suppressed by an active
// actAs mapping's ignore_original=true) -- watch_device() combines this
// with the existing per-category/per-key ignore() suppression to decide
// what actually gets forwarded through the virtual device while the
// real device is grabbed. Injects the acting-key duplicate presses
// itself via kd()/ku().
bool apply_act_as(Runtime& rt, int code, int value);

// kind: "keyboard" or "mouse". Watches `dev` forever (or until a read
// error/EOF), running handle_key_event() for every EV_KEY event and
// forwarding real input through the matching virtual device whenever
// the real device is grabbed and this event's code/category isn't
// itself being suppressed. Registers `dev` into rt.watched_keyboard/
// watched_mouse so ignore()'s grab machinery can find it.
void watch_device(Runtime& rt, MacroRegistry& registry, std::vector<std::unique_ptr<Macro>>& macros,
                   InputDevice& dev, const std::string& kind, int abort_code);

} // namespace puppetry
