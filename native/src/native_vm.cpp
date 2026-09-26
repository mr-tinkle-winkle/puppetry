#include "native_vm.hpp"
#include <cctype>
#include <climits>
#include <cstdint>
#include <functional>
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
// Compiled form.
//
// HOT PATH: every line is compiled ONCE into a ready-to-call operation
// with its arguments already resolved to constants (or to an index into
// this run's parameter bindings). The first version re-resolved each
// line's arguments on every execution into freshly allocated vectors
// and a hash map, and dispatched primitives by string comparison.
// ---------------------------------------------------------------------

struct Value {
    enum class Kind { Int, Float, Str, Bool } kind = Kind::Int;
    long long i = 0;
    double d = 0;
    std::string s;
    bool b = false;

    static Value of_int(long long v) { Value x; x.kind = Kind::Int; x.i = v; return x; }
    static Value of_float(double v) { Value x; x.kind = Kind::Float; x.d = v; return x; }
    static Value of_str(std::string v) { Value x; x.kind = Kind::Str; x.s = std::move(v); return x; }
    static Value of_bool(bool v) { Value x; x.kind = Kind::Bool; x.b = v; return x; }

    int as_int() const {
        switch (kind) {
            case Kind::Int: return (int)i;
            case Kind::Float: return (int)d;
            case Kind::Str: return std::atoi(s.c_str());
            case Kind::Bool: return b ? 1 : 0;
        }
        return 0;
    }
    double as_double() const {
        switch (kind) {
            case Kind::Float: return d;
            case Kind::Int: return (double)i;
            case Kind::Str: return std::atof(s.c_str());
            case Kind::Bool: return b ? 1.0 : 0.0;
        }
        return 0;
    }
    std::string as_string() const {
        switch (kind) {
            case Kind::Str: return s;
            case Kind::Int: return std::to_string(i);
            case Kind::Float: { std::ostringstream o; o << d; return o.str(); }
            case Kind::Bool: return b ? "True" : "False";
        }
        return "";
    }
    bool as_bool() const {
        switch (kind) {
            case Kind::Bool: return b;
            case Kind::Str: return !s.empty() && s != "False" && s != "0";
            case Kind::Int: return i != 0;
            case Kind::Float: return d != 0;
        }
        return false;
    }
    // A string (e.g. a CLI-supplied "KEY_B") is looked up by name first,
    // then parsed as a plain integer code.
    int as_key_code() const {
        if (kind == Kind::Str) {
            int code;
            if (resolve_key_name(s, code)) return code;
            return std::atoi(s.c_str());
        }
        return as_int();
    }
};

using Bindings = std::vector<Value>;

struct Slot {
    bool is_param = false;
    int param = -1;
    Value constant;
    const Value& get(const Bindings& b) const { return is_param ? b[param] : constant; }
};

Slot const_slot(Value v) { Slot s; s.constant = std::move(v); return s; }

Slot to_slot(const CompiledArg& a, const std::vector<ArgumentDecl>& params) {
    switch (a.kind) {
        case CompiledArg::Kind::IntLit: return const_slot(Value::of_int(a.i));
        case CompiledArg::Kind::FloatLit: return const_slot(Value::of_float(a.d));
        case CompiledArg::Kind::StrLit: return const_slot(Value::of_str(a.s));
        case CompiledArg::Kind::BoolLit: return const_slot(Value::of_bool(a.i != 0));
        case CompiledArg::Kind::KeyCodeLit: return const_slot(Value::of_int(a.key_code));
        case CompiledArg::Kind::ParamRef:
            for (size_t i = 0; i < params.size(); ++i) {
                if (params[i].name == a.param_name) { Slot s; s.is_param = true; s.param = (int)i; return s; }
            }
            break;
    }
    return const_slot(Value::of_int(0));
}

using Op = std::function<void(Runtime&, MacroRegistry&, const Bindings&)>;

// Argument lookup for one call at COMPILE time: positional index or
// keyword name, else a default; unknown keywords / missing required
// arguments are compile errors (surfaced when the macro is saved).
struct CallArgs {
    std::vector<Slot> pos;
    std::unordered_map<std::string, Slot> kw;
    std::string fn;
    int line_no;

    void allow(std::initializer_list<const char*> names) const {
        for (const auto& [k, _] : kw) {
            bool ok = false;
            for (const char* n : names) if (k == n) { ok = true; break; }
            if (!ok) throw MacroCompileError("line " + std::to_string(line_no) + ": " + fn +
                                             "() got an unexpected keyword argument '" + k + "'");
        }
    }
    Slot at(size_t i, const char* name, std::optional<Value> fallback = std::nullopt) const {
        auto it = kw.find(name);
        if (it != kw.end()) return it->second;
        if (i < pos.size()) return pos[i];
        if (fallback) return const_slot(*fallback);
        throw MacroCompileError("line " + std::to_string(line_no) + ": " + fn + "() missing argument '" + name + "'");
    }
};

template <class F>
void spawn_background(F f) {
    std::thread([f = std::move(f)]() mutable {
        try { f(); } catch (...) {} // an escaping MacroAborted would std::terminate() the daemon
    }).detach();
}

Op compile_op(const CompiledStatement& stmt, const std::vector<ArgumentDecl>& params) {
    CallArgs c;
    c.fn = stmt.callee;
    c.line_no = stmt.line_no;
    for (const auto& a : stmt.args) {
        Slot s = to_slot(a.value, params);
        if (a.keyword) c.kw[*a.keyword] = s;
        else c.pos.push_back(s);
    }
    const std::string& n = stmt.callee;

    if (n == "kd" || n == "ku") {
        c.allow({"key"});
        Slot key = c.at(0, "key");
        if (n == "kd") return [key](Runtime& rt, MacroRegistry&, const Bindings& b) { kd(rt, key.get(b).as_key_code()); };
        return [key](Runtime& rt, MacroRegistry&, const Bindings& b) { ku(rt, key.get(b).as_key_code()); };
    }
    if (n == "tap") {
        c.allow({"key", "time_"});
        Slot key = c.at(0, "key"), t = c.at(1, "time_", Value::of_float(0.1));
        return [key, t](Runtime& rt, MacroRegistry&, const Bindings& b) {
            tap(rt, key.get(b).as_key_code(), t.get(b).as_double());
        };
    }
    if (n == "combo") {
        c.allow({"time_"});
        Slot t = c.at(SIZE_MAX, "time_", Value::of_float(0.1));
        std::vector<Slot> keys = c.pos;
        return [keys, t](Runtime& rt, MacroRegistry&, const Bindings& b) {
            std::vector<int> codes;
            codes.reserve(keys.size());
            for (const auto& k : keys) codes.push_back(k.get(b).as_key_code());
            combo_fn(rt, codes, t.get(b).as_double());
        };
    }
    if (n == "type") {
        c.allow({"text", "time_per_letter", "async_"});
        Slot text = c.at(0, "text"), tpl = c.at(1, "time_per_letter", Value::of_float(0.05)),
             async_ = c.at(2, "async_", Value::of_bool(false));
        return [text, tpl, async_](Runtime& rt, MacroRegistry&, const Bindings& b) {
            std::string s = text.get(b).as_string();
            double t = tpl.get(b).as_double();
            if (async_.get(b).as_bool()) {
                double mult = Runtime::speed_multiplier();
                spawn_background([&rt, s, t, mult] { Runtime::speed_multiplier() = mult; type_text_fn(rt, s, t); });
            } else {
                type_text_fn(rt, s, t);
            }
        };
    }
    if (n == "move_mouse") {
        c.allow({"x_pixels", "y_pixels", "time_", "easing", "async_", "move_to"});
        Slot x = c.at(0, "x_pixels"), y = c.at(1, "y_pixels"), t = c.at(2, "time_", Value::of_float(0.25)),
             easing = c.at(3, "easing", Value::of_str("inout")), async_ = c.at(4, "async_", Value::of_bool(false)),
             move_to = c.at(5, "move_to", Value::of_bool(false));
        return [=](Runtime& rt, MacroRegistry&, const Bindings& b) {
            int xv = x.get(b).as_int(), yv = y.get(b).as_int();
            double tv = t.get(b).as_double();
            const std::string& ev = easing.get(b).kind == Value::Kind::Str ? easing.get(b).s : std::string("inout");
            bool mt = move_to.get(b).as_bool();
            if (async_.get(b).as_bool()) {
                double mult = Runtime::speed_multiplier();
                std::string e = ev;
                spawn_background([&rt, xv, yv, tv, e, mt, mult] {
                    Runtime::speed_multiplier() = mult;
                    move_mouse_fn(rt, xv, yv, tv, e, mt);
                });
            } else {
                move_mouse_fn(rt, xv, yv, tv, ev, mt);
            }
        };
    }
    if (n == "wheel") {
        c.allow({"amount"});
        Slot amount = c.at(0, "amount");
        return [amount](Runtime& rt, MacroRegistry&, const Bindings& b) { wheel(rt, amount.get(b).as_int()); };
    }
    if (n == "wait") {
        c.allow({"time_", "precise"});
        Slot t = c.at(0, "time_"), precise = c.at(1, "precise", Value::of_bool(false));
        return [t, precise](Runtime& rt, MacroRegistry&, const Bindings& b) {
            wait_fn(rt, t.get(b).as_double(), precise.get(b).as_bool());
        };
    }
    if (n == "speed") {
        c.allow({"multiplier"});
        Slot m = c.at(0, "multiplier");
        return [m](Runtime& rt, MacroRegistry&, const Bindings& b) { speed_fn(rt, m.get(b).as_double()); };
    }
    if (n == "ignore") {
        c.allow({"what"});
        Slot what = c.at(0, "what");
        return [what](Runtime& rt, MacroRegistry&, const Bindings& b) { ignore_fn(rt, what.get(b).as_string()); };
    }
    if (n == "ignore_keys") {
        c.allow({});
        std::vector<Slot> keys = c.pos;
        return [keys](Runtime& rt, MacroRegistry&, const Bindings& b) {
            std::vector<int> codes;
            for (const auto& k : keys) codes.push_back(k.get(b).as_key_code());
            ignore_keys_fn(rt, codes);
        };
    }
    if (n == "actAs") {
        c.allow({});
        if (c.pos.size() < 2) {
            throw MacroCompileError("line " + std::to_string(stmt.line_no) + ": actAs() needs at least 2 arguments");
        }
        std::vector<Slot> all = c.pos;
        return [all](Runtime& rt, MacroRegistry&, const Bindings& b) {
            std::vector<int> acting;
            for (size_t i = 2; i < all.size(); ++i) acting.push_back(all[i].get(b).as_key_code());
            act_as_fn(rt, all[0].get(b).as_key_code(), all[1].get(b).as_bool(), acting);
        };
    }
    if (n == "command") {
        c.allow({});
        if (c.pos.empty()) throw MacroCompileError("line " + std::to_string(stmt.line_no) + ": command() needs a command");
        std::vector<Slot> all = c.pos;
        return [all](Runtime&, MacroRegistry&, const Bindings& b) {
            std::vector<std::string> extra;
            for (size_t i = 1; i < all.size(); ++i) extra.push_back(all[i].get(b).as_string());
            command_fn(format_command(all[0].get(b).as_string(), extra));
        };
    }

    // Anything else: a call to another macro by (sanitized) name, looked
    // up in the registry at CALL time. Crossing macro boundaries in
    // native mode is positional-strings only (same contract as CLI args).
    if (!c.kw.empty()) {
        throw MacroCompileError("line " + std::to_string(stmt.line_no) + ": keyword arguments aren't supported "
                                "when calling another macro from a python_off macro");
    }
    std::vector<Slot> all = c.pos;
    std::string callee = n;
    return [all, callee](Runtime& rt, MacroRegistry& reg, const Bindings& b) {
        std::vector<std::string> call_args;
        for (const auto& s : all) call_args.push_back(s.get(b).as_string());
        reg.get(callee)->run(rt, reg, call_args); // throws a clear "failed to compile" if unknown
    };
}

} // namespace

class NativeMacroBody : public CompiledMacro {
public:
    NativeMacroBody(std::vector<Op> ops, std::vector<Value> defaults, std::string name)
        : ops_(std::move(ops)), defaults_(std::move(defaults)), name_(std::move(name)) {}

    void run(Runtime& rt, MacroRegistry& registry, const std::vector<std::string>& args) override {
        if (defaults_.empty()) {
            static const Bindings kEmpty;
            execute(rt, registry, kEmpty);
            return;
        }
        Bindings bound = defaults_;
        for (size_t i = 0; i < bound.size() && i < args.size(); ++i) bound[i] = Value::of_str(args[i]);
        execute(rt, registry, bound);
    }

private:
    void execute(Runtime& rt, MacroRegistry& registry, const Bindings& b) {
        for (const auto& op : ops_) {
            op(rt, registry, b);
            rt.check_abort();
        }
    }

    std::vector<Op> ops_;
    std::vector<Value> defaults_; // one per declared parameter
    std::string name_;
};

std::shared_ptr<CompiledMacro> compile_native_macro(const json& macro_def, MacroRegistry&) {
    std::string raw_body = json_str(macro_def, "code", "");
    std::string name = json_str(macro_def, "name", json_str(macro_def, "id", "macro"));
    bool simplified_names = json_bool(macro_def, "simplified_names", false);

    ExtractedBody extracted = extract_arguments_signature(raw_body); // throws MacroCompileError

    std::vector<Value> defaults;
    for (const auto& p : extracted.params) {
        Slot s = to_slot(parse_literal_or_ref(p.default_source, {}, simplified_names, 1), {});
        defaults.push_back(s.constant);
    }

    std::vector<Op> ops;
    std::istringstream iss(extracted.body);
    std::string line;
    int line_no = extracted.has_arguments_decl ? 2 : 1;
    while (std::getline(iss, line)) {
        std::string t = trim(line);
        if (!t.empty() && t[0] != '#') {
            ops.push_back(compile_op(parse_call_line(t, line_no, extracted.params, simplified_names), extracted.params));
        }
        ++line_no;
    }
    return std::make_shared<NativeMacroBody>(std::move(ops), std::move(defaults), name);
}

} // namespace puppetry
