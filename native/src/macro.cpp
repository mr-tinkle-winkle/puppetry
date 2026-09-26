#include "macro.hpp"
#include "primitives.hpp"
#include <algorithm>
#include <cctype>
#include <chrono>
#include <regex>
#include <sstream>
#include <thread>

namespace puppetry {

std::string sanitize_macro_name(const std::string& name) {
    std::string src = name.empty() ? "macro" : name;
    std::string ident;
    ident.reserve(src.size());
    for (char c : src) {
        bool word_char = std::isalnum((unsigned char)c) || c == '_';
        ident += word_char ? c : '_';
    }
    if (ident.empty() || std::isdigit((unsigned char)ident[0])) {
        ident = "m_" + ident;
    }
    return ident;
}

// Splits `s` on top-level commas (not inside (), [], {}, or quotes) --
// enough to separate "hits=3, key=KEY_A" into its two parameter specs
// without a full expression parser.
static std::vector<std::string> split_top_level(const std::string& s, char sep) {
    std::vector<std::string> parts;
    int depth = 0;
    bool in_squote = false, in_dquote = false;
    std::string current;
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (in_squote) {
            current += c;
            if (c == '\'' && (i == 0 || s[i - 1] != '\\')) in_squote = false;
            continue;
        }
        if (in_dquote) {
            current += c;
            if (c == '"' && (i == 0 || s[i - 1] != '\\')) in_dquote = false;
            continue;
        }
        if (c == '\'') { in_squote = true; current += c; continue; }
        if (c == '"') { in_dquote = true; current += c; continue; }
        if (c == '(' || c == '[' || c == '{') { ++depth; current += c; continue; }
        if (c == ')' || c == ']' || c == '}') { --depth; current += c; continue; }
        if (c == sep && depth == 0) {
            parts.push_back(current);
            current.clear();
            continue;
        }
        current += c;
    }
    if (!current.empty() || !parts.empty()) parts.push_back(current);
    return parts;
}

static std::string trim(const std::string& s) {
    size_t start = s.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) return "";
    size_t end = s.find_last_not_of(" \t\r\n");
    return s.substr(start, end - start + 1);
}

// Splits on '\n' the way Python's str.split("\n") does: N newlines
// always produce N+1 elements, including a trailing "" if the string
// ends with '\n' -- unlike std::getline, which silently drops that
// trailing empty element. Matters here because the body text is
// rejoined with "\n" afterward, and losing that element would silently
// eat the macro's own trailing newline on every compile.
static std::vector<std::string> split_lines_python_style(const std::string& s) {
    std::vector<std::string> lines;
    size_t start = 0;
    while (true) {
        size_t nl = s.find('\n', start);
        if (nl == std::string::npos) {
            lines.push_back(s.substr(start));
            break;
        }
        lines.push_back(s.substr(start, nl - start));
        start = nl + 1;
    }
    return lines;
}

ExtractedBody extract_arguments_signature(const std::string& body) {
    std::vector<std::string> lines = split_lines_python_style(body);

    int first_idx = -1;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (!trim(lines[i]).empty()) { first_idx = (int)i; break; }
    }

    static const std::regex arguments_line_re(R"(^\s*arguments\s*\((.*)\)\s*$)");
    static const std::regex arguments_call_re(R"((?:^|[^\w.])arguments\s*\()");

    ExtractedBody result;
    if (first_idx >= 0) {
        std::smatch m;
        std::string first_line = lines[first_idx];
        if (std::regex_match(first_line, m, arguments_line_re)) {
            std::string param_source = trim(m[1].str());
            result.has_arguments_decl = true;
            if (!param_source.empty()) {
                for (const auto& raw_part : split_top_level(param_source, ',')) {
                    std::string part = trim(raw_part);
                    if (part.empty()) continue;
                    size_t eq = part.find('=');
                    if (eq == std::string::npos) {
                        throw MacroCompileError(
                            "Every parameter in arguments(...) needs a default value "
                            "(e.g. arguments(hits=3, key=KEY_A)) -- a macro can always "
                            "be triggered with no arguments at all (hotkey combo, "
                            "puppetry --name=...), so there has to be a fallback value.");
                    }
                    std::string pname = trim(part.substr(0, eq));
                    std::string pdefault = trim(part.substr(eq + 1));
                    if (pname.rfind("*", 0) == 0) {
                        throw MacroCompileError(
                            "arguments(...) only supports plain name=default parameters "
                            "-- no *args, **kwargs, or keyword-only markers");
                    }
                    result.params.push_back({pname, pdefault});
                }
            }
            lines.erase(lines.begin() + first_idx);
        }
    }

    std::ostringstream remaining;
    for (size_t i = 0; i < lines.size(); ++i) {
        remaining << lines[i];
        if (i + 1 < lines.size()) remaining << "\n";
    }
    std::string remaining_str = remaining.str();

    if (std::regex_search(remaining_str, arguments_call_re)) {
        throw MacroCompileError("arguments(...) is only allowed on the very first line of a macro");
    }

    result.body = remaining_str;
    return result;
}

RepeatMode parse_repeat_mode(const std::string& s) {
    if (s == "hold") return RepeatMode::Hold;
    if (s == "toggle") return RepeatMode::Toggle;
    return RepeatMode::None;
}

TriggerEdge parse_trigger_edge(const std::string& s) {
    return s == "up" ? TriggerEdge::Up : TriggerEdge::Down;
}

bool Macro::combo_is_subset_of(const std::vector<int>& held_set) const {
    for (int c : combo) {
        if (std::find(held_set.begin(), held_set.end(), c) == held_set.end()) return false;
    }
    return true;
}

static void fire_once(Runtime& rt, MacroRegistry& registry, Macro& macro, std::vector<std::string> args) {
    std::thread([&rt, &registry, &macro, args = std::move(args)]() mutable {
        Runtime::speed_multiplier() = 1.0;
        try {
            macro.func->run(rt, registry, args);
        } catch (const MacroAborted&) {
            // clean stop, not an error -- same as the Python version
        } catch (const std::exception&) {
            // A macro's own runtime error shouldn't take the daemon
            // down; surfaces via the backend's own logging (see
            // python_embed.cpp/native_vm.cpp) before unwinding here.
        }
    }).detach();
}

static void loop_until_stopped(Runtime& rt, MacroRegistry& registry, std::shared_ptr<Macro> macro_ptr,
                                std::vector<std::string> args) {
    auto rts = macro_ptr->runtime;
    try {
        while (!rts->stop_flag.load()) {
            Runtime::speed_multiplier() = 1.0;
            macro_ptr->func->run(rt, registry, args);
        }
    } catch (const MacroAborted&) {
    } catch (const std::exception&) {
    }
    rts->active_hold.store(false);
}

bool macro_is_looping(const Macro& macro) {
    return macro.runtime->thread.joinable() && macro.runtime->active_hold.load();
}

void stop_macro_loop(Macro& macro) {
    macro.runtime->stop_flag.store(true);
}

static void start_loop(Runtime& rt, MacroRegistry& registry, Macro& macro, std::vector<std::string> args) {
    auto rts = macro.runtime;
    rts->stop_flag.store(false);
    rts->active_hold.store(true);
    if (rts->thread.joinable()) rts->thread.detach();
    // loop_until_stopped needs a stable reference to the Macro across
    // the life of the loop; callers own Macro objects in a container
    // that outlives the daemon's whole run (MACROS in daemon.cpp), so a
    // raw pointer wrapped in a non-owning shared_ptr aliasing ctor is
    // safe here and avoids restructuring Macro storage into
    // shared_ptr<Macro> everywhere else.
    Macro* raw = &macro;
    std::shared_ptr<Macro> alias(std::shared_ptr<void>(), raw);
    rts->thread = std::thread(loop_until_stopped, std::ref(rt), std::ref(registry), alias, std::move(args));
}

void trigger_macro(Runtime& rt, MacroRegistry& registry, Macro& macro, const std::vector<std::string>& args) {
    switch (macro.repeat_mode) {
        case RepeatMode::None:
            fire_once(rt, registry, macro, args);
            break;
        case RepeatMode::Hold:
            if (!macro_is_looping(macro)) start_loop(rt, registry, macro, args);
            break;
        case RepeatMode::Toggle:
            if (macro_is_looping(macro)) stop_macro_loop(macro);
            else start_loop(rt, registry, macro, args);
            break;
    }
}

} // namespace puppetry
