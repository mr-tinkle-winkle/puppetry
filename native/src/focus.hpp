#pragma once
// The focused window (KWin, through kdotool), shared and cached: the
// privacy watcher, pause conditions and the getFocusedApp()/appFocused()
// primitives all ask here instead of each spawning kdotool on their own.
//
// A query costs one kdotool run for the window id; the class is asked again
// only when the id changed, the title only when a caller wants it (titles
// change under the same id).
#include <string>
#include <vector>

namespace puppetry {

struct FocusInfo {
    bool ok = false;        // false: kdotool missing / not KWin / no answer
    std::string id;         // "" = no window focused
    std::string cls;        // window class (app id), e.g. org.vinegarhq.Sober
    std::string title;      // only current when asked with want_title
};

// The focused window, at most `max_age_s` old.
FocusInfo focused_window(double max_age_s, bool want_title);

// Case-insensitive "is `needle` part of `hay`".
bool icontains(const std::string& hay, const std::string& needle);

// Test hook: replaces kdotool. Called with the argv after "kdotool"; returns
// ok and fills `out`. nullptr restores the real one.
void focus_set_runner_for_tests(bool (*runner)(const std::vector<std::string>&, std::string&));

} // namespace puppetry
