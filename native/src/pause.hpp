#pragma once
// Pause conditions: per-macro rules that pause a macro (macros.json
// "pause_conditions", see PauseCondition in macro.hpp).
//
// While a macro is paused:
//   - its running thread parks at its next primitive / abort check
//     (Runtime::check_abort -> block_while_paused) and carries on from
//     there when it resumes;
//   - its combo starts nothing (a looping toggle macro can still be
//     switched off);
//   - actAs mappings it made act as the plain keys (keys held across the
//     switch are moved over right away, see act_as_transition_held);
//   - getMouseMotion() in it doesn't count what moved meanwhile.
//
// State for the GUI: $XDG_RUNTIME_DIR/puppetry/paused_macros.json
//   {"paused": ["<macro id>", ...]}
#include <memory>
#include <vector>

#include "macro.hpp"

namespace puppetry {

std::vector<PauseCondition> parse_pause_conditions(const json& macro_def);

// A real key/button event (after the held set was updated): runs every
// "keys" condition. `held` is the set of real keys held now.
void pause_on_key(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros, int code, int value,
                  const std::vector<int>& held);

// The focused window changed (or was polled): runs every "app" condition.
// ok=false (kdotool unavailable) counts every app condition as not holding.
void pause_on_focus(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros, bool ok,
                    const std::string& window_class, const std::string& title);

// Thread body: polls the focused window (kdotool, KWin) every 0.25 s while
// any macro has an "app" condition.
void run_pause_focus_watch(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros);

// Recomputes a macro's paused state from its conditions and applies the
// change (motion offset, actAs handover, log, status file).
void refresh_paused(Runtime& rt, Macro& m);

// pauseWhen(): adds a pause condition to a macro while the daemon runs
// (the same rule twice is one rule). It holds from now on -- a "while"
// rule that is already true pauses the macro at once -- until the abort
// key, which drops every rule added this way.
void pause_when(Runtime& rt, Macro& m, PauseCondition c);
// Drops the pauseWhen() rules of every macro (after an abort).
void clear_runtime_pause_conditions(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros);

void write_pause_status(const std::vector<std::unique_ptr<Macro>>& macros);

} // namespace puppetry
