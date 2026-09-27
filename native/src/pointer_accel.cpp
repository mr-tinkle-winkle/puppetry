#include "pointer_accel.hpp"
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <vector>
#include "config.hpp" // config_dir() -- its parent is ~/.config, where kcminputrc lives

namespace puppetry {

namespace {

// KDE's own names/values. Profile 1 is LIBINPUT_CONFIG_ACCEL_PROFILE_FLAT
// (2 would be adaptive, the default); acceleration 0 is 1:1 within that
// profile, i.e. one pixel of cursor per unit we emit.
constexpr const char* kProfileKey = "PointerAccelerationProfile";
constexpr const char* kProfileValue = "1";
constexpr const char* kAccelKey = "PointerAcceleration";
constexpr const char* kAccelValue = "0";

std::string trimmed(const std::string& s) {
    size_t b = 0, e = s.size();
    while (b < e && std::isspace((unsigned char)s[b])) ++b;
    while (e > b && std::isspace((unsigned char)s[e - 1])) --e;
    return s.substr(b, e - b);
}

// The key part of "Key=value" (trimmed), or "" for a line that isn't a
// key at all. Must be the WHOLE key: "PointerAcceleration" is a prefix of
// "PointerAccelerationProfile", so a prefix test would corrupt one while
// looking for the other.
std::string key_of(const std::string& line) {
    size_t eq = line.find('=');
    if (eq == std::string::npos) return "";
    return trimmed(line.substr(0, eq));
}

std::vector<std::string> split_lines(const std::string& text) {
    std::vector<std::string> lines;
    std::string cur;
    for (char c : text) {
        if (c == '\n') { lines.push_back(cur); cur.clear(); }
        else cur += c;
    }
    if (!cur.empty()) lines.push_back(cur);
    return lines;
}

std::string join_lines(const std::vector<std::string>& lines) {
    std::string out;
    for (const auto& l : lines) { out += l; out += '\n'; }
    return out;
}

bool file_exists(const fs::path& p) {
    std::error_code ec;
    return fs::exists(p, ec);
}

bool env_says_kde() {
    if (std::getenv("KDE_FULL_SESSION")) return true;
    const char* desktop = std::getenv("XDG_CURRENT_DESKTOP");
    if (!desktop) return false;
    std::string lower;
    for (const char* p = desktop; *p; ++p) lower += (char)std::tolower((unsigned char)*p);
    return lower.find("kde") != std::string::npos || lower.find("plasma") != std::string::npos;
}

fs::path xdg_config_home() { return config_dir().parent_path(); }
fs::path kcminputrc_path() { return xdg_config_home() / "kcminputrc"; }

} // namespace

std::string libinput_config_group(int vendor, int product, const std::string& device_name) {
    return "[Libinput][" + std::to_string(vendor) + "][" + std::to_string(product) + "][" + device_name + "]";
}

std::string kcminputrc_with_flat_accel(const std::string& current, const std::string& group) {
    std::vector<std::string> lines = split_lines(current);

    // Find our group's header, then the extent of the group (up to the
    // next header, or the end of the file).
    size_t header = lines.size();
    for (size_t i = 0; i < lines.size(); ++i) {
        if (trimmed(lines[i]) == group) { header = i; break; }
    }

    if (header == lines.size()) {
        // Not there yet -- append it, separated by a blank line from
        // whatever came before.
        std::vector<std::string> out = lines;
        if (!out.empty() && !trimmed(out.back()).empty()) out.push_back("");
        out.push_back(group);
        out.push_back(std::string(kAccelKey) + "=" + kAccelValue);
        out.push_back(std::string(kProfileKey) + "=" + kProfileValue);
        return join_lines(out);
    }

    size_t end = header + 1;
    while (end < lines.size() && !(trimmed(lines[end]).rfind('[', 0) == 0)) ++end;

    // Correct the two keys in place where they already exist; collect the
    // ones that need adding.
    bool have_accel = false, have_profile = false;
    for (size_t i = header + 1; i < end; ++i) {
        std::string k = key_of(lines[i]);
        if (k == kAccelKey) {
            lines[i] = std::string(kAccelKey) + "=" + kAccelValue;
            have_accel = true;
        } else if (k == kProfileKey) {
            lines[i] = std::string(kProfileKey) + "=" + kProfileValue;
            have_profile = true;
        }
    }
    // Insert missing keys directly under the header, so they land inside
    // the group no matter what trails it (blank lines, comments).
    std::vector<std::string> add;
    if (!have_accel) add.push_back(std::string(kAccelKey) + "=" + kAccelValue);
    if (!have_profile) add.push_back(std::string(kProfileKey) + "=" + kProfileValue);
    if (!add.empty()) lines.insert(lines.begin() + (long)header + 1, add.begin(), add.end());

    return join_lines(lines);
}

bool kde_config_present() {
    return file_exists(kcminputrc_path()) || file_exists(xdg_config_home() / "kwinrc") || env_says_kde();
}

AccelResult ensure_flat_pointer_accel(const std::string& device_name, int vendor, int product) {
    if (!kde_config_present()) return AccelResult::NotKde;

    const fs::path path = kcminputrc_path();
    std::string current;
    {
        std::ifstream in(path, std::ios::binary);
        if (in) {
            std::ostringstream ss;
            ss << in.rdbuf();
            current = ss.str();
        }
        // A missing file is fine: KDE is clearly installed (kwinrc or the
        // environment said so), we just get to create this one.
    }

    const std::string group = libinput_config_group(vendor, product, device_name);
    const std::string updated = kcminputrc_with_flat_accel(current, group);
    if (updated == current) return AccelResult::AlreadySet;

    // Temp file + rename, so a crash mid-write can't leave the user with
    // a truncated kcminputrc (it holds their real mouse's settings too).
    const fs::path tmp = path.string() + ".puppetry-tmp";
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        if (!out) return AccelResult::Failed;
        out << updated;
        if (!out) return AccelResult::Failed;
    }
    std::error_code ec;
    fs::rename(tmp, path, ec);
    if (ec) {
        fs::remove(tmp, ec);
        return AccelResult::Failed;
    }
    return AccelResult::Wrote;
}

} // namespace puppetry
