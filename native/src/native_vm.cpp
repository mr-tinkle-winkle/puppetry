// The native (python_off) macro interpreter.
//
// Session 13: no longer primitives-only. This is a small, self-contained
// interpreter for the Python subset the block editor produces (and that
// people naturally hand-write): variables, arithmetic, comparisons,
// and/or/not, if/elif/else, while, for-in (range() without building a
// list), break/continue, per-macro functions (def/return), lists/tuples,
// indexing, tuple unpacking, a few builtins, every primitive, and calls
// to other macros by name.
//
// Pipeline: tokenizer (Python-style INDENT/DEDENT) -> recursive-descent
// parser -> AST -> compile pass that resolves every name ONCE (locals to
// slot indices, key names to constants) and emits a tree of closures.
// Nothing is looked up by string at run time except calls to other
// macros (by design: the registry is resolved at call time).
//
// HOT PATH: a primitive call with literal arguments still compiles to one
// closure holding pre-resolved constant Values, exactly like the old
// primitives-only VM -- transcribed macros and autoclickers don't pay for
// the interpreter.
#include "native_vm.hpp"
#include <algorithm>
#include <cctype>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <optional>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include "keycodes.hpp"
#include "primitives.hpp"
#include "simplified_names.hpp"

namespace puppetry {

namespace {

// =====================================================================
// Values
// =====================================================================

struct Value;
using List = std::vector<Value>;

struct Value {
    enum class Kind { None, Int, Float, Str, Bool, List, Unset } kind = Kind::None;
    long long i = 0;
    double d = 0;
    bool b = false;
    std::string s;
    std::shared_ptr<List> l;
    bool is_tuple = false;
    bool is_point = false; // a getMousePosition() result: also has .x / .y

    static Value none() { return Value{}; }
    static Value unset() { Value v; v.kind = Kind::Unset; return v; }
    static Value of_int(long long x) { Value v; v.kind = Kind::Int; v.i = x; return v; }
    static Value of_float(double x) { Value v; v.kind = Kind::Float; v.d = x; return v; }
    static Value of_str(std::string x) { Value v; v.kind = Kind::Str; v.s = std::move(x); return v; }
    static Value of_bool(bool x) { Value v; v.kind = Kind::Bool; v.b = x; return v; }
    static Value of_list(List x, bool tuple = false) {
        Value v; v.kind = Kind::List; v.l = std::make_shared<List>(std::move(x)); v.is_tuple = tuple; return v;
    }

    bool is_number() const { return kind == Kind::Int || kind == Kind::Float || kind == Kind::Bool; }
    bool is_integral() const { return kind == Kind::Int || kind == Kind::Bool; }
    double num() const {
        switch (kind) {
            case Kind::Int: return (double)i;
            case Kind::Float: return d;
            case Kind::Bool: return b ? 1.0 : 0.0;
            default: return 0;
        }
    }
    long long inum() const { return kind == Kind::Bool ? (b ? 1 : 0) : (kind == Kind::Int ? i : (long long)d); }

    // ---- lenient conversions used for PRIMITIVE arguments (CLI args
    // arrive as strings; these keep the old native VM's behavior) ----
    int as_int() const {
        switch (kind) {
            case Kind::Int: return (int)i;
            case Kind::Float: return (int)d;
            case Kind::Str: return std::atoi(s.c_str());
            case Kind::Bool: return b ? 1 : 0;
            default: return 0;
        }
    }
    double as_double() const {
        switch (kind) {
            case Kind::Float: return d;
            case Kind::Int: return (double)i;
            case Kind::Str: return std::atof(s.c_str());
            case Kind::Bool: return b ? 1.0 : 0.0;
            default: return 0;
        }
    }
    bool as_arg_bool() const {
        switch (kind) {
            case Kind::Bool: return b;
            case Kind::Str: return !s.empty() && s != "False" && s != "0";
            case Kind::Int: return i != 0;
            case Kind::Float: return d != 0;
            case Kind::List: return l && !l->empty();
            default: return false;
        }
    }
    int as_key_code() const {
        if (kind == Kind::Str) {
            int code;
            if (resolve_key_name(s, code)) return code;
            return std::atoi(s.c_str());
        }
        return as_int();
    }

    // ---- Python semantics ----
    bool truthy() const {
        switch (kind) {
            case Kind::None: case Kind::Unset: return false;
            case Kind::Int: return i != 0;
            case Kind::Float: return d != 0;
            case Kind::Bool: return b;
            case Kind::Str: return !s.empty();
            case Kind::List: return l && !l->empty();
        }
        return false;
    }
    std::string str() const;
    std::string repr() const { return kind == Kind::Str ? quote(s) : str(); }
    static std::string quote(const std::string& x) {
        std::string o = "'";
        for (char c : x) { if (c == '\'' || c == '\\') o += '\\'; o += c; }
        return o + "'";
    }
    const char* type_name() const {
        switch (kind) {
            case Kind::None: return "NoneType";
            case Kind::Int: return "int";
            case Kind::Float: return "float";
            case Kind::Str: return "str";
            case Kind::Bool: return "bool";
            case Kind::List: return is_tuple ? "tuple" : "list";
            case Kind::Unset: return "unset";
        }
        return "?";
    }
};

// Python's repr(float): the shortest digits that round-trip, printed in
// fixed notation when the exponent is in [-4, 16), else scientific.
std::string format_float(double d) {
    if (std::isnan(d)) return "nan";
    if (std::isinf(d)) return d > 0 ? "inf" : "-inf";
    if (d == 0) return std::signbit(d) ? "-0.0" : "0.0";
    char buf[64];
    int prec = 1;
    for (; prec <= 17; ++prec) {
        std::snprintf(buf, sizeof buf, "%.*e", prec - 1, d);
        if (std::strtod(buf, nullptr) == d) break;
    }
    std::string sci = buf;                       // e.g. "-3.00e+02"
    size_t epos = sci.find('e');
    int exp = std::atoi(sci.c_str() + epos + 1);
    std::string mant = sci.substr(0, epos);
    bool neg = mant[0] == '-';
    if (neg) mant = mant.substr(1);
    std::string digits;
    for (char c : mant) if (c != '.') digits += c;
    while (digits.size() > 1 && digits.back() == '0') digits.pop_back();
    std::string out;
    if (exp >= -4 && exp < 16) {
        if (exp >= 0) {
            if ((int)digits.size() <= exp + 1) out = digits + std::string(exp + 1 - digits.size(), '0') + ".0";
            else out = digits.substr(0, exp + 1) + "." + digits.substr(exp + 1);
        } else {
            out = "0." + std::string(-exp - 1, '0') + digits;
        }
    } else {
        out = digits.substr(0, 1);
        if (digits.size() > 1) out += "." + digits.substr(1);
        char eb[16];
        std::snprintf(eb, sizeof eb, "e%c%02d", exp < 0 ? '-' : '+', std::abs(exp));
        out += eb;
    }
    return (neg ? "-" : "") + out;
}

std::string Value::str() const {
    switch (kind) {
        case Kind::None: return "None";
        case Kind::Unset: return "<unset>";
        case Kind::Int: return std::to_string(i);
        case Kind::Float: return format_float(d);
        case Kind::Bool: return b ? "True" : "False";
        case Kind::Str: return s;
        case Kind::List: {
            std::string o = is_tuple ? "(" : "[";
            for (size_t k = 0; k < l->size(); ++k) {
                if (k) o += ", ";
                o += (*l)[k].repr();
            }
            if (is_tuple && l->size() == 1) o += ",";
            return o + (is_tuple ? ")" : "]");
        }
    }
    return "";
}

[[noreturn]] void type_error(const std::string& msg) { throw std::runtime_error("TypeError: " + msg); }

bool values_equal(const Value& a, const Value& b) {
    if (a.is_number() && b.is_number()) return a.num() == b.num();
    if (a.kind != b.kind) return false;
    switch (a.kind) {
        case Value::Kind::None: return true;
        case Value::Kind::Str: return a.s == b.s;
        case Value::Kind::List: {
            if (a.l->size() != b.l->size()) return false;
            for (size_t k = 0; k < a.l->size(); ++k) if (!values_equal((*a.l)[k], (*b.l)[k])) return false;
            return true;
        }
        default: return false;
    }
}

int compare_values(const Value& a, const Value& b, const std::string& op) {
    if (a.is_number() && b.is_number()) {
        double x = a.num(), y = b.num();
        return x < y ? -1 : (x > y ? 1 : 0);
    }
    if (a.kind == Value::Kind::Str && b.kind == Value::Kind::Str) return a.s < b.s ? -1 : (a.s > b.s ? 1 : 0);
    if (a.kind == Value::Kind::List && b.kind == Value::Kind::List) {
        size_t n = std::min(a.l->size(), b.l->size());
        for (size_t k = 0; k < n; ++k) {
            if (!values_equal((*a.l)[k], (*b.l)[k])) return compare_values((*a.l)[k], (*b.l)[k], op);
        }
        return a.l->size() < b.l->size() ? -1 : (a.l->size() > b.l->size() ? 1 : 0);
    }
    type_error("'" + op + "' not supported between " + a.type_name() + " and " + b.type_name());
}

bool contains_value(const Value& container, const Value& item) {
    if (container.kind == Value::Kind::List) {
        for (const auto& v : *container.l) if (values_equal(v, item)) return true;
        return false;
    }
    if (container.kind == Value::Kind::Str && item.kind == Value::Kind::Str) {
        return container.s.find(item.s) != std::string::npos;
    }
    type_error(std::string("argument of type '") + container.type_name() + "' is not iterable");
}

long long floordiv_ll(long long a, long long b) {
    long long q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
    return q;
}

Value binary_op(const std::string& op, const Value& a, const Value& b) {
    using K = Value::Kind;
    if (op == "+") {
        if (a.is_integral() && b.is_integral()) return Value::of_int(a.inum() + b.inum());
        if (a.is_number() && b.is_number()) return Value::of_float(a.num() + b.num());
        if (a.kind == K::Str && b.kind == K::Str) return Value::of_str(a.s + b.s);
        if (a.kind == K::List && b.kind == K::List) {
            List out = *a.l;
            out.insert(out.end(), b.l->begin(), b.l->end());
            return Value::of_list(std::move(out), a.is_tuple);
        }
    } else if (op == "-") {
        if (a.is_integral() && b.is_integral()) return Value::of_int(a.inum() - b.inum());
        if (a.is_number() && b.is_number()) return Value::of_float(a.num() - b.num());
    } else if (op == "*") {
        if (a.is_integral() && b.is_integral()) return Value::of_int(a.inum() * b.inum());
        if (a.is_number() && b.is_number()) return Value::of_float(a.num() * b.num());
        const Value* seq = a.kind == K::Str || a.kind == K::List ? &a : (b.kind == K::Str || b.kind == K::List ? &b : nullptr);
        const Value* n = seq == &a ? &b : &a;
        if (seq && n->is_integral()) {
            long long times = std::max(0LL, n->inum());
            if (seq->kind == K::Str) {
                std::string o;
                for (long long k = 0; k < times; ++k) o += seq->s;
                return Value::of_str(o);
            }
            List o;
            for (long long k = 0; k < times; ++k) o.insert(o.end(), seq->l->begin(), seq->l->end());
            return Value::of_list(std::move(o), seq->is_tuple);
        }
    } else if (op == "/") {
        if (a.is_number() && b.is_number()) {
            if (b.num() == 0) throw std::runtime_error("ZeroDivisionError: division by zero");
            return Value::of_float(a.num() / b.num());
        }
    } else if (op == "//") {
        if (a.is_number() && b.is_number()) {
            if (b.num() == 0) throw std::runtime_error("ZeroDivisionError: integer division by zero");
            if (a.is_integral() && b.is_integral()) return Value::of_int(floordiv_ll(a.inum(), b.inum()));
            return Value::of_float(std::floor(a.num() / b.num()));
        }
    } else if (op == "%") {
        if (a.is_number() && b.is_number()) {
            if (b.num() == 0) throw std::runtime_error("ZeroDivisionError: modulo by zero");
            if (a.is_integral() && b.is_integral()) {
                long long r = a.inum() % b.inum();
                if (r != 0 && ((r < 0) != (b.inum() < 0))) r += b.inum();
                return Value::of_int(r);
            }
            double r = std::fmod(a.num(), b.num());
            if (r != 0 && ((r < 0) != (b.num() < 0))) r += b.num();
            return Value::of_float(r);
        }
    } else if (op == "**") {
        if (a.is_integral() && b.is_integral() && b.inum() >= 0) {
            long long r = 1, base = a.inum();
            for (long long e = b.inum(); e > 0; --e) r *= base;
            return Value::of_int(r);
        }
        if (a.is_number() && b.is_number()) return Value::of_float(std::pow(a.num(), b.num()));
    }
    type_error("unsupported operand type(s) for " + op + ": '" + a.type_name() + "' and '" + b.type_name() + "'");
}

Value mouse_point(Runtime& rt) {
    int x = 0, y = 0;
    get_mouse_position_fn(rt, x, y);
    Value v = Value::of_list({Value::of_int(x), Value::of_int(y)}, true);
    v.is_point = true;
    return v;
}

// =====================================================================
// Tokenizer
// =====================================================================

enum class Tk { Name, Number, String, Op, Newline, Indent, Dedent, End };

struct Token {
    Tk t;
    std::string v;
    int line;
    Value num; // Number/String payload
};

MacroCompileError err_at(int line, const std::string& msg) {
    return MacroCompileError("line " + std::to_string(line) + ": " + msg);
}

std::vector<Token> tokenize(const std::string& src, int first_line) {
    std::vector<Token> out;
    std::vector<int> indents{0};
    int line = first_line;
    size_t i = 0, n = src.size();
    int depth = 0;
    bool at_line_start = true;

    auto push = [&](Tk t, std::string v, Value num = Value{}) { out.push_back({t, std::move(v), line, std::move(num)}); };

    while (i <= n) {
        if (at_line_start && depth == 0) {
            // measure indentation; skip blank and comment-only lines
            int col = 0;
            size_t j = i;
            while (j < n && (src[j] == ' ' || src[j] == '\t')) { col = src[j] == '\t' ? (col / 8 + 1) * 8 : col + 1; ++j; }
            if (j >= n) { i = n + 1; break; }
            if (src[j] == '\n') { i = j + 1; ++line; continue; }
            if (src[j] == '\r') { i = j + 1; continue; }
            if (src[j] == '#') { while (j < n && src[j] != '\n') ++j; i = j; continue; }
            if (col > indents.back()) { indents.push_back(col); push(Tk::Indent, ""); }
            while (col < indents.back()) {
                indents.pop_back();
                push(Tk::Dedent, "");
            }
            if (col != indents.back()) throw err_at(line, "unindent does not match any outer indentation level");
            i = j;
            at_line_start = false;
        }
        if (i >= n) break;
        char c = src[i];
        if (c == '\n') {
            if (depth == 0) { push(Tk::Newline, ""); at_line_start = true; }
            ++line;
            ++i;
            continue;
        }
        if (c == ' ' || c == '\t' || c == '\r') { ++i; continue; }
        if (c == '\\' && i + 1 < n && src[i + 1] == '\n') { i += 2; ++line; continue; }
        if (c == '#') { while (i < n && src[i] != '\n') ++i; continue; }
        // strings (with optional r/u/b prefix)
        size_t p = i;
        bool raw = false;
        while (p < n && std::strchr("rRuUbBfF", src[p]) && p - i < 2) {
            if (src[p] == 'f' || src[p] == 'F') {
                if (p + 1 < n && (src[p + 1] == '"' || src[p + 1] == '\'')) {
                    throw err_at(line, "f-strings aren't supported by the native path -- use str() and +");
                }
            }
            if (src[p] == 'r' || src[p] == 'R') raw = true;
            ++p;
        }
        if (p < n && (src[p] == '"' || src[p] == '\'') && (p == i || !std::isalnum((unsigned char)src[p - 1]) || true)) {
            bool prefix_ok = true;
            for (size_t k = i; k < p; ++k) if (!std::strchr("rRuUbB", src[k])) prefix_ok = false;
            if (prefix_ok) {
                char q = src[p];
                bool triple = p + 2 < n && src[p + 1] == q && src[p + 2] == q;
                size_t k = p + (triple ? 3 : 1);
                std::string val;
                int start_line = line;
                while (true) {
                    if (k >= n) throw err_at(start_line, "unterminated string");
                    char ch = src[k];
                    if (!triple && ch == '\n') throw err_at(start_line, "unterminated string");
                    if (triple && ch == q && k + 2 < n + 0 && k + 2 <= n - 1 + 0 && src[k + 1] == q && src[k + 2] == q) { k += 3; break; }
                    if (!triple && ch == q) { ++k; break; }
                    if (ch == '\n') ++line;
                    if (ch == '\\' && !raw && k + 1 < n) {
                        char e = src[k + 1];
                        switch (e) {
                            case 'n': val += '\n'; break;
                            case 't': val += '\t'; break;
                            case 'r': val += '\r'; break;
                            case '0': val += '\0'; break;
                            case '\\': val += '\\'; break;
                            case '\'': val += '\''; break;
                            case '"': val += '"'; break;
                            case '\n': ++line; break;
                            default: val += '\\'; val += e;
                        }
                        k += 2;
                        continue;
                    }
                    if (ch == '\\' && raw && k + 1 < n) { val += ch; val += src[k + 1]; k += 2; continue; }
                    val += ch;
                    ++k;
                }
                // adjacent string literals concatenate, like Python
                if (!out.empty() && out.back().t == Tk::String) {
                    out.back().num.s += val;
                } else {
                    Token t{Tk::String, "", start_line, Value::of_str(val)};
                    out.push_back(std::move(t));
                }
                i = k;
                continue;
            }
        }
        if (std::isalpha((unsigned char)c) || c == '_') {
            size_t k = i;
            while (k < n && (std::isalnum((unsigned char)src[k]) || src[k] == '_')) ++k;
            push(Tk::Name, src.substr(i, k - i));
            i = k;
            continue;
        }
        if (std::isdigit((unsigned char)c) || (c == '.' && i + 1 < n && std::isdigit((unsigned char)src[i + 1]))) {
            size_t k = i;
            bool is_float = false;
            if (c == '0' && k + 1 < n && (src[k + 1] == 'x' || src[k + 1] == 'X')) {
                k += 2;
                while (k < n && (std::isxdigit((unsigned char)src[k]) || src[k] == '_')) ++k;
                std::string t = src.substr(i + 2, k - i - 2);
                t.erase(std::remove(t.begin(), t.end(), '_'), t.end());
                push(Tk::Number, src.substr(i, k - i), Value::of_int(std::stoll(t, nullptr, 16)));
                i = k;
                continue;
            }
            while (k < n && (std::isdigit((unsigned char)src[k]) || src[k] == '_')) ++k;
            if (k < n && src[k] == '.') { is_float = true; ++k; while (k < n && std::isdigit((unsigned char)src[k])) ++k; }
            if (k < n && (src[k] == 'e' || src[k] == 'E')) {
                size_t m = k + 1;
                if (m < n && (src[m] == '+' || src[m] == '-')) ++m;
                if (m < n && std::isdigit((unsigned char)src[m])) {
                    is_float = true;
                    k = m;
                    while (k < n && std::isdigit((unsigned char)src[k])) ++k;
                }
            }
            std::string t = src.substr(i, k - i);
            t.erase(std::remove(t.begin(), t.end(), '_'), t.end());
            push(Tk::Number, t, is_float ? Value::of_float(std::stod(t)) : Value::of_int(std::stoll(t)));
            i = k;
            continue;
        }
        static const char* three[] = {"**=", "//=", nullptr};
        static const char* two[] = {"==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "**", "//", "->", nullptr};
        bool matched = false;
        for (const char** op = three; *op; ++op) {
            if (src.compare(i, 3, *op) == 0) { push(Tk::Op, *op); i += 3; matched = true; break; }
        }
        if (!matched) for (const char** op = two; *op; ++op) {
            if (src.compare(i, 2, *op) == 0) { push(Tk::Op, *op); i += 2; matched = true; break; }
        }
        if (matched) continue;
        if (std::strchr("+-*/%<>=()[]{},:.;", c)) {
            if (c == '(' || c == '[' || c == '{') ++depth;
            if (c == ')' || c == ']' || c == '}') depth = std::max(0, depth - 1);
            push(Tk::Op, std::string(1, c));
            ++i;
            continue;
        }
        throw err_at(line, std::string("unexpected character '") + c + "'");
    }
    if (!out.empty() && out.back().t != Tk::Newline && out.back().t != Tk::Dedent && out.back().t != Tk::Indent) push(Tk::Newline, "");
    while (indents.size() > 1) { indents.pop_back(); push(Tk::Dedent, ""); }
    push(Tk::End, "");
    return out;
}

// =====================================================================
// AST
// =====================================================================

struct Expr;
using ExprP = std::shared_ptr<Expr>;
struct Expr {
    enum class K { Const, Name, Unary, Bin, And, Or, Not, Compare, Call, Subscript, ListE, TupleE, IfExp, Attr } k;
    int line = 0;
    Value cval;
    std::string name;             // Name / Call callee / Unary+Bin op
    std::vector<std::string> ops; // Compare
    std::vector<ExprP> kids;
    std::vector<std::pair<std::string, ExprP>> kwargs;
};

struct Stmt;
using StmtP = std::shared_ptr<Stmt>;
using Block = std::vector<StmtP>;
struct Stmt {
    enum class K { ExprS, Assign, AugAssign, If, While, For, Def, Return, Break, Continue, Pass, Nonlocal } k;
    int line = 0;
    std::vector<std::string> targets; // Assign (1 = plain, >1 = unpack), For, Nonlocal
    std::string op;                   // AugAssign
    ExprP e;                          // value / condition / iterable / return value
    std::vector<ExprP> conds;         // If: one per if/elif
    std::vector<Block> bodies;        // If: one per cond (+1 for else); While/For/Def: [0]
    bool has_else = false;
    std::string name;                 // Def
    std::vector<std::string> params;
    std::vector<ExprP> defaults;      // aligned with params (nullptr = required)
};

// =====================================================================
// Parser
// =====================================================================

class Parser {
public:
    explicit Parser(std::vector<Token> toks) : t_(std::move(toks)) {}

    Block parse_module() {
        Block out;
        while (!at(Tk::End)) {
            if (accept_tk(Tk::Newline)) continue;
            parse_statement(out);
        }
        return out;
    }

private:
    std::vector<Token> t_;
    size_t p_ = 0;

    const Token& cur() const { return t_[p_]; }
    bool at(Tk k) const { return cur().t == k; }
    bool at_op(const char* op) const { return cur().t == Tk::Op && cur().v == op; }
    bool at_kw(const char* kw) const { return cur().t == Tk::Name && cur().v == kw; }
    bool accept_tk(Tk k) { if (at(k)) { ++p_; return true; } return false; }
    bool accept_op(const char* op) { if (at_op(op)) { ++p_; return true; } return false; }
    bool accept_kw(const char* kw) { if (at_kw(kw)) { ++p_; return true; } return false; }
    [[noreturn]] void fail(const std::string& msg) const { throw err_at(cur().line, msg); }
    void expect_op(const char* op) { if (!accept_op(op)) fail(std::string("expected '") + op + "'"); }
    std::string expect_name() {
        if (!at(Tk::Name) || is_keyword(cur().v)) fail("expected a name");
        return t_[p_++].v;
    }
    static bool is_keyword(const std::string& s) {
        static const std::unordered_set<std::string> kws = {
            "if", "elif", "else", "while", "for", "in", "def", "return", "break", "continue", "pass",
            "and", "or", "not", "is", "True", "False", "None", "nonlocal", "global", "lambda",
            "import", "from", "class", "try", "except", "finally", "with", "as", "yield", "del", "raise"};
        return kws.count(s) > 0;
    }
    void end_simple() {
        if (accept_op(";")) { if (at(Tk::Newline)) ++p_; return; }
        if (!accept_tk(Tk::Newline) && !at(Tk::End) && !at(Tk::Dedent)) fail("expected end of line");
    }

    Block parse_suite() {
        expect_op(":");
        Block body;
        if (accept_tk(Tk::Newline)) {
            if (!accept_tk(Tk::Indent)) fail("expected an indented block");
            while (!accept_tk(Tk::Dedent)) {
                if (at(Tk::End)) break;
                if (accept_tk(Tk::Newline)) continue;
                parse_statement(body);
            }
        } else {
            parse_simple(body); // `if x: tap(A)`
        }
        return body;
    }

    void parse_statement(Block& out) {
        int line = cur().line;
        if (at(Tk::Indent)) fail("unexpected indent");
        if (accept_kw("if")) {
            auto s = std::make_shared<Stmt>();
            s->k = Stmt::K::If; s->line = line;
            s->conds.push_back(parse_expr());
            s->bodies.push_back(parse_suite());
            while (at_kw("elif")) {
                ++p_;
                s->conds.push_back(parse_expr());
                s->bodies.push_back(parse_suite());
            }
            if (accept_kw("else")) { s->bodies.push_back(parse_suite()); s->has_else = true; }
            out.push_back(s);
            return;
        }
        if (accept_kw("while")) {
            auto s = std::make_shared<Stmt>();
            s->k = Stmt::K::While; s->line = line;
            s->e = parse_expr();
            s->bodies.push_back(parse_suite());
            if (at_kw("else")) fail("while/else isn't supported by the native path");
            out.push_back(s);
            return;
        }
        if (accept_kw("for")) {
            auto s = std::make_shared<Stmt>();
            s->k = Stmt::K::For; s->line = line;
            s->targets.push_back(expect_name());
            while (accept_op(",")) s->targets.push_back(expect_name());
            if (!accept_kw("in")) fail("expected 'in'");
            s->e = parse_expr_list();
            s->bodies.push_back(parse_suite());
            if (at_kw("else")) fail("for/else isn't supported by the native path");
            out.push_back(s);
            return;
        }
        if (accept_kw("def")) {
            auto s = std::make_shared<Stmt>();
            s->k = Stmt::K::Def; s->line = line;
            s->name = expect_name();
            expect_op("(");
            while (!accept_op(")")) {
                if (at_op("*") || at_op("**")) fail("*args/**kwargs aren't supported by the native path");
                s->params.push_back(expect_name());
                s->defaults.push_back(accept_op("=") ? parse_expr() : nullptr);
                if (!accept_op(",") && !at_op(")")) fail("expected ',' or ')'");
            }
            if (accept_op("->")) parse_expr();
            s->bodies.push_back(parse_suite());
            out.push_back(s);
            return;
        }
        static const char* unsupported[] = {"import", "from", "class", "try", "with", "lambda", "yield",
                                            "raise", "del", "global", "async", "await", "match", nullptr};
        for (const char** u = unsupported; *u; ++u) {
            if (at_kw(*u) && !(std::string(*u) == "match" && p_ + 1 < t_.size() && t_[p_ + 1].t == Tk::Op)) {
                fail(std::string("'") + *u + "' isn't supported by the native path -- turn on "
                     "\"Run as embedded Python\" for this macro");
            }
        }
        parse_simple(out);
    }

    void parse_simple(Block& out) {
        int line = cur().line;
        auto s = std::make_shared<Stmt>();
        s->line = line;
        if (accept_kw("pass")) { s->k = Stmt::K::Pass; }
        else if (accept_kw("break")) { s->k = Stmt::K::Break; }
        else if (accept_kw("continue")) { s->k = Stmt::K::Continue; }
        else if (accept_kw("return")) {
            s->k = Stmt::K::Return;
            if (!at(Tk::Newline) && !at_op(";") && !at(Tk::End) && !at(Tk::Dedent)) s->e = parse_expr_list();
        } else if (accept_kw("nonlocal")) {
            s->k = Stmt::K::Nonlocal;
            s->targets.push_back(expect_name());
            while (accept_op(",")) s->targets.push_back(expect_name());
        } else {
            ExprP first = parse_expr_list();
            if (at_op("=")) {
                std::vector<std::string> names;
                auto collect = [&](const ExprP& e) {
                    if (e->k == Expr::K::Name) names.push_back(e->name);
                    else if (e->k == Expr::K::TupleE || e->k == Expr::K::ListE) {
                        for (auto& kid : e->kids) {
                            if (kid->k != Expr::K::Name) fail("can only assign to plain names");
                            names.push_back(kid->name);
                        }
                    } else fail("can only assign to plain names (no a[0] = / a.b =)");
                };
                collect(first);
                bool unpack = first->k != Expr::K::Name;
                ++p_;
                ExprP value = parse_expr_list();
                if (at_op("=")) fail("chained assignment (a = b = 1) isn't supported by the native path");
                s->k = Stmt::K::Assign;
                s->targets = names;
                if (unpack && names.size() == 1) s->targets.push_back(""); // marker: unpack 1
                s->e = value;
            } else if (cur().t == Tk::Op && cur().v.size() >= 2 && cur().v.back() == '=' &&
                       cur().v != "==" && cur().v != "!=" && cur().v != "<=" && cur().v != ">=") {
                if (first->k != Expr::K::Name) fail("can only change plain names");
                s->k = Stmt::K::AugAssign;
                s->targets.push_back(first->name);
                s->op = cur().v.substr(0, cur().v.size() - 1);
                ++p_;
                s->e = parse_expr();
            } else {
                s->k = Stmt::K::ExprS;
                s->e = first;
            }
        }
        end_simple();
        out.push_back(s);
    }

    // expr_list: a, b, c  -> tuple (single without comma -> the expr itself)
    ExprP parse_expr_list() {
        int line = cur().line;
        ExprP first = parse_expr();
        if (!at_op(",")) return first;
        auto t = std::make_shared<Expr>();
        t->k = Expr::K::TupleE; t->line = line;
        t->kids.push_back(first);
        while (accept_op(",")) {
            if (at(Tk::Newline) || at_op("=") || at_op(")") || at_op(":") || at(Tk::End)) break;
            t->kids.push_back(parse_expr());
        }
        return t;
    }

    ExprP mk(Expr::K k, int line) { auto e = std::make_shared<Expr>(); e->k = k; e->line = line; return e; }

    ExprP parse_expr() {
        int line = cur().line;
        ExprP e = parse_or();
        if (at_kw("if")) {
            ++p_;
            ExprP cond = parse_or();
            if (!accept_kw("else")) fail("expected 'else'");
            ExprP other = parse_expr();
            auto r = mk(Expr::K::IfExp, line);
            r->kids = {cond, e, other};
            return r;
        }
        return e;
    }
    ExprP parse_or() {
        ExprP e = parse_and();
        while (at_kw("or")) {
            int line = cur().line; ++p_;
            auto r = mk(Expr::K::Or, line);
            r->kids = {e, parse_and()};
            e = r;
        }
        return e;
    }
    ExprP parse_and() {
        ExprP e = parse_not();
        while (at_kw("and")) {
            int line = cur().line; ++p_;
            auto r = mk(Expr::K::And, line);
            r->kids = {e, parse_not()};
            e = r;
        }
        return e;
    }
    ExprP parse_not() {
        if (at_kw("not")) {
            int line = cur().line; ++p_;
            auto r = mk(Expr::K::Not, line);
            r->kids = {parse_not()};
            return r;
        }
        return parse_compare();
    }
    ExprP parse_compare() {
        int line = cur().line;
        ExprP e = parse_arith();
        std::vector<std::string> ops;
        std::vector<ExprP> operands{e};
        while (true) {
            std::string op;
            if (cur().t == Tk::Op && (cur().v == "==" || cur().v == "!=" || cur().v == "<" || cur().v == ">" ||
                                      cur().v == "<=" || cur().v == ">=")) { op = cur().v; ++p_; }
            else if (at_kw("in")) { op = "in"; ++p_; }
            else if (at_kw("not") && p_ + 1 < t_.size() && t_[p_ + 1].t == Tk::Name && t_[p_ + 1].v == "in") { op = "not in"; p_ += 2; }
            else if (at_kw("is")) {
                ++p_;
                op = accept_kw("not") ? "is not" : "is";
            } else break;
            ops.push_back(op);
            operands.push_back(parse_arith());
        }
        if (ops.empty()) return e;
        auto r = mk(Expr::K::Compare, line);
        r->ops = ops;
        r->kids = operands;
        return r;
    }
    ExprP parse_arith() {
        ExprP e = parse_term();
        while (at_op("+") || at_op("-")) {
            int line = cur().line;
            std::string op = t_[p_++].v;
            auto r = mk(Expr::K::Bin, line);
            r->name = op;
            r->kids = {e, parse_term()};
            e = r;
        }
        return e;
    }
    ExprP parse_term() {
        ExprP e = parse_unary();
        while (at_op("*") || at_op("/") || at_op("//") || at_op("%")) {
            int line = cur().line;
            std::string op = t_[p_++].v;
            auto r = mk(Expr::K::Bin, line);
            r->name = op;
            r->kids = {e, parse_unary()};
            e = r;
        }
        return e;
    }
    ExprP parse_unary() {
        if (at_op("-") || at_op("+")) {
            int line = cur().line;
            std::string op = t_[p_++].v;
            ExprP operand = parse_unary();
            if (operand->k == Expr::K::Const && operand->cval.is_number() && op == "-") {
                // fold -5 into a constant (keeps `wait(-1)`-style args constant)
                if (operand->cval.kind == Value::Kind::Float) operand->cval.d = -operand->cval.d;
                else operand->cval = Value::of_int(-operand->cval.inum());
                return operand;
            }
            auto r = mk(Expr::K::Unary, line);
            r->name = op;
            r->kids = {operand};
            return r;
        }
        return parse_power();
    }
    ExprP parse_power() {
        ExprP e = parse_postfix();
        if (at_op("**")) {
            int line = cur().line; ++p_;
            auto r = mk(Expr::K::Bin, line);
            r->name = "**";
            r->kids = {e, parse_unary()};
            return r;
        }
        return e;
    }
    ExprP parse_postfix() {
        ExprP e = parse_atom();
        while (true) {
            int line = cur().line;
            if (at_op("(")) {
                if (e->k != Expr::K::Name) fail("only named functions can be called");
                ++p_;
                auto c = mk(Expr::K::Call, line);
                c->name = e->name;
                while (!accept_op(")")) {
                    if (at_op("*") || at_op("**")) fail("*args/**kwargs aren't supported by the native path");
                    if (cur().t == Tk::Name && p_ + 1 < t_.size() && t_[p_ + 1].t == Tk::Op && t_[p_ + 1].v == "=") {
                        std::string kw = t_[p_].v;
                        p_ += 2;
                        c->kwargs.emplace_back(kw, parse_expr());
                    } else {
                        if (!c->kwargs.empty()) fail("positional argument follows keyword argument");
                        c->kids.push_back(parse_expr());
                    }
                    if (!accept_op(",") && !at_op(")")) fail("expected ',' or ')'");
                }
                e = c;
            } else if (at_op("[")) {
                ++p_;
                if (at_op(":")) fail("slicing isn't supported by the native path");
                auto s = mk(Expr::K::Subscript, line);
                s->kids = {e, parse_expr()};
                if (at_op(":")) fail("slicing isn't supported by the native path");
                expect_op("]");
                e = s;
            } else if (at_op(".")) {
                ++p_;
                auto a = mk(Expr::K::Attr, line);
                a->name = expect_name();
                a->kids = {e};
                if (at_op("(")) fail("method calls (a.b()) aren't supported by the native path -- turn on "
                                     "\"Run as embedded Python\"");
                e = a;
            } else break;
        }
        return e;
    }
    ExprP parse_atom() {
        int line = cur().line;
        const Token& t = cur();
        if (t.t == Tk::Number || t.t == Tk::String) {
            auto e = mk(Expr::K::Const, line);
            e->cval = t.num;
            ++p_;
            return e;
        }
        if (t.t == Tk::Name) {
            if (t.v == "True" || t.v == "False") { ++p_; auto e = mk(Expr::K::Const, line); e->cval = Value::of_bool(t.v == "True"); return e; }
            if (t.v == "None") { ++p_; return mk(Expr::K::Const, line); }
            if (is_keyword(t.v)) fail("unexpected '" + t.v + "'");
            auto e = mk(Expr::K::Name, line);
            e->name = t.v;
            ++p_;
            return e;
        }
        if (accept_op("(")) {
            if (accept_op(")")) return mk(Expr::K::TupleE, line);
            ExprP first = parse_expr();
            if (accept_op(")")) return first;
            auto tup = mk(Expr::K::TupleE, line);
            tup->kids.push_back(first);
            while (accept_op(",")) {
                if (at_op(")")) break;
                tup->kids.push_back(parse_expr());
            }
            expect_op(")");
            return tup;
        }
        if (accept_op("[")) {
            auto lst = mk(Expr::K::ListE, line);
            while (!accept_op("]")) {
                lst->kids.push_back(parse_expr());
                if (!accept_op(",") && !at_op("]")) fail("expected ',' or ']'");
            }
            return lst;
        }
        if (at_op("{")) fail("dicts/sets aren't supported by the native path");
        fail(t.t == Tk::Indent ? "unexpected indent" : "invalid syntax");
    }
};

// =====================================================================
// Compile: AST -> closures
// =====================================================================

struct Frame;
using EvalFn = std::function<Value(Frame&)>;
enum class Flow { Normal, Break, Continue, Return };
using ExecFn = std::function<Flow(Frame&)>;

struct Frame {
    std::vector<Value> locals;
    Frame* root = nullptr;   // the macro-level frame (itself at top level)
    Runtime* rt = nullptr;
    MacroRegistry* reg = nullptr;
    Value ret;
    int depth = 0;
};

// A compiled argument: a constant (hot path: no call at all) or an expression.
struct Arg {
    bool is_const = true;
    Value c;
    EvalFn fn;
    Value get(Frame& f) const { return is_const ? c : fn(f); }
    // Typed accessors: the constant path reads straight from `c` with no
    // Value copy (the hot path for transcribed/autoclicker lines).
    int key_code(Frame& f) const { return is_const ? c.as_key_code() : fn(f).as_key_code(); }
    double dbl(Frame& f) const { return is_const ? c.as_double() : fn(f).as_double(); }
    int integer(Frame& f) const { return is_const ? c.as_int() : fn(f).as_int(); }
    bool flag(Frame& f) const { return is_const ? c.as_arg_bool() : fn(f).as_arg_bool(); }
    std::string text(Frame& f) const { return is_const ? c.str() : fn(f).str(); }
};

struct FuncObj {
    std::string name;
    std::vector<std::string> params;
    std::vector<EvalFn> defaults; // empty fn = required; evaluated in the ROOT frame at call time
    int nlocals = 0;
    std::vector<ExecFn> body;
};

struct Scope {
    std::unordered_map<std::string, int> slots;
    std::unordered_set<std::string> nonlocals;
    Scope* parent = nullptr; // function scope -> macro scope
    int count() const { return (int)slots.size(); }
};

class Compiler {
public:
    Compiler(bool simplified, const std::vector<ArgumentDecl>& params) : simplified_(simplified) {
        for (const auto& p : params) top_.slots.emplace(p.name, (int)top_.slots.size());
        if (simplified_) simple_ns_ = build_simplified_namespace();
    }

    std::vector<ExecFn> compile_module(const Block& body) {
        // functions first (hoisted), so calls can precede the def and
        // functions can call each other/themselves
        collect_defs(body);
        collect_assigned(body, top_);
        for (auto& [name, st] : def_stmts_) compile_function(name, st);
        return compile_block(body, top_, false, false);
    }

    int top_count() const { return top_.count(); }
    Scope& top() { return top_; }

    EvalFn compile_expr_public(const ExprP& e) { return compile_expr(e, top_); }
    bool is_constant(const ExprP& e) const { return const_value(e).has_value(); }
    std::optional<Value> const_value(const ExprP& e) const;

private:
    bool simplified_;
    std::unordered_map<std::string, int> simple_ns_;
    Scope top_;
    std::unordered_map<std::string, StmtP> def_stmts_;
    std::unordered_map<std::string, std::shared_ptr<FuncObj>> funcs_;
    int loop_depth_ = 0;
    bool in_function_ = false;

    // ---- scope analysis ----
    void collect_defs(const Block& b) {
        for (const auto& s : b) {
            if (s->k == Stmt::K::Def) {
                if (funcs_.count(s->name)) throw err_at(s->line, "function '" + s->name + "' is defined twice");
                auto fo = std::make_shared<FuncObj>();
                fo->name = s->name;
                fo->params = s->params;
                funcs_[s->name] = fo;
                def_stmts_[s->name] = s;
            }
            for (const auto& body : s->bodies) {
                if (s->k != Stmt::K::Def) collect_defs(body);
            }
        }
    }
    void collect_assigned(const Block& b, Scope& sc) {
        for (const auto& s : b) {
            switch (s->k) {
                case Stmt::K::Assign: case Stmt::K::AugAssign: case Stmt::K::For:
                    for (const auto& t : s->targets) if (!t.empty() && !sc.nonlocals.count(t)) sc.slots.emplace(t, sc.count());
                    break;
                case Stmt::K::Nonlocal:
                    if (!sc.parent) throw err_at(s->line, "nonlocal is only allowed inside a function");
                    for (const auto& t : s->targets) sc.nonlocals.insert(t);
                    break;
                default: break;
            }
            if (s->k != Stmt::K::Def) for (const auto& body : s->bodies) collect_assigned(body, sc);
        }
    }

    void compile_function(const std::string& name, const StmtP& st) {
        auto fo = funcs_[name];
        Scope sc;
        sc.parent = &top_;
        for (const auto& p : st->params) sc.slots.emplace(p, sc.count());
        // nonlocal declarations must be seen before assignments are collected
        std::function<void(const Block&)> find_nonlocals = [&](const Block& b) {
            for (const auto& s : b) {
                if (s->k == Stmt::K::Nonlocal) for (const auto& t : s->targets) {
                    if (!top_.slots.count(t)) throw err_at(s->line, "no binding for nonlocal '" + t + "' found");
                    sc.nonlocals.insert(t);
                }
                if (s->k == Stmt::K::Def) throw err_at(s->line, "functions can't be defined inside functions on the native path");
                for (const auto& body : s->bodies) find_nonlocals(body);
            }
        };
        find_nonlocals(st->bodies[0]);
        collect_assigned(st->bodies[0], sc);
        for (size_t k = 0; k < st->defaults.size(); ++k) {
            fo->defaults.push_back(st->defaults[k] ? compile_expr(st->defaults[k], top_) : EvalFn{});
        }
        bool was = in_function_;
        int loops = loop_depth_;
        in_function_ = true;
        loop_depth_ = 0;
        fo->body = compile_block(st->bodies[0], sc, true, false);
        in_function_ = was;
        loop_depth_ = loops;
        fo->nlocals = sc.count();
    }

    // ---- names ----
    std::optional<int> key_constant(const std::string& n) const {
        int code;
        if (resolve_key_name(n, code)) return code;
        if (simplified_) {
            auto it = simple_ns_.find(n);
            if (it != simple_ns_.end()) return it->second;
        }
        return std::nullopt;
    }

    EvalFn load_name(const std::string& n, Scope& sc, int line) {
        auto unbound = [n, line]() -> Value {
            throw std::runtime_error("line " + std::to_string(line) + ": NameError: '" + n +
                                     "' is used before it's given a value");
        };
        if (!sc.nonlocals.count(n)) {
            auto it = sc.slots.find(n);
            if (it != sc.slots.end()) {
                int idx = it->second;
                return [idx, unbound](Frame& f) -> Value {
                    const Value& v = f.locals[idx];
                    if (v.kind == Value::Kind::Unset) unbound();
                    return v;
                };
            }
        }
        if (sc.parent) {
            auto it = sc.parent->slots.find(n);
            if (it != sc.parent->slots.end()) {
                int idx = it->second;
                return [idx, unbound](Frame& f) -> Value {
                    const Value& v = f.root->locals[idx];
                    if (v.kind == Value::Kind::Unset) unbound();
                    return v;
                };
            }
        }
        if (auto code = key_constant(n)) {
            Value c = Value::of_int(*code);
            return [c](Frame&) { return c; };
        }
        if (funcs_.count(n)) throw err_at(line, "'" + n + "' is a function -- call it with " + n + "()");
        throw err_at(line, "unknown name '" + n + "' (not a variable, a parameter, a KEY_*/BTN_* name" +
                           std::string(simplified_ ? ", or a simplified name)" : ")"));
    }

    std::function<void(Frame&, Value)> store_name(const std::string& n, Scope& sc, int line) {
        if (sc.parent && sc.nonlocals.count(n)) {
            int idx = sc.parent->slots.at(n);
            return [idx](Frame& f, Value v) { f.root->locals[idx] = std::move(v); };
        }
        auto it = sc.slots.find(n);
        if (it == sc.slots.end()) throw err_at(line, "internal: no slot for '" + n + "'");
        int idx = it->second;
        return [idx](Frame& f, Value v) { f.locals[idx] = std::move(v); };
    }

    // ---- expressions ----
    EvalFn compile_expr(const ExprP& e, Scope& sc) {
        if (auto c = const_value_in(e, sc)) {
            Value v = *c;
            return [v](Frame&) { return v; };
        }
        switch (e->k) {
            case Expr::K::Const: { Value v = e->cval; return [v](Frame&) { return v; }; }
            case Expr::K::Name: return load_name(e->name, sc, e->line);
            case Expr::K::Unary: {
                EvalFn a = compile_expr(e->kids[0], sc);
                std::string op = e->name;
                return [a, op](Frame& f) {
                    Value v = a(f);
                    if (!v.is_number()) type_error("bad operand type for unary " + op + ": '" + v.type_name() + "'");
                    if (op == "+") return v.kind == Value::Kind::Bool ? Value::of_int(v.inum()) : v;
                    return v.kind == Value::Kind::Float ? Value::of_float(-v.d) : Value::of_int(-v.inum());
                };
            }
            case Expr::K::Bin: {
                EvalFn a = compile_expr(e->kids[0], sc), b = compile_expr(e->kids[1], sc);
                std::string op = e->name;
                int line = e->line;
                return [a, b, op, line](Frame& f) {
                    Value x = a(f), y = b(f);
                    return binary_op(op, x, y);
                    (void)line;
                };
            }
            case Expr::K::And: {
                EvalFn a = compile_expr(e->kids[0], sc), b = compile_expr(e->kids[1], sc);
                return [a, b](Frame& f) { Value x = a(f); return x.truthy() ? b(f) : x; };
            }
            case Expr::K::Or: {
                EvalFn a = compile_expr(e->kids[0], sc), b = compile_expr(e->kids[1], sc);
                return [a, b](Frame& f) { Value x = a(f); return x.truthy() ? x : b(f); };
            }
            case Expr::K::Not: {
                EvalFn a = compile_expr(e->kids[0], sc);
                return [a](Frame& f) { return Value::of_bool(!a(f).truthy()); };
            }
            case Expr::K::IfExp: {
                EvalFn c = compile_expr(e->kids[0], sc), a = compile_expr(e->kids[1], sc), b = compile_expr(e->kids[2], sc);
                return [c, a, b](Frame& f) { return c(f).truthy() ? a(f) : b(f); };
            }
            case Expr::K::Compare: {
                std::vector<EvalFn> parts;
                for (const auto& k : e->kids) parts.push_back(compile_expr(k, sc));
                std::vector<std::string> ops = e->ops;
                return [parts, ops](Frame& f) {
                    Value left = parts[0](f);
                    for (size_t k = 0; k < ops.size(); ++k) {
                        Value right = parts[k + 1](f);
                        const std::string& op = ops[k];
                        bool ok;
                        if (op == "==") ok = values_equal(left, right);
                        else if (op == "!=") ok = !values_equal(left, right);
                        else if (op == "in") ok = contains_value(right, left);
                        else if (op == "not in") ok = !contains_value(right, left);
                        else if (op == "is") ok = (left.kind == Value::Kind::None && right.kind == Value::Kind::None) ||
                                                  (left.kind == right.kind && values_equal(left, right) &&
                                                   left.kind != Value::Kind::List);
                        else if (op == "is not") ok = !((left.kind == Value::Kind::None && right.kind == Value::Kind::None) ||
                                                        (left.kind == right.kind && values_equal(left, right) &&
                                                         left.kind != Value::Kind::List));
                        else {
                            int c = compare_values(left, right, op);
                            ok = op == "<" ? c < 0 : op == ">" ? c > 0 : op == "<=" ? c <= 0 : c >= 0;
                        }
                        if (!ok) return Value::of_bool(false);
                        left = std::move(right);
                    }
                    return Value::of_bool(true);
                };
            }
            case Expr::K::ListE: case Expr::K::TupleE: {
                std::vector<EvalFn> items;
                for (const auto& k : e->kids) items.push_back(compile_expr(k, sc));
                bool tuple = e->k == Expr::K::TupleE;
                return [items, tuple](Frame& f) {
                    List out;
                    out.reserve(items.size());
                    for (const auto& it : items) out.push_back(it(f));
                    return Value::of_list(std::move(out), tuple);
                };
            }
            case Expr::K::Subscript: {
                EvalFn a = compile_expr(e->kids[0], sc), idx = compile_expr(e->kids[1], sc);
                return [a, idx](Frame& f) {
                    Value v = a(f), k = idx(f);
                    if (!k.is_integral()) type_error(std::string("indices must be integers, not ") + k.type_name());
                    long long i = k.inum();
                    if (v.kind == Value::Kind::List) {
                        long long n = (long long)v.l->size();
                        if (i < 0) i += n;
                        if (i < 0 || i >= n) throw std::runtime_error("IndexError: index out of range");
                        return (*v.l)[i];
                    }
                    if (v.kind == Value::Kind::Str) {
                        long long n = (long long)v.s.size();
                        if (i < 0) i += n;
                        if (i < 0 || i >= n) throw std::runtime_error("IndexError: string index out of range");
                        return Value::of_str(std::string(1, v.s[i]));
                    }
                    type_error(std::string("'") + v.type_name() + "' object is not subscriptable");
                };
            }
            case Expr::K::Call: return compile_call(e, sc);
            case Expr::K::Attr: {
                if (e->name != "x" && e->name != "y") {
                    throw err_at(e->line, "only .x and .y (of a mouse position) are supported by the native path");
                }
                size_t idx = e->name == "x" ? 0 : 1;
                const ExprP& obj = e->kids[0];
                EvalFn base;
                bool shadowed = obj->k == Expr::K::Name &&
                                (sc.slots.count(obj->name) || (sc.parent && sc.parent->slots.count(obj->name)));
                if (obj->k == Expr::K::Name && obj->name == "getMousePosition" && !shadowed && !funcs_.count(obj->name)) {
                    // getMousePosition.x -- the live position, no call needed
                    base = [](Frame& f) { return mouse_point(*f.rt); };
                } else {
                    base = compile_expr(obj, sc);
                }
                std::string attr = e->name;
                return [base, idx, attr](Frame& f) {
                    Value v = base(f);
                    if (v.kind == Value::Kind::List && v.is_point && v.l->size() == 2) return (*v.l)[idx];
                    throw std::runtime_error(std::string("AttributeError: '") + v.type_name() + "' object has no attribute '" +
                                             attr + "' (.x/.y work on a mouse position)");
                };
            }
        }
        throw err_at(e->line, "internal: unknown expression");
    }

    std::optional<Value> const_value_in(const ExprP& e, Scope& sc) const {
        // literal, key name (unless shadowed), or a list/tuple of those
        if (e->k == Expr::K::Const) return e->cval;
        if (e->k == Expr::K::Name) {
            if (sc.slots.count(e->name) || (sc.parent && sc.parent->slots.count(e->name))) return std::nullopt;
            if (auto code = key_constant(e->name)) return Value::of_int(*code);
            return std::nullopt;
        }
        if (e->k == Expr::K::ListE || e->k == Expr::K::TupleE) {
            List out;
            for (const auto& k : e->kids) {
                auto v = const_value_in(k, sc);
                if (!v) return std::nullopt;
                out.push_back(*v);
            }
            return Value::of_list(std::move(out), e->k == Expr::K::TupleE);
        }
        return std::nullopt;
    }

    Arg compile_arg(const ExprP& e, Scope& sc) {
        Arg a;
        if (auto c = const_value_in(e, sc)) { a.c = *c; return a; }
        a.is_const = false;
        a.fn = compile_expr(e, sc);
        return a;
    }

    struct CallArgs {
        std::vector<Arg> pos;
        std::unordered_map<std::string, Arg> kw;
        std::string fn;
        int line = 0;
        void allow(std::initializer_list<const char*> names) const {
            for (const auto& [k, _] : kw) {
                bool ok = false;
                for (const char* n : names) if (k == n) { ok = true; break; }
                if (!ok) throw err_at(line, fn + "() got an unexpected keyword argument '" + k + "'");
            }
        }
        Arg at(size_t i, const char* name, std::optional<Value> fallback = std::nullopt) const {
            auto it = kw.find(name);
            if (it != kw.end()) return it->second;
            if (i < pos.size()) return pos[i];
            if (fallback) { Arg a; a.c = *fallback; return a; }
            throw err_at(line, fn + "() missing argument '" + name + "'");
        }
        void max_pos(size_t n) const {
            if (pos.size() > n) throw err_at(line, fn + "() takes at most " + std::to_string(n) + " positional argument" +
                                                   (n == 1 ? "" : "s"));
        }
    };

    template <class F>
    static void spawn_background(F f) {
        std::thread([f = std::move(f)]() mutable {
            try { f(); } catch (...) {}
        }).detach();
    }

    EvalFn compile_call(const ExprP& e, Scope& sc) {
        const std::string& n = e->name;
        CallArgs c;
        c.fn = n;
        c.line = e->line;
        for (const auto& k : e->kids) c.pos.push_back(compile_arg(k, sc));
        for (const auto& [k, v] : e->kwargs) {
            if (c.kw.count(k)) throw err_at(e->line, n + "() got multiple values for '" + k + "'");
            c.kw[k] = compile_arg(v, sc);
        }
        bool shadowed = sc.slots.count(n) || (sc.parent && sc.parent->slots.count(n));
        if (shadowed) throw err_at(e->line, "'" + n + "' is a variable, not something you can call");

        // ---- this macro's own functions ----
        auto fit = funcs_.find(n);
        if (fit != funcs_.end()) return compile_user_call(fit->second, c);

        // ---- primitives ----
        if (auto op = compile_primitive(n, c)) return *op;

        // ---- builtins ----
        if (auto op = compile_builtin(n, c)) return *op;

        // ---- another macro (or custom block), looked up at CALL time ----
        if (!c.kw.empty()) {
            throw err_at(e->line, "keyword arguments aren't supported when calling another macro from a "
                                  "python_off macro");
        }
        std::vector<Arg> all = c.pos;
        std::string callee = n;
        return [all, callee](Frame& f) {
            std::vector<std::string> call_args;
            call_args.reserve(all.size());
            for (const auto& s : all) call_args.push_back(s.get(f).str());
            f.reg->get(callee)->run(*f.rt, *f.reg, call_args); // throws a clear "failed to compile" if unknown
            return Value::none();
        };
    }

    EvalFn compile_user_call(std::shared_ptr<FuncObj> fo, const CallArgs& c) {
        // bind args to params at compile time
        size_t np = fo->params.size();
        if (c.pos.size() > np) throw err_at(c.line, fo->name + "() takes " + std::to_string(np) + " argument" +
                                                    (np == 1 ? "" : "s") + " but " + std::to_string(c.pos.size()) + " were given");
        std::vector<std::optional<Arg>> bound(np);
        for (size_t k = 0; k < c.pos.size(); ++k) bound[k] = c.pos[k];
        for (const auto& [name, a] : c.kw) {
            auto it = std::find(fo->params.begin(), fo->params.end(), name);
            if (it == fo->params.end()) throw err_at(c.line, fo->name + "() got an unexpected keyword argument '" + name + "'");
            size_t k = it - fo->params.begin();
            if (bound[k]) throw err_at(c.line, fo->name + "() got multiple values for '" + name + "'");
            bound[k] = a;
        }
        std::weak_ptr<FuncObj> weak = fo;
        std::string fname = fo->name;
        int line = c.line;
        return [weak, bound, fname, line](Frame& f) -> Value {
            auto fn = weak.lock();
            if (!fn) return Value::none();
            if (f.depth > 200) throw std::runtime_error("RecursionError: " + fname + "() called itself too deeply");
            Frame nf;
            nf.locals.assign(fn->nlocals, Value::unset());
            nf.root = f.root;
            nf.rt = f.rt;
            nf.reg = f.reg;
            nf.depth = f.depth + 1;
            for (size_t k = 0; k < bound.size(); ++k) {
                if (bound[k]) nf.locals[k] = bound[k]->get(f);
                else if (k < fn->defaults.size() && fn->defaults[k]) nf.locals[k] = fn->defaults[k](*f.root);
                else throw std::runtime_error("line " + std::to_string(line) + ": " + fname +
                                              "() missing argument '" + fn->params[k] + "'");
            }
            for (const auto& s : fn->body) {
                if (s(nf) == Flow::Return) break;
            }
            return nf.ret;
        };
    }

    std::optional<EvalFn> compile_primitive(const std::string& n, CallArgs& c) {
        auto none = [](auto&& f) { return f; };
        (void)none;
        if (n == "kd" || n == "ku") {
            c.allow({"key"});
            c.max_pos(1);
            Arg key = c.at(0, "key");
            if (n == "kd") return EvalFn([key](Frame& f) { kd(*f.rt, key.key_code(f)); return Value::none(); });
            return EvalFn([key](Frame& f) { ku(*f.rt, key.key_code(f)); return Value::none(); });
        }
        if (n == "tap") {
            c.allow({"key", "time_"});
            c.max_pos(2);
            Arg key = c.at(0, "key"), t = c.at(1, "time_", Value::of_float(0.1));
            return EvalFn([key, t](Frame& f) { tap(*f.rt, key.key_code(f), t.dbl(f)); return Value::none(); });
        }
        if (n == "combo") {
            c.allow({"time_"});
            Arg t = c.at(SIZE_MAX, "time_", Value::of_float(0.1));
            std::vector<Arg> keys = c.pos;
            return EvalFn([keys, t](Frame& f) {
                std::vector<int> codes;
                codes.reserve(keys.size());
                for (const auto& k : keys) codes.push_back(k.key_code(f));
                combo_fn(*f.rt, codes, t.dbl(f));
                return Value::none();
            });
        }
        if (n == "type") {
            c.allow({"text", "time_per_letter", "async_"});
            c.max_pos(3);
            Arg text = c.at(0, "text"), tpl = c.at(1, "time_per_letter", Value::of_float(0.05)),
                async_ = c.at(2, "async_", Value::of_bool(false));
            return EvalFn([text, tpl, async_](Frame& f) {
                std::string s = text.text(f);
                double t = tpl.dbl(f);
                Runtime& rt = *f.rt;
                if (async_.flag(f)) {
                    double mult = Runtime::speed_multiplier();
                    spawn_background([&rt, s, t, mult] { Runtime::speed_multiplier() = mult; type_text_fn(rt, s, t); });
                } else {
                    type_text_fn(rt, s, t);
                }
                return Value::none();
            });
        }
        if (n == "move_mouse") {
            c.allow({"x_pixels", "y_pixels", "time_", "easing", "async_", "move_to"});
            c.max_pos(6);
            // y may be omitted when x is a position: move_mouse(saved_pos).
            // A real saved position means "go back there" -> absolute unless
            // move_to is given explicitly.
            bool move_to_given = c.kw.count("move_to") || c.pos.size() > 5;
            Arg x = c.at(0, "x_pixels"), y = c.at(1, "y_pixels", Value::none()), t = c.at(2, "time_", Value::of_float(0.25)),
                easing = c.at(3, "easing", Value::of_str("inout")), async_ = c.at(4, "async_", Value::of_bool(false)),
                move_to = c.at(5, "move_to", Value::of_bool(false));
            return EvalFn([=](Frame& f) {
                int xv, yv;
                bool point_default = false;
                Value yval = y.get(f);
                if (yval.kind == Value::Kind::None) {
                    Value xval = x.get(f);
                    if (xval.kind != Value::Kind::List || xval.l->size() != 2) {
                        type_error("move_mouse() needs x_pixels and y_pixels (or a position)");
                    }
                    xv = (*xval.l)[0].as_int();
                    yv = (*xval.l)[1].as_int();
                    point_default = xval.is_point;
                } else {
                    xv = x.integer(f);
                    yv = yval.as_int();
                }
                double tv = t.dbl(f);
                Value ev = easing.get(f);
                std::string es = ev.kind == Value::Kind::Str ? ev.s : std::string("inout");
                bool mt = move_to_given ? move_to.flag(f) : point_default;
                Runtime& rt = *f.rt;
                if (async_.flag(f)) {
                    double mult = Runtime::speed_multiplier();
                    spawn_background([&rt, xv, yv, tv, es, mt, mult] {
                        Runtime::speed_multiplier() = mult;
                        move_mouse_fn(rt, xv, yv, tv, es, mt);
                    });
                } else {
                    move_mouse_fn(rt, xv, yv, tv, es, mt);
                }
                return Value::none();
            });
        }
        if (n == "wheel") {
            c.allow({"amount"});
            c.max_pos(1);
            Arg amount = c.at(0, "amount");
            return EvalFn([amount](Frame& f) { wheel(*f.rt, amount.integer(f)); return Value::none(); });
        }
        if (n == "wait") {
            c.allow({"time_", "precise"});
            c.max_pos(2);
            Arg t = c.at(0, "time_"), precise = c.at(1, "precise", Value::of_bool(false));
            return EvalFn([t, precise](Frame& f) {
                wait_fn(*f.rt, t.dbl(f), precise.flag(f));
                return Value::none();
            });
        }
        if (n == "speed") {
            c.allow({"multiplier"});
            c.max_pos(1);
            Arg m = c.at(0, "multiplier");
            return EvalFn([m](Frame& f) { speed_fn(*f.rt, m.dbl(f)); return Value::none(); });
        }
        if (n == "ignore") {
            c.allow({"what"});
            c.max_pos(1);
            Arg what = c.at(0, "what");
            return EvalFn([what](Frame& f) { ignore_fn(*f.rt, what.text(f)); return Value::none(); });
        }
        if (n == "ignore_keys") {
            c.allow({});
            std::vector<Arg> keys = c.pos;
            return EvalFn([keys](Frame& f) {
                std::vector<int> codes;
                for (const auto& k : keys) codes.push_back(k.key_code(f));
                ignore_keys_fn(*f.rt, codes);
                return Value::none();
            });
        }
        if (n == "actAs") {
            c.allow({});
            if (c.pos.size() < 2) throw err_at(c.line, "actAs() needs at least 2 arguments");
            std::vector<Arg> all = c.pos;
            return EvalFn([all](Frame& f) {
                std::vector<int> acting;
                for (size_t i = 2; i < all.size(); ++i) acting.push_back(all[i].key_code(f));
                act_as_fn(*f.rt, all[0].key_code(f), all[1].flag(f), acting);
                return Value::none();
            });
        }
        if (n == "checkpoint") {
            c.allow({});
            if (!c.pos.empty()) throw err_at(c.line, "checkpoint() takes no arguments");
            return EvalFn([](Frame&) { checkpoint_fn(); return Value::none(); });
        }
        if (n == "command") {
            c.allow({});
            if (c.pos.empty()) throw err_at(c.line, "command() needs a command");
            std::vector<Arg> all = c.pos;
            return EvalFn([all](Frame& f) {
                std::vector<std::string> extra;
                for (size_t i = 1; i < all.size(); ++i) extra.push_back(all[i].text(f));
                command_fn(format_command(all[0].text(f), extra));
                return Value::none();
            });
        }
        if (n == "getMousePosition") {
            c.allow({});
            if (!c.pos.empty()) throw err_at(c.line, "getMousePosition() takes no arguments");
            return EvalFn([](Frame& f) { return mouse_point(*f.rt); });
        }
        if (n == "getButtonsHeld") {
            c.allow({});
            if (!c.pos.empty()) throw err_at(c.line, "getButtonsHeld() takes no arguments");
            return EvalFn([](Frame& f) {
                List out;
                for (int code : get_buttons_held_fn(*f.rt)) out.push_back(Value::of_int(code));
                return Value::of_list(std::move(out));
            });
        }
        if (n == "waitForReactivation") {
            c.allow({"repress"});
            c.max_pos(1);
            Arg repress = c.at(0, "repress", Value::of_bool(false));
            return EvalFn([repress](Frame& f) {
                wait_for_reactivation(*f.rt, repress.flag(f));
                return Value::none();
            });
        }
        if (n == "waitForPress") {
            c.allow({"button", "repress"});
            c.max_pos(2);
            Arg button = c.at(0, "button", Value::none()), repress = c.at(1, "repress", Value::of_bool(false));
            return EvalFn([button, repress](Frame& f) {
                Value bv = button.get(f);
                int code = (bv.kind == Value::Kind::None || (bv.kind == Value::Kind::Str && is_any_key_word(bv.s)))
                               ? -1 : bv.as_key_code();
                return Value::of_int(wait_for_press_fn(*f.rt, code, repress.flag(f)));
            });
        }
        return std::nullopt;
    }

    static long long to_int_strict(const Value& v, const std::string& fn) {
        if (v.is_integral()) return v.inum();
        if (v.kind == Value::Kind::Float) {
            if (std::isnan(v.d) || std::isinf(v.d)) throw std::runtime_error("ValueError: cannot convert float to integer");
            return (long long)v.d;
        }
        if (v.kind == Value::Kind::Str) {
            std::string t = v.s;
            t.erase(0, t.find_first_not_of(" \t\n"));
            t.erase(t.find_last_not_of(" \t\n") + 1);
            size_t used = 0;
            long long r = 0;
            try { r = std::stoll(t, &used); } catch (...) { used = 0; }
            if (t.empty() || used != t.size()) {
                throw std::runtime_error("ValueError: invalid literal for " + fn + "(): " + Value::quote(v.s));
            }
            return r;
        }
        type_error(fn + "() argument must be a string or a number, not '" + v.type_name() + "'");
    }

    std::optional<EvalFn> compile_builtin(const std::string& n, CallArgs& c) {
        auto need = [&](size_t lo, size_t hi) {
            if (!c.kw.empty()) throw err_at(c.line, n + "() takes no keyword arguments here");
            if (c.pos.size() < lo || c.pos.size() > hi) {
                throw err_at(c.line, n + "() takes " + (lo == hi ? std::to_string(lo) : std::to_string(lo) + "-" +
                                     std::to_string(hi)) + " argument(s)");
            }
        };
        std::vector<Arg> a = c.pos;
        if (n == "int") {
            need(0, 1);
            return EvalFn([a](Frame& f) { return a.empty() ? Value::of_int(0) : Value::of_int(to_int_strict(a[0].get(f), "int")); });
        }
        if (n == "float") {
            need(0, 1);
            return EvalFn([a](Frame& f) {
                if (a.empty()) return Value::of_float(0);
                Value v = a[0].get(f);
                if (v.is_number()) return Value::of_float(v.num());
                if (v.kind == Value::Kind::Str) {
                    char* end = nullptr;
                    double d = std::strtod(v.s.c_str(), &end);
                    std::string rest = end ? std::string(end) : "";
                    if (end == v.s.c_str() || rest.find_first_not_of(" \t\n") != std::string::npos) {
                        throw std::runtime_error("ValueError: could not convert string to float: " + Value::quote(v.s));
                    }
                    return Value::of_float(d);
                }
                type_error(std::string("float() argument must be a string or a number, not '") + v.type_name() + "'");
            });
        }
        if (n == "str") { need(0, 1); return EvalFn([a](Frame& f) { return Value::of_str(a.empty() ? "" : a[0].get(f).str()); }); }
        if (n == "bool") { need(0, 1); return EvalFn([a](Frame& f) { return Value::of_bool(!a.empty() && a[0].get(f).truthy()); }); }
        if (n == "len") {
            need(1, 1);
            return EvalFn([a](Frame& f) {
                Value v = a[0].get(f);
                if (v.kind == Value::Kind::Str) return Value::of_int((long long)v.s.size());
                if (v.kind == Value::Kind::List) return Value::of_int((long long)v.l->size());
                type_error(std::string("object of type '") + v.type_name() + "' has no len()");
            });
        }
        if (n == "abs") {
            need(1, 1);
            return EvalFn([a](Frame& f) {
                Value v = a[0].get(f);
                if (v.kind == Value::Kind::Float) return Value::of_float(std::fabs(v.d));
                if (v.is_integral()) return Value::of_int(std::llabs(v.inum()));
                type_error(std::string("bad operand type for abs(): '") + v.type_name() + "'");
            });
        }
        if (n == "round") {
            need(1, 2);
            return EvalFn([a](Frame& f) {
                Value v = a[0].get(f);
                if (!v.is_number()) type_error("round() needs a number");
                if (a.size() == 1) return Value::of_int((long long)std::nearbyint(v.num()));
                double m = std::pow(10.0, (double)a[1].get(f).inum());
                return Value::of_float(std::nearbyint(v.num() * m) / m);
            });
        }
        if (n == "min" || n == "max") {
            if (!c.kw.empty() || c.pos.empty()) throw err_at(c.line, n + "() needs at least one argument");
            bool is_min = n == "min";
            return EvalFn([a, is_min](Frame& f) {
                List items;
                if (a.size() == 1) {
                    Value v = a[0].get(f);
                    if (v.kind != Value::Kind::List) type_error("min()/max() of a single value needs a list");
                    items = *v.l;
                } else for (const auto& x : a) items.push_back(x.get(f));
                if (items.empty()) throw std::runtime_error("ValueError: min()/max() of an empty list");
                Value best = items[0];
                for (size_t k = 1; k < items.size(); ++k) {
                    int cmp = compare_values(items[k], best, is_min ? "<" : ">");
                    if (is_min ? cmp < 0 : cmp > 0) best = items[k];
                }
                return best;
            });
        }
        if (n == "range") {
            need(1, 3);
            return EvalFn([a](Frame& f) {
                long long start = 0, stop, step = 1;
                auto arg = [&](size_t k) {
                    Value v = a[k].get(f);
                    if (!v.is_integral()) type_error(std::string("'") + v.type_name() + "' object cannot be interpreted as an integer");
                    return v.inum();
                };
                if (a.size() == 1) stop = arg(0);
                else { start = arg(0); stop = arg(1); if (a.size() == 3) step = arg(2); }
                if (step == 0) throw std::runtime_error("ValueError: range() step must not be zero");
                List out;
                for (long long k = start; step > 0 ? k < stop : k > stop; k += step) out.push_back(Value::of_int(k));
                return Value::of_list(std::move(out));
            });
        }
        if (n == "list") {
            need(0, 1);
            return EvalFn([a](Frame& f) {
                if (a.empty()) return Value::of_list({});
                Value v = a[0].get(f);
                if (v.kind == Value::Kind::List) return Value::of_list(*v.l);
                if (v.kind == Value::Kind::Str) {
                    List out;
                    for (char ch : v.s) out.push_back(Value::of_str(std::string(1, ch)));
                    return Value::of_list(std::move(out));
                }
                type_error(std::string("'") + v.type_name() + "' object is not iterable");
            });
        }
        if (n == "print") {
            if (!c.kw.empty()) throw err_at(c.line, "print() keyword arguments aren't supported by the native path");
            return EvalFn([a](Frame& f) {
                std::string line;
                for (size_t k = 0; k < a.size(); ++k) { if (k) line += " "; line += a[k].get(f).str(); }
                std::fprintf(stderr, "%s\n", line.c_str());
                return Value::none();
            });
        }
        return std::nullopt;
    }

    // ---- statements ----
    std::vector<ExecFn> compile_block(const Block& b, Scope& sc, bool in_func, bool) {
        std::vector<ExecFn> out;
        for (const auto& s : b) {
            if (auto fn = compile_stmt(s, sc, in_func)) out.push_back(std::move(*fn));
        }
        return out;
    }

    static Flow run_block(const std::vector<ExecFn>& body, Frame& f) {
        for (const auto& s : body) {
            Flow fl = s(f);
            if (fl != Flow::Normal) return fl;
        }
        return Flow::Normal;
    }

    [[noreturn]] static void rethrow_with_line(const std::runtime_error& exc, int line) {
        std::string msg = exc.what();
        if (msg.rfind("line ", 0) == 0) throw exc;
        throw std::runtime_error("line " + std::to_string(line) + ": " + msg);
    }

    static ExecFn with_line(ExecFn fn, int line) {
        return [fn, line](Frame& f) -> Flow {
            try {
                return fn(f);
            } catch (const std::runtime_error& exc) {
                std::string msg = exc.what();
                if (msg.rfind("line ", 0) == 0) throw;
                throw std::runtime_error("line " + std::to_string(line) + ": " + msg);
            }
        };
    }

    std::optional<ExecFn> compile_stmt(const StmtP& s, Scope& sc, bool in_func) {
        int line = s->line;
        switch (s->k) {
            case Stmt::K::Pass: case Stmt::K::Nonlocal: case Stmt::K::Def:
                return std::nullopt;
            case Stmt::K::Break:
                if (!loop_depth_) throw err_at(line, "'break' outside a loop");
                return ExecFn([](Frame&) { return Flow::Break; });
            case Stmt::K::Continue:
                if (!loop_depth_) throw err_at(line, "'continue' outside a loop");
                return ExecFn([](Frame&) { return Flow::Continue; });
            case Stmt::K::Return: {
                // (top level: ends this run of the macro early, like the
                // embedded-Python path where the body is a function too)
                EvalFn v = s->e ? compile_expr(s->e, sc) : EvalFn{};
                return with_line([v](Frame& f) {
                    f.ret = v ? v(f) : Value::none();
                    return Flow::Return;
                }, line);
            }
            case Stmt::K::ExprS: {
                // hot path (every transcribed line): one closure, no wrapper
                EvalFn v = compile_expr(s->e, sc);
                return ExecFn([v, line](Frame& f) {
                    try {
                        v(f);
                    } catch (const std::runtime_error& exc) {
                        rethrow_with_line(exc, line);
                    }
                    f.rt->check_abort();
                    return Flow::Normal;
                });
            }
            case Stmt::K::Assign: {
                EvalFn v = compile_expr(s->e, sc);
                std::vector<std::string> names;
                for (const auto& t : s->targets) if (!t.empty()) names.push_back(t);
                bool unpack = s->targets.size() > 1;
                std::vector<std::function<void(Frame&, Value)>> stores;
                for (const auto& nme : names) stores.push_back(store_name(nme, sc, line));
                if (!unpack) {
                    auto st = stores[0];
                    return with_line([v, st](Frame& f) { st(f, v(f)); return Flow::Normal; }, line);
                }
                size_t count = names.size();
                return with_line([v, stores, count](Frame& f) {
                    Value val = v(f);
                    if (val.kind != Value::Kind::List) type_error(std::string("cannot unpack non-sequence ") + val.type_name());
                    if (val.l->size() != count) {
                        throw std::runtime_error("ValueError: expected " + std::to_string(count) + " values to unpack, got " +
                                                 std::to_string(val.l->size()));
                    }
                    List items = *val.l;
                    for (size_t k = 0; k < count; ++k) stores[k](f, items[k]);
                    return Flow::Normal;
                }, line);
            }
            case Stmt::K::AugAssign: {
                const std::string& nme = s->targets[0];
                auto name_expr = std::make_shared<Expr>();
                name_expr->k = Expr::K::Name; name_expr->name = nme; name_expr->line = line;
                EvalFn cur = load_name(nme, sc, line);
                EvalFn v = compile_expr(s->e, sc);
                auto st = store_name(nme, sc, line);
                std::string op = s->op;
                return with_line([cur, v, st, op](Frame& f) {
                    Value a = cur(f);
                    Value b = v(f);
                    st(f, binary_op(op, a, b));
                    return Flow::Normal;
                }, line);
            }
            case Stmt::K::If: {
                std::vector<EvalFn> conds;
                for (const auto& c : s->conds) conds.push_back(compile_expr(c, sc));
                std::vector<std::vector<ExecFn>> bodies;
                for (const auto& b : s->bodies) bodies.push_back(compile_block(b, sc, in_func, false));
                bool has_else = s->has_else;
                return with_line([conds, bodies, has_else](Frame& f) {
                    for (size_t k = 0; k < conds.size(); ++k) {
                        if (conds[k](f).truthy()) return run_block(bodies[k], f);
                    }
                    if (has_else) return run_block(bodies.back(), f);
                    return Flow::Normal;
                }, line);
            }
            case Stmt::K::While: {
                EvalFn cond = compile_expr(s->e, sc);
                ++loop_depth_;
                auto body = compile_block(s->bodies[0], sc, in_func, false);
                --loop_depth_;
                return with_line([cond, body](Frame& f) {
                    while (cond(f).truthy()) {
                        f.rt->check_abort();
                        Flow fl = run_block(body, f);
                        if (fl == Flow::Break) break;
                        if (fl == Flow::Return) return fl;
                    }
                    return Flow::Normal;
                }, line);
            }
            case Stmt::K::For: {
                std::vector<std::function<void(Frame&, Value)>> stores;
                for (const auto& t : s->targets) stores.push_back(store_name(t, sc, line));
                ++loop_depth_;
                auto body = compile_block(s->bodies[0], sc, in_func, false);
                --loop_depth_;
                // range(...) is iterated lazily -- `for _ in range(1000000)`
                // must not build a million-element list.
                const ExprP& it = s->e;
                bool is_range = it->k == Expr::K::Call && it->name == "range" && !funcs_.count("range") &&
                                it->kwargs.empty() && !it->kids.empty() && it->kids.size() <= 3 &&
                                !sc.slots.count("range") && stores.size() == 1;
                if (is_range) {
                    std::vector<EvalFn> args;
                    for (const auto& k : it->kids) args.push_back(compile_expr(k, sc));
                    auto st = stores[0];
                    return with_line([args, st, body](Frame& f) {
                        auto get = [&](size_t k) {
                            Value v = args[k](f);
                            if (!v.is_integral()) type_error(std::string("'") + v.type_name() +
                                                             "' object cannot be interpreted as an integer");
                            return v.inum();
                        };
                        long long start = 0, stop, step = 1;
                        if (args.size() == 1) stop = get(0);
                        else { start = get(0); stop = get(1); if (args.size() == 3) step = get(2); }
                        if (step == 0) throw std::runtime_error("ValueError: range() step must not be zero");
                        for (long long k = start; step > 0 ? k < stop : k > stop; k += step) {
                            f.rt->check_abort();
                            st(f, Value::of_int(k));
                            Flow fl = run_block(body, f);
                            if (fl == Flow::Break) break;
                            if (fl == Flow::Return) return fl;
                        }
                        return Flow::Normal;
                    }, line);
                }
                EvalFn iter = compile_expr(it, sc);
                size_t ntargets = stores.size();
                return with_line([iter, stores, body, ntargets](Frame& f) {
                    Value seq = iter(f);
                    List items;
                    if (seq.kind == Value::Kind::List) items = *seq.l;
                    else if (seq.kind == Value::Kind::Str) for (char ch : seq.s) items.push_back(Value::of_str(std::string(1, ch)));
                    else type_error(std::string("'") + seq.type_name() + "' object is not iterable");
                    for (const auto& item : items) {
                        f.rt->check_abort();
                        if (ntargets == 1) stores[0](f, item);
                        else {
                            if (item.kind != Value::Kind::List || item.l->size() != ntargets) {
                                throw std::runtime_error("ValueError: can't unpack loop item into " + std::to_string(ntargets) + " names");
                            }
                            for (size_t k = 0; k < ntargets; ++k) stores[k](f, (*item.l)[k]);
                        }
                        Flow fl = run_block(body, f);
                        if (fl == Flow::Break) break;
                        if (fl == Flow::Return) return fl;
                    }
                    return Flow::Normal;
                }, line);
            }
        }
        return std::nullopt;
    }

public:
    std::unordered_map<std::string, std::shared_ptr<FuncObj>>& funcs() { return funcs_; }
};

std::optional<Value> Compiler::const_value(const ExprP& e) const {
    return const_value_in(e, const_cast<Scope&>(top_));
}

} // namespace

class NativeMacroBody : public CompiledMacro {
public:
    NativeMacroBody(std::vector<ExecFn> body, std::vector<Value> defaults, int nlocals, std::string name,
                    std::unordered_map<std::string, std::shared_ptr<FuncObj>> funcs)
        : body_(std::move(body)), defaults_(std::move(defaults)), nlocals_(nlocals), name_(std::move(name)),
          funcs_(std::move(funcs)) {}

    void run(Runtime& rt, MacroRegistry& registry, const std::vector<std::string>& args) override {
        Frame f;
        f.rt = &rt;
        f.reg = &registry;
        f.root = &f;
        if (nlocals_) {
            f.locals.assign(nlocals_, Value::unset());
            for (size_t i = 0; i < defaults_.size(); ++i) {
                if (i >= args.size()) { f.locals[i] = defaults_[i]; continue; }
                const Value& d = defaults_[i];
                ArgKind want = d.kind == Value::Kind::Bool ? ArgKind::Bool
                             : d.kind == Value::Kind::Int ? ArgKind::Int
                             : d.kind == Value::Kind::Float ? ArgKind::Float : ArgKind::Other;
                CoercedArg c = coerce_macro_arg(args[i], want);
                switch (c.kind) {
                    case ArgKind::Int: f.locals[i] = Value::of_int(c.i); break;
                    case ArgKind::Float: f.locals[i] = Value::of_float(c.d); break;
                    case ArgKind::Bool: f.locals[i] = Value::of_bool(c.b); break;
                    default: f.locals[i] = Value::of_str(args[i]);
                }
            }
        }
        for (const auto& s : body_) {
            if (s(f) != Flow::Normal) break;
        }
    }

private:
    std::vector<ExecFn> body_;
    std::vector<Value> defaults_; // one per declared parameter (slots 0..n-1)
    int nlocals_;
    std::string name_;
    std::unordered_map<std::string, std::shared_ptr<FuncObj>> funcs_; // keeps function bodies alive
};

std::shared_ptr<CompiledMacro> compile_native_macro(const json& macro_def, MacroRegistry&) {
    std::string raw_body = json_str(macro_def, "code", "");
    std::string name = json_str(macro_def, "name", json_str(macro_def, "id", "macro"));
    bool simplified_names = json_bool(macro_def, "simplified_names", false);

    ExtractedBody extracted = extract_arguments_signature(raw_body); // throws MacroCompileError

    Compiler compiler(simplified_names, extracted.params);
    std::vector<Value> defaults;
    for (const auto& p : extracted.params) {
        auto toks = tokenize(p.default_source, 1);
        Parser dp(toks);
        Block b = dp.parse_module();
        if (b.size() != 1 || b[0]->k != Stmt::K::ExprS) throw err_at(1, "bad default for '" + p.name + "'");
        auto v = compiler.const_value(b[0]->e);
        if (!v) {
            throw err_at(1, "the default for '" + p.name + "' must be a literal number/string/True/False or a "
                            "KEY_*/BTN_* name");
        }
        defaults.push_back(*v);
    }

    int first_line = extracted.has_arguments_decl ? 2 : 1;
    Parser parser(tokenize(extracted.body, first_line));
    Block module = parser.parse_module();
    std::vector<ExecFn> body = compiler.compile_module(module);
    return std::make_shared<NativeMacroBody>(std::move(body), std::move(defaults), compiler.top_count(), name,
                                             compiler.funcs());
}

} // namespace puppetry
