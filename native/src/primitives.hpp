#pragma once
// Macro-callable primitives -- mirrors macro_daemon.py's PRIMITIVES
// section. Shared, unmodified logic behind BOTH execution paths: a
// "python-on" macro calls these through the CPython embedding
// (python_embed.cpp exposes each one as a Python-callable), a
// "python-off" macro calls them directly from the native fast-path
// interpreter (native_vm.cpp). Keeping the actual behavior in one place
// means the two paths can never drift apart on what e.g. tap() does.
#include <chrono>
#include <string>
#include <vector>
#include "runtime.hpp"

namespace puppetry {

void kd(Runtime& rt, int code);
void ku(Runtime& rt, int code);
void tap(Runtime& rt, int code, double time_ = 0.1);
void combo_fn(Runtime& rt, const std::vector<int>& keys, double time_ = 0.1);

void wheel(Runtime& rt, int amount);

// precise=false: chunked time.sleep()-equivalent, checked for abort
// every ~30ms. precise=true: busy-wait against a steady clock, checked
// continuously. Both scaled by Runtime::speed_multiplier().
void wait_fn(Runtime& rt, double time_, bool precise = false);

void speed_fn(Runtime& rt, double multiplier);

// Waits until an absolute steady_clock deadline (spin/sleep hybrid, see
// primitives.cpp). Does not touch the wait() timeline anchor.
void wait_until(Runtime& rt, std::chrono::steady_clock::time_point target, bool precise);

// True if wait(time_) at the current speed() would be short enough that
// releasing Python's GIL around it costs more than it's worth.
bool wait_is_short(double time_);

// Asks KWin for the cursor position via kdotool. false if unavailable.
bool get_cursor_pos_kde(int& x, int& y, double timeout_s = 1.0);

// Asks KWin (via kdotool) whether the active window's title contains
// "puppetry" -- used by the transcriber's "Ignore Puppetry" option. False
// (never suppress) if kdotool isn't installed or the query fails, same
// fail-open philosophy as get_cursor_pos_kde.
bool active_window_is_puppetry(double timeout_s = 1.0);

// what: "keyboard" | "mouse" | "mouse_buttons" | "mouse_movement".
// Throws std::invalid_argument for anything else, same as the Python
// version's ValueError.
void ignore_fn(Runtime& rt, const std::string& what);

// New this session: block specific key/button codes regardless of the
// whole-category ignore_fn() flags above. Function-only (no GUI
// checkbox). Same toggle-per-code semantics as ignore_fn: calling with
// a code already in the ignored set removes it.
void ignore_keys_fn(Runtime& rt, const std::vector<int>& codes);

// actAs(key_pressing, ignore_original, acting_keys) -- see runtime.hpp
// and the project handoff for the full spec (runtime-only, global
// scope, held-key transition, toggle-by-repetition). Applied from the
// dispatch loop's raw key events, not from macro bodies calling kd/ku
// directly -- this function only updates the Runtime's remap tables;
// dispatch.cpp is what actually injects the duplicate presses.
void act_as_fn(Runtime& rt, int key_pressing, bool ignore_original, const std::vector<int>& acting_keys);

// x_pixels/y_pixels: pixel delta (move_to=false) or absolute target
// (move_to=true, resolved against kdotool's cursor-position query, with
// the corner-anchored fallback described in the Python version's
// docstring when that query is unavailable).
void move_mouse_fn(Runtime& rt, int x_pixels, int y_pixels, double time_ = 0.25,
                    const std::string& easing = "inout", bool move_to = false);

// Fire-and-forget shell command via /bin/sh, PATH-augmented for NixOS,
// stdout/stderr inherited (not discarded) -- exact behavior described
// in the Python version's command() docstring. `args` are already
// shell-quoted-and-substituted by the caller (python_embed.cpp /
// native_vm.cpp), matching the .format()-based substitution the
// original used, so this just execs the final string.
void command_fn(const std::string& cmd);

void type_text_fn(Runtime& rt, const std::string& text, double time_per_letter = 0.05);

// Formats cmd with shell-quoted args, .format()-style ({0}, {1}, ...).
std::string format_command(const std::string& cmd, const std::vector<std::string>& args);

// Panic button -- mirrors abort_all(): stops every running macro
// (cooperative, via check_abort()), releases every key/button our own
// virtual devices currently have held down, force-releases any active
// ignore()/grab, and clears every active actAs remap.
void abort_all(Runtime& rt);

} // namespace puppetry
