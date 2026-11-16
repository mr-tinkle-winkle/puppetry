#include "focus.hpp"

#include <algorithm>
#include <chrono>
#include <mutex>

#include "primitives.hpp"

namespace puppetry {

namespace {

std::mutex g_mutex;
FocusInfo g_info;
std::chrono::steady_clock::time_point g_id_at{}, g_title_at{};
bool g_have = false, g_have_title = false;
bool (*g_runner)(const std::vector<std::string>&, std::string&) = nullptr;

std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

bool run(const std::vector<std::string>& args, std::string& out) {
    std::string raw;
    bool ok = g_runner ? g_runner(args, raw) : run_kdotool(args, raw, 1.0);
    out = trim(raw);
    return ok;
}

} // namespace

bool icontains(const std::string& hay, const std::string& needle) {
    if (needle.empty()) return true;
    auto it = std::search(hay.begin(), hay.end(), needle.begin(), needle.end(), [](char a, char b) {
        return std::tolower((unsigned char)a) == std::tolower((unsigned char)b);
    });
    return it != hay.end();
}

FocusInfo focused_window(double max_age_s, bool want_title) {
    std::lock_guard<std::mutex> lock(g_mutex);   // callers queue behind one query instead of each spawning their own
    auto now = std::chrono::steady_clock::now();
    auto age = [&](std::chrono::steady_clock::time_point t) { return std::chrono::duration<double>(now - t).count(); };
    if (!g_have || age(g_id_at) > max_age_s) {
        std::string id;
        bool ok = run({"getactivewindow"}, id);
        if (!ok) {
            g_info = FocusInfo{};
        } else if (id != g_info.id || !g_info.ok) {
            g_info = FocusInfo{};
            g_info.ok = true;
            g_info.id = id;
            if (!id.empty()) run({"getwindowclassname", id}, g_info.cls);
            g_have_title = false;
        }
        g_have = true;
        g_id_at = now;
    }
    if (want_title && g_info.ok && !g_info.id.empty() && (!g_have_title || age(g_title_at) > max_age_s)) {
        run({"getwindowname", g_info.id}, g_info.title);
        g_have_title = true;
        g_title_at = now;
    }
    return g_info;
}

void focus_set_runner_for_tests(bool (*runner)(const std::vector<std::string>&, std::string&)) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_runner = runner;
    g_have = g_have_title = false;
    g_info = FocusInfo{};
}

} // namespace puppetry
