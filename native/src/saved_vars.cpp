#include "saved_vars.hpp"

#include <filesystem>
#include <fstream>
#include <mutex>
#include <stdexcept>

#include "config.hpp"
#include "macro.hpp"

namespace puppetry {

namespace {

std::mutex g_mutex;
json g_doc = json::object();
bool g_loaded = false;
fs::file_time_type g_mtime{};
std::string g_test_path;

fs::path vars_path() {
    if (!g_test_path.empty()) return g_test_path;
    return config_dir() / "saved_variables.json";
}

// Re-reads the file when it changed on disk (the GUI edited it). Caller holds g_mutex.
void sync_from_disk() {
    std::error_code ec;
    fs::path p = vars_path();
    auto mt = fs::last_write_time(p, ec);
    if (ec) {                       // no file yet
        if (!g_loaded) { g_doc = json::object(); g_loaded = true; }
        return;
    }
    if (g_loaded && mt == g_mtime) return;
    std::ifstream f(p);
    json doc = json::parse(f, nullptr, false);
    g_doc = (doc.is_discarded() || !doc.is_object()) ? json::object() : doc;
    g_mtime = mt;
    g_loaded = true;
}

void write_to_disk() {
    std::error_code ec;
    fs::path p = vars_path();
    fs::create_directories(p.parent_path(), ec);
    fs::path tmp = p;
    tmp += ".tmp";
    {
        std::ofstream f(tmp);
        if (!f) throw std::runtime_error("saveVar: can't write " + tmp.string());
        f << g_doc.dump(2) << "\n";
    }
    fs::rename(tmp, p, ec);
    if (ec) throw std::runtime_error("saveVar: can't write " + p.string());
    g_mtime = fs::last_write_time(p, ec);
}

} // namespace

void saved_var_set(const std::string& owner, const std::string& name, const json& value) {
    if (name.empty()) throw std::invalid_argument("saveVar: the name can't be empty");
    std::lock_guard<std::mutex> lock(g_mutex);
    sync_from_disk();
    json& set = g_doc[owner];
    if (!set.is_object()) set = json::object();
    if (set.contains(name) && set[name] == value) return;   // nothing to write
    set[name] = value;
    write_to_disk();
}

bool saved_var_get(const std::string& owner, const std::string& name, json& out) {
    std::lock_guard<std::mutex> lock(g_mutex);
    sync_from_disk();
    auto o = g_doc.find(owner);
    if (o == g_doc.end() || !o->is_object()) return false;
    auto v = o->find(name);
    if (v == o->end()) return false;
    out = *v;
    return true;
}

bool saved_var_clear(const std::string& owner, const std::string& name) {
    std::lock_guard<std::mutex> lock(g_mutex);
    sync_from_disk();
    auto o = g_doc.find(owner);
    if (o == g_doc.end() || !o->is_object() || !o->contains(name)) return false;
    o->erase(name);
    if (o->empty()) g_doc.erase(owner);
    write_to_disk();
    return true;
}

std::string saved_var_owner() {
    Macro* m = current_macro();
    return (m && !m->id.empty()) ? m->id : std::string("_global");
}

void saved_vars_set_path_for_tests(const std::string& path) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_test_path = path;
    g_loaded = false;
    g_doc = json::object();
}

} // namespace puppetry
