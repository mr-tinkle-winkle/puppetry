#define PY_SSIZE_T_CLEAN
#include "python_embed.hpp"
#include <Python.h>
#include <cstdio>
#include <sstream>
#include <thread>
#include <vector>
#include "keycodes.hpp"
#include "primitives.hpp"
#include "simplified_names.hpp"

namespace puppetry {

// Process-lifetime Runtime pointer -- every exposed primitive operates
// on this (C-level Python callables can't carry C++ closures).
static Runtime* g_runtime = nullptr;
static PyThreadState* g_main_thread_state = nullptr;

// ---------------------------------------------------------------------
// Argument access for METH_FASTCALL | METH_KEYWORDS functions.
//
// HOT PATH: the first version used METH_VARARGS + PyArg_Parse*, which
// builds an argument tuple (and a kwargs dict) per call and runs a
// format-string parser. Fastcall hands us a plain C array instead.
// ---------------------------------------------------------------------

namespace {

struct FastArgs {
    PyObject* const* args;
    Py_ssize_t nargs;
    PyObject* kwnames;

    // Positional slot `pos`, else keyword `kw`, else nullptr.
    PyObject* get(Py_ssize_t pos, const char* kw) const {
        if (pos >= 0 && pos < nargs) return args[pos];
        if (kwnames && kw) {
            Py_ssize_t nk = PyTuple_GET_SIZE(kwnames);
            for (Py_ssize_t i = 0; i < nk; ++i) {
                if (PyUnicode_CompareWithASCIIString(PyTuple_GET_ITEM(kwnames, i), kw) == 0) return args[nargs + i];
            }
        }
        return nullptr;
    }

    // Rejects unknown keyword names (a typo like time=0.1 instead of
    // time_=0.1 would otherwise be silently ignored).
    bool check_kwargs(const char* fn, std::initializer_list<const char*> allowed) const {
        if (!kwnames) return true;
        Py_ssize_t nk = PyTuple_GET_SIZE(kwnames);
        for (Py_ssize_t i = 0; i < nk; ++i) {
            PyObject* name = PyTuple_GET_ITEM(kwnames, i);
            bool ok = false;
            for (const char* a : allowed) if (PyUnicode_CompareWithASCIIString(name, a) == 0) { ok = true; break; }
            if (!ok) {
                PyErr_Format(PyExc_TypeError, "%s() got an unexpected keyword argument '%U'", fn, name);
                return false;
            }
        }
        return true;
    }
};

bool resolve_key_arg(PyObject* obj, int& out_code) {
    if (PyLong_Check(obj)) {
        out_code = (int)PyLong_AsLong(obj);
        return true;
    }
    if (PyUnicode_Check(obj)) {
        const char* s = PyUnicode_AsUTF8(obj);
        return s && resolve_key_name(s, out_code);
    }
    return false;
}

bool key_arg(PyObject* obj, const char* fn, int& out) {
    if (!obj) { PyErr_Format(PyExc_TypeError, "%s() missing required argument 'key'", fn); return false; }
    if (!resolve_key_arg(obj, out)) {
        PyErr_Format(PyExc_ValueError, "%s(): unknown key %R", fn, obj);
        return false;
    }
    return true;
}

bool double_arg(PyObject* obj, double fallback, double& out) {
    if (!obj) { out = fallback; return true; }
    out = PyFloat_AsDouble(obj);
    return !(out == -1.0 && PyErr_Occurred());
}

bool bool_arg(PyObject* obj, bool fallback, bool& out) {
    if (!obj) { out = fallback; return true; }
    int r = PyObject_IsTrue(obj);
    if (r < 0) return false;
    out = r != 0;
    return true;
}

// Runs `f` (pure C++, never touches Python objects), translating C++
// exceptions into Python ones. They must NEVER propagate through
// CPython's C frames -- the first version let MacroAborted fly out of
// Py_BEGIN_ALLOW_THREADS blocks, which is undefined behavior (crash on
// abort mid-tap). The GIL is released only when `release` is set: for
// sub-200us work, releasing and re-taking it costs more than it buys.
template <class F>
PyObject* run_native(bool release, F&& f) {
    PyThreadState* ts = release ? PyEval_SaveThread() : nullptr;
    const char* err = nullptr;
    std::string msg;
    try {
        f();
    } catch (const MacroAborted&) {
        err = "abort";
    } catch (const std::exception& exc) {
        err = "error";
        msg = exc.what();
    }
    if (ts) PyEval_RestoreThread(ts);
    if (err) {
        if (err[0] == 'a') PyErr_SetString(PyExc_KeyboardInterrupt, "puppetry: aborted");
        else PyErr_SetString(PyExc_RuntimeError, msg.c_str());
        return nullptr;
    }
    Py_RETURN_NONE;
}

// Background work (async_=True) must swallow MacroAborted itself -- an
// exception escaping a std::thread calls std::terminate(), i.e. an abort
// during async typing used to kill the whole daemon.
template <class F>
void spawn_background(F f) {
    std::thread([f = std::move(f)]() mutable {
        try { f(); } catch (...) {}
    }).detach();
}

} // namespace

// ---------------------------------------------------------------------
// Exposed primitives
// ---------------------------------------------------------------------

#define FASTCALL_SIG(name) static PyObject* name(PyObject*, PyObject* const* args, Py_ssize_t nargs, PyObject* kwnames)
#define ARGS FastArgs a{args, nargs, kwnames}

FASTCALL_SIG(py_kd) {
    ARGS;
    if (!a.check_kwargs("kd", {"key"})) return nullptr;
    int code;
    if (!key_arg(a.get(0, "key"), "kd", code)) return nullptr;
    return run_native(false, [&] { kd(*g_runtime, code); });
}

FASTCALL_SIG(py_ku) {
    ARGS;
    if (!a.check_kwargs("ku", {"key"})) return nullptr;
    int code;
    if (!key_arg(a.get(0, "key"), "ku", code)) return nullptr;
    return run_native(false, [&] { ku(*g_runtime, code); });
}

FASTCALL_SIG(py_tap) {
    ARGS;
    if (!a.check_kwargs("tap", {"key", "time_"})) return nullptr;
    int code;
    double t;
    if (!key_arg(a.get(0, "key"), "tap", code) || !double_arg(a.get(1, "time_"), 0.1, t)) return nullptr;
    return run_native(!wait_is_short(t), [&] { tap(*g_runtime, code, t); });
}

FASTCALL_SIG(py_combo) {
    ARGS;
    if (!a.check_kwargs("combo", {"time_"})) return nullptr;
    double t;
    if (!double_arg(a.get(-1, "time_"), 0.1, t)) return nullptr;
    std::vector<int> keys;
    for (Py_ssize_t i = 0; i < nargs; ++i) {
        int code;
        if (!key_arg(args[i], "combo", code)) return nullptr;
        keys.push_back(code);
    }
    return run_native(!wait_is_short(t), [&] { combo_fn(*g_runtime, keys, t); });
}

FASTCALL_SIG(py_type) {
    ARGS;
    if (!a.check_kwargs("type", {"text", "time_per_letter", "async_"})) return nullptr;
    PyObject* text_obj = a.get(0, "text");
    if (!text_obj || !PyUnicode_Check(text_obj)) { PyErr_SetString(PyExc_TypeError, "type(): text must be a string"); return nullptr; }
    std::string text = PyUnicode_AsUTF8(text_obj);
    double tpl;
    bool async_;
    if (!double_arg(a.get(1, "time_per_letter"), 0.05, tpl) || !bool_arg(a.get(2, "async_"), false, async_)) return nullptr;
    if (async_) {
        // The background thread's speed() starts at 1.0 -- capture ours.
        double mult = Runtime::speed_multiplier();
        spawn_background([text, tpl, mult] { Runtime::speed_multiplier() = mult; type_text_fn(*g_runtime, text, tpl); });
        Py_RETURN_NONE;
    }
    return run_native(true, [&] { type_text_fn(*g_runtime, text, tpl); });
}

FASTCALL_SIG(py_move_mouse) {
    ARGS;
    if (!a.check_kwargs("move_mouse", {"x_pixels", "y_pixels", "time_", "easing", "async_", "move_to"})) return nullptr;
    PyObject* xo = a.get(0, "x_pixels");
    PyObject* yo = a.get(1, "y_pixels");
    if (!xo || !yo) { PyErr_SetString(PyExc_TypeError, "move_mouse() needs x_pixels and y_pixels"); return nullptr; }
    int x = (int)PyLong_AsLong(xo), y = (int)PyLong_AsLong(yo);
    if (PyErr_Occurred()) return nullptr;
    double t;
    bool async_, move_to;
    if (!double_arg(a.get(2, "time_"), 0.25, t) || !bool_arg(a.get(4, "async_"), false, async_) ||
        !bool_arg(a.get(5, "move_to"), false, move_to))
        return nullptr;
    std::string easing = "inout";
    if (PyObject* e = a.get(3, "easing")) {
        if (!PyUnicode_Check(e)) { PyErr_SetString(PyExc_TypeError, "move_mouse(): easing must be a string"); return nullptr; }
        easing = PyUnicode_AsUTF8(e);
    }
    if (async_) {
        double mult = Runtime::speed_multiplier();
        spawn_background([=] { Runtime::speed_multiplier() = mult; move_mouse_fn(*g_runtime, x, y, t, easing, move_to); });
        Py_RETURN_NONE;
    }
    // An instant relative move (transcribed raw mouse lines, up to ~1000/s)
    // is one write -- don't pay GIL churn for it.
    bool instant = !move_to && (easing == "none" || wait_is_short(t));
    return run_native(!instant, [&] { move_mouse_fn(*g_runtime, x, y, t, easing, move_to); });
}

FASTCALL_SIG(py_wheel) {
    ARGS;
    if (!a.check_kwargs("wheel", {"amount"})) return nullptr;
    PyObject* o = a.get(0, "amount");
    if (!o) { PyErr_SetString(PyExc_TypeError, "wheel() needs amount"); return nullptr; }
    int amount = (int)PyLong_AsLong(o);
    if (PyErr_Occurred()) return nullptr;
    return run_native(false, [&] { wheel(*g_runtime, amount); });
}

FASTCALL_SIG(py_wait) {
    ARGS;
    if (!a.check_kwargs("wait", {"time_", "precise"})) return nullptr;
    PyObject* to = a.get(0, "time_");
    if (!to) { PyErr_SetString(PyExc_TypeError, "wait() needs time_"); return nullptr; }
    double t;
    bool precise;
    if (!double_arg(to, 0, t) || !bool_arg(a.get(1, "precise"), false, precise)) return nullptr;
    return run_native(!wait_is_short(t), [&] { wait_fn(*g_runtime, t, precise); });
}

FASTCALL_SIG(py_speed) {
    ARGS;
    double m;
    PyObject* o = a.get(0, "multiplier");
    if (!o) { PyErr_SetString(PyExc_TypeError, "speed() needs multiplier"); return nullptr; }
    if (!double_arg(o, 1, m)) return nullptr;
    speed_fn(*g_runtime, m);
    Py_RETURN_NONE;
}

FASTCALL_SIG(py_checkpoint) {
    ARGS;
    if (!a.check_kwargs("checkpoint", {})) return nullptr;
    if (nargs != 0) { PyErr_SetString(PyExc_TypeError, "checkpoint() takes no arguments"); return nullptr; }
    checkpoint_fn(); // no-op -- see its comment in primitives.hpp
    Py_RETURN_NONE;
}

FASTCALL_SIG(py_ignore) {
    ARGS;
    PyObject* o = a.get(0, "what");
    if (!o || !PyUnicode_Check(o)) { PyErr_SetString(PyExc_TypeError, "ignore() needs a target string"); return nullptr; }
    std::string what = PyUnicode_AsUTF8(o);
    return run_native(false, [&] { ignore_fn(*g_runtime, what); });
}

FASTCALL_SIG(py_ignore_keys) {
    std::vector<int> codes;
    for (Py_ssize_t i = 0; i < nargs; ++i) {
        int code;
        if (!key_arg(args[i], "ignore_keys", code)) return nullptr;
        codes.push_back(code);
    }
    (void)kwnames;
    return run_native(false, [&] { ignore_keys_fn(*g_runtime, codes); });
}

FASTCALL_SIG(py_act_as) {
    (void)kwnames;
    if (nargs < 2) {
        PyErr_SetString(PyExc_TypeError, "actAs(key_pressing, ignore, *acting_keys) needs at least 2 arguments");
        return nullptr;
    }
    int key_pressing;
    if (!key_arg(args[0], "actAs", key_pressing)) return nullptr;
    int ignore_val = PyObject_IsTrue(args[1]);
    if (ignore_val < 0) return nullptr;
    std::vector<int> acting;
    for (Py_ssize_t i = 2; i < nargs; ++i) {
        int code;
        if (!key_arg(args[i], "actAs", code)) return nullptr;
        acting.push_back(code);
    }
    return run_native(false, [&] { act_as_fn(*g_runtime, key_pressing, ignore_val != 0, acting); });
}

FASTCALL_SIG(py_command) {
    (void)kwnames;
    if (nargs < 1 || !PyUnicode_Check(args[0])) { PyErr_SetString(PyExc_TypeError, "command(cmd, *args): cmd must be a string"); return nullptr; }
    std::string cmd = PyUnicode_AsUTF8(args[0]);
    std::vector<std::string> extra;
    for (Py_ssize_t i = 1; i < nargs; ++i) {
        PyObject* s = PyObject_Str(args[i]);
        if (!s) return nullptr;
        extra.push_back(PyUnicode_AsUTF8(s));
        Py_DECREF(s);
    }
    return run_native(false, [&] { command_fn(format_command(cmd, extra)); });
}

#define FC(name, fn) {name, (PyCFunction)(void (*)(void))fn, METH_FASTCALL | METH_KEYWORDS, nullptr}
static PyMethodDef kMethods[] = {
    FC("kd", py_kd), FC("ku", py_ku), FC("tap", py_tap), FC("combo", py_combo),
    FC("type", py_type), FC("move_mouse", py_move_mouse), FC("wheel", py_wheel),
    FC("wait", py_wait), FC("speed", py_speed), FC("checkpoint", py_checkpoint), FC("ignore", py_ignore),
    FC("ignore_keys", py_ignore_keys), FC("actAs", py_act_as), FC("command", py_command),
    {nullptr, nullptr, 0, nullptr},
};
#undef FC

static PyModuleDef kModuleDef = {
    PyModuleDef_HEAD_INIT, "_puppetry_native", nullptr, -1, kMethods,
    nullptr, nullptr, nullptr, nullptr,
};

static PyObject* PyInit__puppetry_native() { return PyModule_Create(&kModuleDef); }

// ---------------------------------------------------------------------
// Per-thread Python state (see MacroThreadHooks in macro.hpp).
// ---------------------------------------------------------------------

namespace {
thread_local int t_attach_depth = 0;
thread_local PyGILState_STATE t_gil_state;
thread_local PyThreadState* t_saved = nullptr;
} // namespace

void python_thread_enter() {
    if (!Py_IsInitialized()) return;
    if (t_attach_depth++ > 0) return;
    t_gil_state = PyGILState_Ensure();  // creates this thread's PyThreadState once
    t_saved = PyEval_SaveThread();      // ...then drops the GIL, keeping the state alive
}

void python_thread_exit() {
    if (t_attach_depth == 0 || --t_attach_depth > 0) return;
    if (!Py_IsInitialized()) return;
    PyEval_RestoreThread(t_saved);
    PyGILState_Release(t_gil_state);
    t_saved = nullptr;
}

void python_embed_init(Runtime& rt) {
    g_runtime = &rt;
    PyImport_AppendInittab("_puppetry_native", &PyInit__puppetry_native);
    Py_Initialize();
    g_main_thread_state = PyEval_SaveThread(); // release the GIL -- each macro run acquires it itself
    macro_thread_hooks().enter = &python_thread_enter;
    macro_thread_hooks().exit = &python_thread_exit;
}

void python_embed_shutdown() {
    macro_thread_hooks() = MacroThreadHooks{};
    if (g_main_thread_state) {
        PyEval_RestoreThread(g_main_thread_state);
        Py_Finalize();
        g_main_thread_state = nullptr;
    }
}

// ---------------------------------------------------------------------
// Per-macro namespace + compiled function
// ---------------------------------------------------------------------

namespace {

class GilGuard {
public:
    GilGuard() { state_ = PyGILState_Ensure(); }
    ~GilGuard() { PyGILState_Release(state_); }
private:
    PyGILState_STATE state_;
};

std::string fetch_python_error() {
    if (!PyErr_Occurred()) return "unknown Python error";
    PyObject *type, *value, *tb;
    PyErr_Fetch(&type, &value, &tb);
    PyErr_NormalizeException(&type, &value, &tb);
    std::string msg = "unknown Python error";
    if (value) {
        PyObject* str = PyObject_Str(value);
        if (str) { msg = PyUnicode_AsUTF8(str); Py_DECREF(str); }
    }
    Py_XDECREF(type);
    Py_XDECREF(value);
    Py_XDECREF(tb);
    return msg;
}

} // namespace

class PythonMacroBody : public CompiledMacro {
public:
    PythonMacroBody(PyObject* func, std::string name) : func_(func), name_(std::move(name)) {}
    ~PythonMacroBody() override {
        // A Macro's shared_ptr<CompiledMacro> can legitimately outlive
        // python_embed_shutdown() (e.g. static-duration containers,
        // or a caller that tears down the interpreter before dropping
        // its own references) -- Py_Finalize() invalidates every
        // PyObject*, so touching func_ (or even asking for the GIL)
        // after that is undefined behavior, not just a leak. Skip the
        // decref entirely in that case: the process is already
        // shutting the interpreter down, so the reference doesn't need
        // releasing, and there is no safe way left to release it.
        if (!Py_IsInitialized()) return;
        GilGuard guard;
        Py_XDECREF(func_);
    }

    // Borrowed-then-owned reference for the cross-macro trampoline fast
    // path in build_macro_refs() below -- caller must Py_DECREF it.
    PyObject* new_ref() const {
        GilGuard guard;
        Py_XINCREF(func_);
        return func_;
    }

    void run(Runtime& rt, MacroRegistry&, const std::vector<std::string>& args) override {
        (void)rt;
        GilGuard guard;
        PyObject* result;
        if (args.empty()) {
            result = PyObject_CallNoArgs(func_); // vectorcall, no tuple built
        } else {
            PyObject* py_args = PyTuple_New((Py_ssize_t)args.size());
            for (size_t i = 0; i < args.size(); ++i) {
                PyTuple_SetItem(py_args, (Py_ssize_t)i, PyUnicode_FromString(args[i].c_str()));
            }
            result = PyObject_CallObject(func_, py_args);
            Py_DECREF(py_args);
        }
        if (!result) {
            if (PyErr_ExceptionMatches(PyExc_KeyboardInterrupt)) {
                PyErr_Clear();
                throw MacroAborted{};
            }
            std::string err = fetch_python_error();
            std::fprintf(stderr, "Macro '%s' raised: %s\n", name_.c_str(), err.c_str());
            return;
        }
        Py_DECREF(result);
    }

private:
    PyObject* func_;
    std::string name_;
};

// A trampoline for calling another macro by its sanitized name, looked
// up in the registry AT CALL TIME (not compile time) -- mirrors the
// original's _FINAL_FUNCS scheme, which is what lets macro A call macro
// B call macro C to unlimited nesting depth regardless of compile
// order. Implemented as a Python-callable closure over `registry` and
// `sanitized_name` via a capsule, since a plain PyCFunction has no
// closure state of its own.
namespace {

struct TrampolineContext {
    MacroRegistry* registry;
    std::string name;
};

PyObject* trampoline_call(PyObject* self, PyObject* args, PyObject* kwargs) {
    auto* ctx = (TrampolineContext*)PyCapsule_GetPointer(self, "puppetry.trampoline");
    if (!ctx) return nullptr;

    std::shared_ptr<CompiledMacro> target;
    try {
        target = ctx->registry->get(ctx->name);
    } catch (const std::exception& exc) {
        PyErr_SetString(PyExc_NameError, exc.what());
        return nullptr;
    }

    if (auto* py_target = dynamic_cast<PythonMacroBody*>(target.get())) {
        // Full-fidelity Python-to-Python call: forward *args/**kwargs
        // exactly as received, including any arguments(...) defaults
        // and keyword usage -- e.g. Flick_and_Click(hits=5).
        PyObject* callable = py_target->new_ref();
        PyObject* result = PyObject_Call(callable, args, kwargs);
        Py_DECREF(callable);
        return result;
    }

    // Crossing into a native (python_off) macro: only positional,
    // string-convertible arguments make sense there -- native mode is
    // intentionally primitives-only, so kwargs from the Python side
    // have nowhere sensible to land.
    if (kwargs && PyDict_Size(kwargs) > 0) {
        PyErr_SetString(PyExc_TypeError,
                         "calling a native (python_off) macro with keyword arguments isn't supported");
        return nullptr;
    }
    std::vector<std::string> str_args;
    for (Py_ssize_t i = 0; i < PyTuple_Size(args); ++i) {
        PyObject* item = PyTuple_GetItem(args, i);
        PyObject* str_obj = PyObject_Str(item);
        str_args.push_back(str_obj ? PyUnicode_AsUTF8(str_obj) : "");
        Py_XDECREF(str_obj);
    }
    // run_native() translates MacroAborted too -- letting it escape
    // through this C frame into CPython would be undefined behavior.
    // The GIL is released: the native macro may itself call back into a
    // python_on macro, whose run() re-acquires it on this same thread.
    return run_native(true, [&] { target->run(*g_runtime, *ctx->registry, str_args); });
}

void trampoline_capsule_destructor(PyObject* capsule) {
    delete (TrampolineContext*)PyCapsule_GetPointer(capsule, "puppetry.trampoline");
}

} // namespace

std::shared_ptr<CompiledMacro> compile_python_macro(const json& macro_def, MacroRegistry& registry,
                                                      const std::vector<std::string>& all_macro_names) {
    std::string raw_body = json_str(macro_def, "code", "");
    if (raw_body.empty()) raw_body = "pass";
    std::string name = json_str(macro_def, "name", json_str(macro_def, "id", "macro"));

    ExtractedBody extracted = extract_arguments_signature(raw_body); // throws MacroCompileError

    std::ostringstream sig;
    if (extracted.has_arguments_decl) {
        for (size_t i = 0; i < extracted.params.size(); ++i) {
            if (i) sig << ", ";
            sig << extracted.params[i].name << "=" << extracted.params[i].default_source;
        }
    } else {
        sig << "*_args, **_kwargs";
    }

    std::vector<std::string> ignore_targets;
    if (json_bool(macro_def, "ignore_keyboard", false)) ignore_targets.push_back("keyboard");
    if (json_bool(macro_def, "ignore_mouse_buttons", false)) ignore_targets.push_back("mouse_buttons");
    if (json_bool(macro_def, "ignore_mouse_movement", false)) ignore_targets.push_back("mouse_movement");

    auto indent = [](const std::string& text, const std::string& prefix) {
        std::string out;
        std::istringstream iss(text);
        std::string line;
        bool first = true;
        while (std::getline(iss, line)) {
            if (!first) out += "\n";
            first = false;
            out += prefix + line;
        }
        return out;
    };

    std::string src;
    if (!ignore_targets.empty()) {
        std::string on_block, off_block;
        for (auto& t : ignore_targets) {
            on_block += "    ignore('" + t + "')\n";
            off_block += "        ignore('" + t + "')\n";
        }
        src = "def _macro(" + sig.str() + "):\n" + on_block + "    try:\n" +
              indent(extracted.body, "        ") + "\n    finally:\n" + off_block;
    } else {
        src = "def _macro(" + sig.str() + "):\n" + indent(extracted.body, "    ") + "\n";
    }

    GilGuard guard;

    PyObject* main_module = PyImport_AddModule("__main__"); // borrowed
    PyObject* native_module = PyImport_ImportModule("_puppetry_native");
    if (!native_module) throw std::runtime_error("failed to import _puppetry_native: " + fetch_python_error());

    PyObject* globals = PyDict_New();
    PyDict_SetItemString(globals, "__builtins__", PyModule_GetDict(main_module) ? PyDict_GetItemString(PyModule_GetDict(main_module), "__builtins__") : Py_None);

    // Copy every primitive from _puppetry_native into the macro's own
    // globals, bare (no module prefix) -- matches PRIMITIVES_NAMESPACE.
    PyObject* native_dict = PyModule_GetDict(native_module);
    PyDict_Update(globals, native_dict);
    PyObject* time_module = PyImport_ImportModule("time");
    if (time_module) { PyDict_SetItemString(globals, "time", time_module); Py_DECREF(time_module); }

    // Bare KEY_*/BTN_* identifiers.
    for (const auto& [name_str, code] : key_name_to_code()) {
        PyObject* val = PyLong_FromLong(code);
        PyDict_SetItemString(globals, name_str.c_str(), val);
        Py_DECREF(val);
    }

    if (json_bool(macro_def, "simplified_names", false)) {
        for (const auto& [simple_name, code] : build_simplified_namespace()) {
            PyObject* val = PyLong_FromLong(code);
            PyDict_SetItemString(globals, simple_name.c_str(), val);
            Py_DECREF(val);
        }
    }

    // Trampolines for every OTHER macro, by sanitized name -- built
    // fresh per compile since each needs its own capsule. `all_macro_names`
    // comes from the caller (which already has the full macro list),
    // not re-read from disk here -- see the header comment for why.
    static PyMethodDef trampoline_def = {"_trampoline", (PyCFunction)(void*)trampoline_call,
                                          METH_VARARGS | METH_KEYWORDS, nullptr};
    for (const auto& other_name : all_macro_names) {
        auto* ctx = new TrampolineContext{&registry, other_name};
        PyObject* capsule = PyCapsule_New(ctx, "puppetry.trampoline", trampoline_capsule_destructor);
        PyObject* func = PyCFunction_New(&trampoline_def, capsule);
        Py_DECREF(capsule);
        PyDict_SetItemString(globals, other_name.c_str(), func);
        Py_DECREF(func);
    }

    PyObject* local_ns = PyDict_New();
    std::string filename = "<macro:" + name + ">";
    PyObject* compiled_code = Py_CompileString(src.c_str(), filename.c_str(), Py_file_input);
    if (!compiled_code) {
        std::string err = fetch_python_error();
        Py_DECREF(globals);
        Py_DECREF(local_ns);
        Py_DECREF(native_module);
        throw std::runtime_error("SyntaxError compiling macro '" + name + "': " + err);
    }
    PyObject* exec_result = PyEval_EvalCode(compiled_code, globals, local_ns);
    Py_DECREF(compiled_code);
    if (!exec_result) {
        std::string err = fetch_python_error();
        Py_DECREF(globals);
        Py_DECREF(local_ns);
        Py_DECREF(native_module);
        throw std::runtime_error("error compiling macro '" + name + "': " + err);
    }
    Py_DECREF(exec_result);

    PyObject* func = PyDict_GetItemString(local_ns, "_macro"); // borrowed
    Py_XINCREF(func);
    Py_DECREF(globals);
    Py_DECREF(local_ns);
    Py_DECREF(native_module);

    if (!func) throw std::runtime_error("macro '" + name + "' compiled but produced no _macro function");

    return std::make_shared<PythonMacroBody>(func, name);
}

} // namespace puppetry
