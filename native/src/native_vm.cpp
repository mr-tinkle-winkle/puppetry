#include "native_vm.hpp"
#include <cctype>
#include <optional>
#include <regex>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include "keycodes.hpp"
#include "primitives.hpp"
#include "simplified_names.hpp"

namespace puppetry {

// ---------------------------------------------------------------------
// Parsing: one call-statement line -> a CompiledCall
// ---------------------------------------------------------------------

namespace {

struct CompiledArg {
    enum class Kind { IntLit, FloatLit, StrLit, BoolLit, KeyCodeLit, ParamRef } kind;
    long long i = 0;
    double d = 0;
    std::string s;
    int key_code = 0;
    std::string param_name;
};

struct CompiledCallArg {
    std::optional<std::string> keyword;
    CompiledArg value;
};

struct CompiledStatement {
    std::string callee;
    std::vector<CompiledCallArg> args;
    int line_no;
};

std::string trim(const std::string& s) {
    size_t start = s.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) return "";
    size_t end = s.find_last_not_of(" \t\r\n");
    return s.substr(start, end - start + 1);
}

std::vector<std::string> split_top_level_commas(const std::string& s) {
    std::vector<std::string> parts;
    int depth = 0;
    bool in_squote = false, in_dquote = false;
    std::string current;
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (in_squote) { current += c; if (c == '\'' && s[i - 1] != '\\') in_squote = false; continue; }
        if (in_dquote) { current += c; if (c == '"' && s[i - 1] != '\\') in_dquote = false; continue; }
        if (c == '\'') { in_squote = true; current += c; continue; }
        if (c == '"') { in_dquote = true; current += c; continue; }
        if (c == '(' || c == '[' || c == '{') { ++depth; current += c; continue; }
        if (c == ')' || c == ']' || c == '}') { --depth; current += c; continue; }
        if (c == ',' && depth == 0) { parts.push_back(current); current.clear(); continue; }
        current += c;
    }
    if (!trim(current).empty() || !parts.empty()) parts.push_back(current);
    return parts;
}

bool is_identifier(const std::string& s) {
    if (s.empty() || !(std::isalpha((unsigned char)s[0]) || s[0] == '_')) return false;
    for (char c : s) if (!(std::isalnum((unsigned char)c) || c == '_')) return false;
    return true;
}

std::string unescape_string_literal(const std::string& quoted) {
    // quoted includes the surrounding quote characters.
    std::string body = quoted.substr(1, quoted.size() - 2);
    std::string out;
    for (size_t i = 0; i < body.size(); ++i) {
        if (body[i] == '\\' && i + 1 < body.size() && (body[i + 1] == '"' || body[i + 1] == '\'' || body[i + 1] == '\\')) {
            out += body[++i];
        } else {
            out += body[i];
        }
    }
    return out;
}

// Resolves a bare identifier at COMPILE time against: this macro's own
// declared parameters (-> ParamRef, resolved per-call at run time),
// then KEY_*/BTN_* names, then (if enabled) simplified names.
CompiledArg resolve_identifier(const std::string& name, const std::vector<ArgumentDecl>& params,
                                bool simplified_names, int line_no) {
    for (const auto& p : params) {
        if (p.name == name) return {CompiledArg::Kind::ParamRef, 0, 0, "", 0, name};
    }
    int code;
    if (resolve_key_name(name, code)) return {CompiledArg::Kind::KeyCodeLit, 0, 0, "", code, ""};
    if (simplified_names) {
        auto ns = build_simplified_namespace();
        auto it = ns.find(name);
        if (it != ns.end()) return {CompiledArg::Kind::KeyCodeLit, 0, 0, "", it->second, ""};
    }
    throw MacroCompileError("line " + std::to_string(line_no) + ": unknown name '" + name +
                             "' (python_off macros only resolve parameters, KEY_*/BTN_* names, "
                             "and, if enabled, simplified names -- no other Python names exist here)");
}

CompiledArg parse_literal_or_ref(const std::string& raw, const std::vector<ArgumentDecl>& params,
                                  bool simplified_names, int line_no) {
    std::string text = trim(raw);
    if (text.size() >= 2 && ((text.front() == '\'' && text.back() == '\'') ||
                              (text.front() == '"' && text.back() == '"'))) {
        return {CompiledArg::Kind::StrLit, 0, 0, unescape_string_literal(text), 0, ""};
    }
    if (text == "True") return {CompiledArg::Kind::BoolLit, 1, 0, "", 0, ""};
    if (text == "False") return {CompiledArg::Kind::BoolLit, 0, 0, "", 0, ""};
    static const std::regex int_re(R"(^-?\d+$)");
    static const std::regex float_re(R"(^-?\d+\.\d+$)");
    if (std::regex_match(text, int_re)) {
        return {CompiledArg::Kind::IntLit, std::stoll(text), 0, "", 0, ""};
    }
    if (std::regex_match(text, float_re)) {
        return {CompiledArg::Kind::FloatLit, 0, std::stod(text), "", 0, ""};
    }
    if (is_identifier(text)) {
        return resolve_identifier(text, params, simplified_names, line_no);
    }
    throw MacroCompileError("line " + std::to_string(line_no) + ": can't parse '" + raw +
                             "' -- python_off macros only accept literal numbers/strings/"
                             "True/False and bare names, no expressions");
}

CompiledStatement parse_call_line(const std::string& line, int line_no,
                                   const std::vector<ArgumentDecl>& params, bool simplified_names) {
    static const std::regex call_re(R"(^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$)");
    std::smatch m;
    if (!std::regex_match(line, m, call_re)) {
        throw MacroCompileError("line " + std::to_string(line_no) + ": expected a single call "
                                 "'name(args...)' -- python_off macros are primitives-only, no "
                                 "if/while/assignment/other statements");
    }
    CompiledStatement stmt;
    stmt.callee = m[1].str();
    stmt.line_no = line_no;
    std::string inner = trim(m[2].str());
    if (!inner.empty()) {
        for (const auto& raw_part : split_top_level_commas(inner)) {
            std::string part = trim(raw_part);
            if (part.empty()) continue;
            std::optional<std::string> keyword;
            std::string value_text = part;
            // keyword=value, distinguished from a plain "==" or string
            // containing '=' by requiring the part BEFORE '=' to be a
            // bare identifier.
            size_t eq = part.find('=');
            if (eq != std::string::npos && eq > 0 && part[eq - 1] != '=' &&
                (eq + 1 >= part.size() || part[eq + 1] != '=')) {
                std::string maybe_name = trim(part.substr(0, eq));
                if (is_identifier(maybe_name)) {
                    keyword = maybe_name;
                    value_text = part.substr(eq + 1);
                }
            }
            stmt.args.push_back({keyword, parse_literal_or_ref(value_text, params, simplified_names, line_no)});
        }
    }
    return stmt;
}

// ---------------------------------------------------------------------
// Runtime value + resolution
// ---------------------------------------------------------------------

struct RuntimeValue {
    enum class Kind { Int, Float, Str, Bool } kind;
    long long i = 0;
    double d = 0;
    std::string s;
    bool b = false;

    int as_int() const {
        if (kind == Kind::Int) return (int)i;
        if (kind == Kind::Float) return (int)d;
        if (kind == Kind::Str) return std::atoi(s.c_str());
        return b ? 1 : 0;
    }
    double as_double() const {
        if (kind == Kind::Float) return d;
        if (kind == Kind::Int) return (double)i;
        if (kind == Kind::Str) return std::atof(s.c_str());
        return b ? 1.0 : 0.0;
    }
    std::string as_string() const {
        switch (kind) {
            case Kind::Str: return s;
            case Kind::Int: return std::to_string(i);
            case Kind::Float: return std::to_string(d);
            case Kind::Bool: return b ? "True" : "False";
        }
        return "";
    }
    bool as_bool() const {
        if (kind == Kind::Bool) return b;
        if (kind == Kind::Str) return !s.empty();
        if (kind == Kind::Int) return i != 0;
        return d != 0;
    }
    // Resolves to a key/button code: already-int is used directly; a
    // string is looked up by name first (KEY_A etc.), falling back to
    // parsing it as a plain integer -- covers a CLI-supplied string
    // arg like puppetry --name=... "KEY_A" as well as a bare "30".
    int as_key_code() const {
        if (kind == Kind::Int) return (int)i;
        if (kind == Kind::Str) {
            int code;
            if (resolve_key_name(s, code)) return code;
            return std::atoi(s.c_str());
        }
        return as_int();
    }
};

RuntimeValue value_from_string(const std::string& s) { return {RuntimeValue::Kind::Str, 0, 0, s, false}; }

RuntimeValue resolve_arg(const CompiledArg& arg, const std::unordered_map<std::string, RuntimeValue>& params) {
    switch (arg.kind) {
        case CompiledArg::Kind::IntLit: return {RuntimeValue::Kind::Int, arg.i, 0, "", false};
        case CompiledArg::Kind::FloatLit: return {RuntimeValue::Kind::Float, 0, arg.d, "", false};
        case CompiledArg::Kind::StrLit: return {RuntimeValue::Kind::Str, 0, 0, arg.s, false};
        case CompiledArg::Kind::BoolLit: return {RuntimeValue::Kind::Bool, 0, 0, "", arg.i != 0};
        case CompiledArg::Kind::KeyCodeLit: return {RuntimeValue::Kind::Int, arg.key_code, 0, "", false};
        case CompiledArg::Kind::ParamRef: {
            auto it = params.find(arg.param_name);
            if (it != params.end()) return it->second;
            return {RuntimeValue::Kind::Int, 0, 0, "", false};
        }
    }
    return {RuntimeValue::Kind::Int, 0, 0, "", false};
}

// Resolves a compiled call's arguments against the current param
// bindings, splitting into ordered positionals and a keyword map.
struct ResolvedArgs {
    std::vector<RuntimeValue> positional;
    std::unordered_map<std::string, RuntimeValue> keyword;

    RuntimeValue get(size_t pos_index, const std::string& kw_name, const RuntimeValue& fallback) const {
        auto it = keyword.find(kw_name);
        if (it != keyword.end()) return it->second;
        if (pos_index < positional.size()) return positional[pos_index];
        return fallback;
    }
};

ResolvedArgs resolve_args(const CompiledStatement& stmt, const std::unordered_map<std::string, RuntimeValue>& params) {
    ResolvedArgs out;
    for (const auto& a : stmt.args) {
        RuntimeValue v = resolve_arg(a.value, params);
        if (a.keyword) out.keyword[*a.keyword] = v;
        else out.positional.push_back(v);
    }
    return out;
}

const std::unordered_set<std::string> kKnownPrimitives = {
    "kd", "ku", "tap", "combo", "type", "move_mouse", "wheel", "wait",
    "speed", "ignore", "ignore_keys", "actAs", "command",
};

void execute_primitive(Runtime& rt, const std::string& name, const ResolvedArgs& args, int line_no) {
    if (name == "kd") { kd(rt, args.get(0, "key", {}).as_key_code()); return; }
    if (name == "ku") { ku(rt, args.get(0, "key", {}).as_key_code()); return; }
    if (name == "tap") {
        int key = args.get(0, "key", {}).as_key_code();
        double time_ = args.get(1, "time_", {RuntimeValue::Kind::Float, 0, 0.1, "", false}).as_double();
        tap(rt, key, time_);
        return;
    }
    if (name == "combo") {
        std::vector<int> keys;
        for (auto& v : args.positional) keys.push_back(v.as_key_code());
        double time_ = args.keyword.count("time_") ? args.keyword.at("time_").as_double() : 0.1;
        combo_fn(rt, keys, time_);
        return;
    }
    if (name == "type") {
        std::string text = args.get(0, "text", {}).as_string();
        double tpl = args.get(1, "time_per_letter", {RuntimeValue::Kind::Float, 0, 0.05, "", false}).as_double();
        bool async_ = args.get(2, "async_", {RuntimeValue::Kind::Bool, 0, 0, "", false}).as_bool();
        if (async_) std::thread([&rt, text, tpl] { type_text_fn(rt, text, tpl); }).detach();
        else type_text_fn(rt, text, tpl);
        return;
    }
    if (name == "move_mouse") {
        int x = args.get(0, "x_pixels", {}).as_int();
        int y = args.get(1, "y_pixels", {}).as_int();
        double time_ = args.get(2, "time_", {RuntimeValue::Kind::Float, 0, 0.25, "", false}).as_double();
        std::string easing = args.get(3, "easing", value_from_string("inout")).as_string();
        bool async_ = args.get(4, "async_", {RuntimeValue::Kind::Bool, 0, 0, "", false}).as_bool();
        bool move_to = args.get(5, "move_to", {RuntimeValue::Kind::Bool, 0, 0, "", false}).as_bool();
        if (async_) std::thread([&rt, x, y, time_, easing, move_to] { move_mouse_fn(rt, x, y, time_, easing, move_to); }).detach();
        else move_mouse_fn(rt, x, y, time_, easing, move_to);
        return;
    }
    if (name == "wheel") { wheel(rt, args.get(0, "amount", {}).as_int()); return; }
    if (name == "wait") {
        double time_ = args.get(0, "time_", {}).as_double();
        bool precise = args.get(1, "precise", {RuntimeValue::Kind::Bool, 0, 0, "", false}).as_bool();
        wait_fn(rt, time_, precise);
        return;
    }
    if (name == "speed") { speed_fn(rt, args.get(0, "multiplier", {}).as_double()); return; }
    if (name == "ignore") { ignore_fn(rt, args.get(0, "what", {}).as_string()); return; }
    if (name == "ignore_keys") {
        std::vector<int> codes;
        for (auto& v : args.positional) codes.push_back(v.as_key_code());
        ignore_keys_fn(rt, codes);
        return;
    }
    if (name == "actAs") {
        if (args.positional.size() < 2) {
            throw std::runtime_error("line " + std::to_string(line_no) + ": actAs() needs at least 2 arguments");
        }
        int key_pressing = args.positional[0].as_key_code();
        bool ignore_original = args.positional[1].as_bool();
        std::vector<int> acting;
        for (size_t i = 2; i < args.positional.size(); ++i) acting.push_back(args.positional[i].as_key_code());
        act_as_fn(rt, key_pressing, ignore_original, acting);
        return;
    }
    if (name == "command") {
        if (args.positional.empty()) throw std::runtime_error("line " + std::to_string(line_no) + ": command() needs at least 1 argument");
        std::string cmd = args.positional[0].as_string();
        std::vector<std::string> extra;
        for (size_t i = 1; i < args.positional.size(); ++i) extra.push_back(args.positional[i].as_string());
        command_fn(format_command(cmd, extra));
        return;
    }
}

} // namespace

// ---------------------------------------------------------------------
// NativeMacroBody
// ---------------------------------------------------------------------

class NativeMacroBody : public CompiledMacro {
public:
    NativeMacroBody(std::vector<CompiledStatement> statements, std::vector<ArgumentDecl> params,
                     std::vector<CompiledArg> resolved_defaults, std::string name)
        : statements_(std::move(statements)), params_(std::move(params)),
          resolved_defaults_(std::move(resolved_defaults)), name_(std::move(name)) {}

    void run(Runtime& rt, MacroRegistry& registry, const std::vector<std::string>& args) override {
        std::unordered_map<std::string, RuntimeValue> bound;
        for (size_t i = 0; i < params_.size(); ++i) {
            if (i < args.size()) {
                bound[params_[i].name] = value_from_string(args[i]);
            } else {
                bound[params_[i].name] = resolve_arg(resolved_defaults_[i], {});
            }
        }

        try {
            for (const auto& stmt : statements_) {
                ResolvedArgs resolved = resolve_args(stmt, bound);
                if (kKnownPrimitives.count(stmt.callee)) {
                    execute_primitive(rt, stmt.callee, resolved, stmt.line_no);
                } else {
                    std::shared_ptr<CompiledMacro> target;
                    try {
                        target = registry.get(stmt.callee);
                    } catch (const std::exception& exc) {
                        std::fprintf(stderr, "Macro '%s' line %d: %s\n", name_.c_str(), stmt.line_no, exc.what());
                        return;
                    }
                    std::vector<std::string> call_args;
                    for (auto& v : resolved.positional) call_args.push_back(v.as_string());
                    target->run(rt, registry, call_args);
                }
                rt.check_abort();
            }
        } catch (const MacroAborted&) {
            throw; // let the caller's fire-once/loop wrapper treat this as a clean stop
        }
    }

private:
    std::vector<CompiledStatement> statements_;
    std::vector<ArgumentDecl> params_;
    std::vector<CompiledArg> resolved_defaults_; // one per params_[i], parsed once at compile time
    std::string name_;
};

std::shared_ptr<CompiledMacro> compile_native_macro(const json& macro_def, MacroRegistry&) {
    std::string raw_body = macro_def.value("code", "");
    std::string name = macro_def.value("name", macro_def.value("id", std::string("macro")));
    bool simplified_names = macro_def.value("simplified_names", false);

    ExtractedBody extracted = extract_arguments_signature(raw_body); // throws MacroCompileError

    std::vector<CompiledArg> resolved_defaults;
    for (const auto& p : extracted.params) {
        resolved_defaults.push_back(parse_literal_or_ref(p.default_source, {}, simplified_names, 0));
    }

    std::vector<CompiledStatement> statements;
    std::istringstream iss(extracted.body);
    std::string line;
    int line_no = extracted.has_arguments_decl ? 2 : 1; // line 1 was the arguments(...) declaration, if present
    while (std::getline(iss, line)) {
        std::string t = trim(line);
        if (!t.empty() && t[0] != '#') {
            statements.push_back(parse_call_line(t, line_no, extracted.params, simplified_names));
        }
        ++line_no;
    }

    return std::make_shared<NativeMacroBody>(std::move(statements), extracted.params,
                                              std::move(resolved_defaults), name);
}

} // namespace puppetry
