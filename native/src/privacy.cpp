#include "privacy.hpp"

#include <cctype>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <mutex>
#include <thread>

#include "event_stream.hpp"
#include "macro.hpp"
#include "primitives.hpp"
#include "runtime.hpp"

namespace puppetry {

static std::string lower(std::string s) {
    for (char& c : s) c = (char)std::tolower((unsigned char)c);
    return s;
}

static std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace((unsigned char)s[a])) ++a;
    while (b > a && std::isspace((unsigned char)s[b - 1])) --b;
    return s.substr(a, b - a);
}

std::vector<IgnoredApp> parse_ignored_apps(const json& overlay) {
    std::vector<IgnoredApp> out;
    if (!overlay.is_object() || !overlay.contains("ignored_apps") || !overlay["ignored_apps"].is_array()) return out;
    for (const auto& e : overlay["ignored_apps"]) {
        if (!e.is_object()) continue;
        IgnoredApp r;
        r.value = lower(trim(json_str(e, "value", "")));
        if (r.value.empty()) continue;
        r.match = json_str(e, "match", "class") == "title" ? "title" : "class";
        r.when_open = json_str(e, "when", "focused") == "open";
        out.push_back(r);
    }
    return out;
}

bool ignored_app_matches(const IgnoredApp& rule, const std::string& window_class, const std::string& title) {
    const std::string hay = lower(rule.match == "title" ? title : window_class);
    return !rule.value.empty() && hay.find(rule.value) != std::string::npos;
}

std::string icase_substring_regex(const std::string& literal) {
    std::string re = ".*";
    for (char c : literal) {
        unsigned char u = (unsigned char)c;
        if (std::isalpha(u)) {
            re += '[';
            re += (char)std::tolower(u);
            re += (char)std::toupper(u);
            re += ']';
        } else if (std::string("\\^$.|?*+()[]{}").find(c) != std::string::npos) {
            re += '\\';
            re += c;
        } else {
            re += c;
        }
    }
    return re + ".*";
}

// ---------------------------------------------------------------------------
static fs::path manual_flag() { return runtime_dir() / "visualizer_blocked"; }
static fs::path status_path() { return runtime_dir() / "privacy.json"; }

static std::mutex g_mutex;
static std::string g_app, g_why;
static bool g_watching = true;
static size_t g_rules = 0;

void privacy_apply(Runtime& rt) {
    bool manual = rt.visualizer_blocked.load();
    bool app = rt.app_blocked.load();
    if (EventStream* es = g_event_stream.load()) es->set_paused(manual || app);
    json st;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        st = {{"blocked", manual || app}, {"manual", manual}, {"app", app ? g_app : ""},
              {"why", manual ? "manual" : (app ? g_why : "")}, {"watching", g_watching}, {"rules", g_rules}};
    }
    std::error_code ec;
    fs::path tmp = status_path();
    tmp += ".tmp";
    {
        std::ofstream f(tmp);
        f << st.dump() << "\n";
    }
    fs::rename(tmp, status_path(), ec);
}

bool privacy_set_manual(Runtime& rt, int mode) {
    bool now = mode < 0 ? !rt.visualizer_blocked.load() : mode == 1;
    rt.visualizer_blocked.store(now);
    std::error_code ec;
    if (now) {
        std::ofstream f(manual_flag());
        f << "1\n";
    } else {
        fs::remove(manual_flag(), ec);
    }
    std::printf("Input visualizer: %s\n", now ? "BLOCKED (manual)" : "unblocked (manual)");
    std::fflush(stdout);
    privacy_apply(rt);
    return now;
}

void privacy_load_manual(Runtime& rt) {
    std::error_code ec;
    rt.visualizer_blocked.store(fs::exists(manual_flag(), ec));
    privacy_apply(rt);
}

static std::string kd(const std::vector<std::string>& args, bool& ok) {
    std::string out;
    ok = run_kdotool(args, out, 1.0);
    return trim(out);
}

void run_privacy_watch(Runtime& rt) {
    std::vector<IgnoredApp> rules;
    fs::file_time_type seen{};
    bool have_seen = false;
    bool last_blocked = false;
    std::string last_app;
    while (true) {
        std::error_code ec;
        auto mt = fs::last_write_time(overlay_config_file(), ec);
        if (!ec && (!have_seen || mt != seen)) {
            seen = mt;
            have_seen = true;
            rules = parse_ignored_apps(load_overlay_config());
            std::lock_guard<std::mutex> lock(g_mutex);
            g_rules = rules.size();
        }
        if (rules.empty()) {
            if (rt.app_blocked.exchange(false) || last_blocked) {
                last_blocked = false;
                privacy_apply(rt);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1000));
            continue;
        }

        bool blocked = false, watching = true;
        std::string app, why;
        bool any_focused = false, any_open_class = false, any_open_title = false;
        for (const auto& r : rules) {
            if (!r.when_open) any_focused = true;
            else if (r.match == "title") any_open_title = true;
            else any_open_class = true;
        }
        // focused: one window's class + title against every rule (an "open" rule
        // matches the focused window too)
        if (any_focused || any_open_class || any_open_title) {
            bool ok;
            std::string id = kd({"getactivewindow"}, ok);
            if (!ok) watching = false;
            if (ok && !id.empty()) {
                bool ok2, ok3;
                std::string cls = kd({"getwindowclassname", id}, ok2);
                std::string title = kd({"getwindowname", id}, ok3);
                for (const auto& r : rules) {
                    if (ignored_app_matches(r, cls, title)) {
                        blocked = true;
                        app = r.value;
                        why = "focused";
                        break;
                    }
                }
            }
        }
        // open anywhere: one search per kind, all of that kind's rules in one regex
        if (!blocked) {
            for (const char* kind : {"class", "title"}) {
                const bool is_title = std::string(kind) == "title";
                if (is_title ? !any_open_title : !any_open_class) continue;
                for (const auto& r : rules) {
                    if (!r.when_open || (r.match == "title") != is_title) continue;
                    bool ok;
                    std::string ids = kd({"search", is_title ? "--name" : "--class", icase_substring_regex(r.value)}, ok);
                    if (!ids.empty()) {
                        blocked = true;
                        app = r.value;
                        why = "open";
                        break;
                    }
                }
                if (blocked) break;
            }
        }
        {
            std::lock_guard<std::mutex> lock(g_mutex);
            g_app = app;
            g_why = why;
            g_watching = watching;
        }
        rt.app_blocked.store(blocked);
        if (blocked != last_blocked || app != last_app) {
            if (blocked != last_blocked)
                std::printf("Input visualizer: %s\n", blocked ? ("blocked -- ignored app " + why + ": " + app).c_str()
                                                              : "unblocked (no ignored app)");
            std::fflush(stdout);
            last_blocked = blocked;
            last_app = app;
            privacy_apply(rt);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
}

} // namespace puppetry
