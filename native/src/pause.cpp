#include "pause.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <stdexcept>
#include <thread>

#include "config.hpp"
#include "focus.hpp"
#include "keycodes.hpp"
#include "primitives.hpp"

namespace puppetry {

namespace {

std::string lower(std::string s) {
    for (char& c : s) c = (char)std::tolower((unsigned char)c);
    return s;
}

std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

bool contains(const std::vector<int>& v, int x) { return std::find(v.begin(), v.end(), x) != v.end(); }

std::mutex g_status_mutex;
const std::vector<std::unique_ptr<Macro>>* g_macros = nullptr;

} // namespace

std::vector<PauseCondition> parse_pause_conditions(const json& macro_def) {
    std::vector<PauseCondition> out;
    auto it = macro_def.find("pause_conditions");
    if (it == macro_def.end() || !it->is_array()) return out;
    for (const auto& e : *it) {
        if (!e.is_object()) continue;
        PauseCondition c;
        c.toggle = json_str(e, "mode", "while") == "toggle";
        std::string kind = json_str(e, "kind", "keys");
        if (kind == "app") {
            c.kind = PauseCondition::Kind::App;
            c.match = json_str(e, "match", "class") == "title" ? "title" : "class";
            c.value = lower(trim(json_str(e, "value", "")));
            c.focused = json_str(e, "state", "unfocused") == "focused";
            if (c.value.empty()) continue;
        } else {
            c.kind = PauseCondition::Kind::Keys;
            auto keys = e.find("keys");
            if (keys == e.end() || !keys->is_array()) continue;
            for (const auto& k : *keys) {
                int code;
                if (k.is_string() && resolve_key_name(k.get<std::string>(), code) && !contains(c.keys, code))
                    c.keys.push_back(code);
            }
            if (c.keys.empty()) continue;
        }
        out.push_back(std::move(c));
    }
    return out;
}

void write_pause_status(const std::vector<std::unique_ptr<Macro>>& macros) {
    std::lock_guard<std::mutex> lock(g_status_mutex);
    json ids = json::array();
    for (const auto& m : macros)
        if (m->runtime->paused.load()) ids.push_back(m->id);
    std::error_code ec;
    fs::create_directories(runtime_dir(), ec);
    fs::path p = runtime_dir() / "paused_macros.json";
    fs::path tmp = p;
    tmp += ".tmp";
    {
        std::ofstream f(tmp);
        if (!f) return;
        f << json{{"paused", ids}}.dump() << "\n";
    }
    fs::rename(tmp, p, ec);
}

void refresh_paused(Runtime& rt, Macro& m) {
    auto rs = m.runtime;
    bool now;
    MappingSnapshot before;
    {
        std::lock_guard<std::mutex> lock(rs->pause_mutex);
        now = rs->toggle_paused;
        for (const auto& c : m.pause_conditions)
            if (!c.toggle && c.holding) now = true;
        if (now == rs->paused.load()) return;
        before = held_mapping_snapshot(rt);
        long long mx = rt.motion_x.load(), my = rt.motion_y.load();
        if (now) {
            rs->paused_at_x = mx;
            rs->paused_at_y = my;
        } else {
            rs->motion_off_x.fetch_add(mx - rs->paused_at_x);
            rs->motion_off_y.fetch_add(my - rs->paused_at_y);
        }
        rs->paused.store(now);
    }
    // Held keys under this macro's actAs mappings / remap profiles switch over
    // right away; its intercepts in progress end.
    if (now) {
        std::vector<std::string> owned;
        {
            std::lock_guard<std::mutex> lock(rt.act_as_mutex);
            for (const auto& p : rt.remap_profiles) if (p.owner_paused == &rs->paused) owned.push_back(p.name);
        }
        end_intercepts_if(rt, [&](const ActiveIntercept& in) {
            return std::find(owned.begin(), owned.end(), in.profile) != owned.end();
        });
    }
    transition_snapshots(rt, before, held_mapping_snapshot(rt));

    std::printf("Macro '%s' %s\n", m.name.c_str(), now ? "paused" : "resumed");
    std::fflush(stdout);
    if (g_macros) write_pause_status(*g_macros);
}

void pause_on_key(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros, int code, int value,
                  const std::vector<int>& held) {
    if (value == 2) return;
    for (auto& mp : macros) {
        Macro& m = *mp;
        if (!m.enabled) continue;
        bool changed = false;
        {
            std::lock_guard<std::mutex> lock(m.runtime->pause_mutex);
            for (auto& c : m.pause_conditions) {
                if (c.kind != PauseCondition::Kind::Keys || !contains(c.keys, code)) continue;
                bool all = true;
                for (int k : c.keys) if (!contains(held, k)) { all = false; break; }
                if (value == 1 && all && !c.holding) {
                    c.holding = true;
                    if (c.toggle) m.runtime->toggle_paused = !m.runtime->toggle_paused;
                    changed = true;
                } else if (value == 0 && c.holding) {
                    c.holding = false;
                    changed = true;
                }
            }
        }
        if (changed) refresh_paused(rt, m);
    }
}

void pause_on_focus(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros, bool ok,
                    const std::string& window_class, const std::string& title) {
    const std::string cls = lower(window_class), ttl = lower(title);
    for (auto& mp : macros) {
        Macro& m = *mp;
        bool changed = false;
        {
            std::lock_guard<std::mutex> lock(m.runtime->pause_mutex);
            for (auto& c : m.pause_conditions) {
                if (c.kind != PauseCondition::Kind::App) continue;
                bool is_focused = (c.match == "title" ? ttl : cls).find(c.value) != std::string::npos;
                bool holds = ok && (c.focused ? is_focused : !is_focused);
                if (holds == c.holding) continue;
                c.holding = holds;
                if (holds && c.toggle) m.runtime->toggle_paused = !m.runtime->toggle_paused;
                changed = true;
            }
        }
        if (changed) refresh_paused(rt, m);
    }
}

void pause_when(Runtime& rt, Macro& m, PauseCondition c) {
    c.runtime = true;
    c.value = lower(trim(c.value));
    if (c.kind == PauseCondition::Kind::Keys && c.keys.empty()) throw std::invalid_argument("pauseWhen(): no keys given");
    if (c.kind == PauseCondition::Kind::App && c.value.empty()) throw std::invalid_argument("pauseWhen(): no app given");
    // where it stands right now (a toggle rule only reacts to the NEXT time it happens)
    if (c.kind == PauseCondition::Kind::Keys) {
        std::lock_guard<std::mutex> lock(rt.held_mutex);
        c.holding = true;
        for (int k : c.keys) if (!rt.held.count(k)) { c.holding = false; break; }
    } else {
        FocusInfo f = focused_window(0.2, c.match == "title");
        bool is_focused = icontains(c.match == "title" ? f.title : f.cls, c.value);
        c.holding = f.ok && (c.focused ? is_focused : !is_focused);
    }
    {
        std::lock_guard<std::mutex> lock(m.runtime->pause_mutex);
        for (const auto& have : m.pause_conditions)
            if (have.same_rule(c)) return;
        m.pause_conditions.push_back(std::move(c));
    }
    refresh_paused(rt, m);
}

void clear_runtime_pause_conditions(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros) {
    for (auto& mp : macros) {
        bool changed = false;
        {
            std::lock_guard<std::mutex> lock(mp->runtime->pause_mutex);
            auto& v = mp->pause_conditions;
            size_t before = v.size();
            v.erase(std::remove_if(v.begin(), v.end(), [](const PauseCondition& c) { return c.runtime; }), v.end());
            changed = v.size() != before;
        }
        if (changed) refresh_paused(rt, *mp);
    }
}

void run_pause_focus_watch(Runtime& rt, std::vector<std::unique_ptr<Macro>>& macros) {
    g_macros = &macros;
    write_pause_status(macros);
    bool warned = false;
    int epoch = rt.abort_epoch.load();
    while (true) {
        int now_epoch = rt.abort_epoch.load();
        if (now_epoch != epoch) {            // the abort key: pauseWhen() rules go
            epoch = now_epoch;
            clear_runtime_pause_conditions(rt, macros);
        }
        // conditions can appear while running (pauseWhen), so this is looked at every pass
        bool any_app = false, want_title = false;
        for (const auto& m : macros) {
            std::lock_guard<std::mutex> lock(m->runtime->pause_mutex);
            for (const auto& c : m->pause_conditions) {
                if (c.kind != PauseCondition::Kind::App) continue;
                any_app = true;
                if (c.match == "title") want_title = true;
            }
        }
        if (any_app) {
            FocusInfo f = focused_window(0.2, want_title);   // shared with the privacy watcher and the primitives
            if (!f.ok && !warned) {
                std::fprintf(stderr, "Pause conditions: can't read the focused window (kdotool) -- app conditions are off\n");
                warned = true;
            }
            pause_on_focus(rt, macros, f.ok, f.cls, f.title);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
}

} // namespace puppetry
