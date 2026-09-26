#define PY_SSIZE_T_CLEAN
#include "python_embed.hpp"
#include <Python.h>
#include <sstream>
#include <thread>
#include <vector>
#include "keycodes.hpp"
#include "primitives.hpp"
#include "simplified_names.hpp"

namespace puppetry {

// Process-lifetime Runtime pointer -- every exposed primitive operates
// on this. Mirrors the original module's own bare-global design (its
// ui_keyboard/held/etc. were already daemon-lifetime globals); the
// embedding just needs one place to reach them from C-level Python
// callables, which don't get to carry C++ closures.
static Runtime* g_runtime = nullptr;
static PyThreadState* g_main_thread_state = nullptr;

// ---------------------------------------------------------------------
// Argument helpers
// ---------------------------------------------------------------------

static bool resolve_key_arg(PyObject* obj, int& out_code) {
    if (PyLong_Check(obj)) {
        out_code = (int)PyLong_AsLong(obj);
        return true;
    }
    if (PyUnicode_Check(obj)) {
        std::string name = PyUnicode_AsUTF8(obj);
        return resolve_key_name(name, out_code);
    }
    return false;
}

static bool parse_key_codes(PyObject* args_tuple, Py_ssize_t start, std::vector<int>& out) {
    for (Py_ssize_t i = start; i < PyTuple_Size(args_tuple); ++i) {
        int code;
        if (!resolve_key_arg(PyTuple_GetItem(args_tuple, i), code)) return false;
        out.push_back(code);
    }
    return true;
}

// ---------------------------------------------------------------------
// Exposed primitives
// ---------------------------------------------------------------------

static PyObject* py_kd(PyObject*, PyObject* args) {
    PyObject* key;
    if (!PyArg_ParseTuple(args, "O", &key)) return nullptr;
    int code;
    if (!resolve_key_arg(key, code)) { PyErr_SetString(PyExc_ValueError, "kd(): unknown key"); return nullptr; }
    Py_BEGIN_ALLOW_THREADS
    kd(*g_runtime, code);
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}

static PyObject* py_ku(PyObject*, PyObject* args) {
    PyObject* key;
    if (!PyArg_ParseTuple(args, "O", &key)) return nullptr;
    int code;
    if (!resolve_key_arg(key, code)) { PyErr_SetString(PyExc_ValueError, "ku(): unknown key"); return nullptr; }
    Py_BEGIN_ALLOW_THREADS
    ku(*g_runtime, code);
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}

static PyObject* py_tap(PyObject*, PyObject* args, PyObject* kwargs) {
    PyObject* key;
    double time_ = 0.1;
    static const char* kwlist[] = {"key", "time_", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|d", (char**)kwlist, &key, &time_)) return nullptr;
    int code;
    if (!resolve_key_arg(key, code)) { PyErr_SetString(PyExc_ValueError, "tap(): unknown key"); return nullptr; }
    Py_BEGIN_ALLOW_THREADS
    tap(*g_runtime, code, time_);
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}

static PyObject* py_combo(PyObject*, PyObject* args, PyObject* kwargs) {
    double time_ = 0.1;
    if (kwargs) {
        PyObject* t = PyDict_GetItemString(kwargs, "time_");
        if (t) time_ = PyFloat_AsDouble(t);
    }
    std::vector<int> keys;
    if (!parse_key_codes(args, 0, keys)) { PyErr_SetString(PyExc_ValueError, "combo(): unknown key"); return nullptr; }
    Py_BEGIN_ALLOW_THREADS
    combo_fn(*g_runtime, keys, time_);
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}

static PyObject* py_type(PyObject*, PyObject* args, PyObject* kwargs) {
    const char* text;
    double time_per_letter = 0.05;
    int async_ = 0;
    static const char* kwlist[] = {"text", "time_per_letter", "async_", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "s|dp", (char**)kwlist, &text, &time_per_letter, &async_))
        return nullptr;
    std::string text_str(text);
    if (async_) {
        std::thread([text_str, time_per_letter] { type_text_fn(*g_runtime, text_str, time_per_letter); }).detach();
    } else {
        Py_BEGIN_ALLOW_THREADS
        type_text_fn(*g_runtime, text_str, time_per_letter);
        Py_END_ALLOW_THREADS
    }
    Py_RETURN_NONE;
}

static PyObject* py_move_mouse(PyObject*, PyObject* args, PyObject* kwargs) {
    int x, y;
    double time_ = 0.25;
    const char* easing = "inout";
    int async_ = 0, move_to = 0;
    static const char* kwlist[] = {"x_pixels", "y_pixels", "time_", "easing", "async_", "move_to", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "ii|dsp p", (char**)kwlist,
                                      &x, &y, &time_, &easing, &async_, &move_to))
        return nullptr;
    std::string easing_str(easing);
    if (async_) {
        std::thread([x, y, time_, easing_str, move_to] {
            move_mouse_fn(*g_runtime, x, y, time_, easing_str, move_to != 0);
        }).detach();
    } else {
        Py_BEGIN_ALLOW_THREADS
        move_mouse_fn(*g_runtime, x, y, time_, easing_str, move_to != 0);
        Py_END_ALLOW_THREADS
    }
    Py_RETURN_NONE;
}

static PyObject* py_wheel(PyObject*, PyObject* args) {
    int amount;
    if (!PyArg_ParseTuple(args, "i", &amount)) return nullptr;
    wheel(*g_runtime, amount);
    Py_RETURN_NONE;
}

static PyObject* py_wait(PyObject*, PyObject* args, PyObject* kwargs) {
    double time_;
    int precise = 0;
    static const char* kwlist[] = {"time_", "precise", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "d|p", (char**)kwlist, &time_, &precise)) return nullptr;
    PyThreadState* save = PyEval_SaveThread();
    try {
        wait_fn(*g_runtime, time_, precise != 0);
    } catch (const MacroAborted&) {
        PyEval_RestoreThread(save);
        PyErr_SetString(PyExc_KeyboardInterrupt, "puppetry: aborted");
        return nullptr;
    }
    PyEval_RestoreThread(save);
    Py_RETURN_NONE;
}

static PyObject* py_speed(PyObject*, PyObject* args) {
    double multiplier;
    if (!PyArg_ParseTuple(args, "d", &multiplier)) return nullptr;
    speed_fn(*g_runtime, multiplier);
    Py_RETURN_NONE;
}

static PyObject* py_ignore(PyObject*, PyObject* args) {
    const char* what;
    if (!PyArg_ParseTuple(args, "s", &what)) return nullptr;
    try {
        ignore_fn(*g_runtime, what);
    } catch (const std::exception& exc) {
        PyErr_SetString(PyExc_ValueError, exc.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

static PyObject* py_ignore_keys(PyObject*, PyObject* args) {
    std::vector<int> codes;
    if (!parse_key_codes(args, 0, codes)) { PyErr_SetString(PyExc_ValueError, "ignore_keys(): unknown key"); return nullptr; }
    ignore_keys_fn(*g_runtime, codes);
    Py_RETURN_NONE;
}

static PyObject* py_act_as(PyObject*, PyObject* args) {
    if (PyTuple_Size(args) < 2) {
        PyErr_SetString(PyExc_TypeError, "actAs(key_pressing, ignore, *acting_keys) needs at least 2 arguments");
        return nullptr;
    }
    int key_pressing;
    if (!resolve_key_arg(PyTuple_GetItem(args, 0), key_pressing)) {
        PyErr_SetString(PyExc_ValueError, "actAs(): unknown key_pressing");
        return nullptr;
    }
    int ignore_val = PyObject_IsTrue(PyTuple_GetItem(args, 1));
    std::vector<int> acting_keys;
    if (!parse_key_codes(args, 2, acting_keys)) {
        PyErr_SetString(PyExc_ValueError, "actAs(): unknown acting key");
        return nullptr;
    }
    act_as_fn(*g_runtime, key_pressing, ignore_val != 0, acting_keys);
    Py_RETURN_NONE;
}

static PyObject* py_command(PyObject*, PyObject* args) {
    if (PyTuple_Size(args) < 1) { PyErr_SetString(PyExc_TypeError, "command() needs at least 1 argument"); return nullptr; }
    PyObject* cmd_obj = PyTuple_GetItem(args, 0);
    if (!PyUnicode_Check(cmd_obj)) { PyErr_SetString(PyExc_TypeError, "command(): cmd must be a string"); return nullptr; }
    std::string cmd = PyUnicode_AsUTF8(cmd_obj);
    std::vector<std::string> extra;
    for (Py_ssize_t i = 1; i < PyTuple_Size(args); ++i) {
        PyObject* item = PyTuple_GetItem(args, i);
        PyObject* str_obj = PyObject_Str(item);
        extra.push_back(str_obj ? PyUnicode_AsUTF8(str_obj) : "");
        Py_XDECREF(str_obj);
    }
    command_fn(format_command(cmd, extra));
    Py_RETURN_NONE;
}

static PyMethodDef kMethods[] = {
    {"kd", py_kd, METH_VARARGS, nullptr},
    {"ku", py_ku, METH_VARARGS, nullptr},
    {"tap", (PyCFunction)(void*)py_tap, METH_VARARGS | METH_KEYWORDS, nullptr},
    {"combo", (PyCFunction)(void*)py_combo, METH_VARARGS | METH_KEYWORDS, nullptr},
    {"type", (PyCFunction)(void*)py_type, METH_VARARGS | METH_KEYWORDS, nullptr},
    {"move_mouse", (PyCFunction)(void*)py_move_mouse, METH_VARARGS | METH_KEYWORDS, nullptr},
    {"wheel", py_wheel, METH_VARARGS, nullptr},
    {"wait", (PyCFunction)(void*)py_wait, METH_VARARGS | METH_KEYWORDS, nullptr},
    {"speed", py_speed, METH_VARARGS, nullptr},
    {"ignore", py_ignore, METH_VARARGS, nullptr},
    {"ignore_keys", py_ignore_keys, METH_VARARGS, nullptr},
    {"actAs", py_act_as, METH_VARARGS, nullptr},
    {"command", py_command, METH_VARARGS, nullptr},
    {nullptr, nullptr, 0, nullptr},
};

static PyModuleDef kModuleDef = {
    PyModuleDef_HEAD_INIT, "_puppetry_native", nullptr, -1, kMethods,
    nullptr, nullptr, nullptr, nullptr,
};

static PyObject* PyInit__puppetry_native() { return PyModule_Create(&kModuleDef); }

void python_embed_init(Runtime& rt) {
    g_runtime = &rt;
    PyImport_AppendInittab("_puppetry_native", &PyInit__puppetry_native);
    Py_Initialize();
    g_main_thread_state = PyEval_SaveThread(); // release the GIL -- each macro run acquires it itself
}

void python_embed_shutdown() {
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
        PyObject* py_args = PyTuple_New((Py_ssize_t)args.size());
        for (size_t i = 0; i < args.size(); ++i) {
            PyTuple_SetItem(py_args, (Py_ssize_t)i, PyUnicode_FromString(args[i].c_str()));
        }
        PyObject* result = PyObject_CallObject(func_, py_args);
        Py_DECREF(py_args);
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
    try {
        target->run(*g_runtime, *ctx->registry, str_args);
    } catch (const std::exception& exc) {
        PyErr_SetString(PyExc_RuntimeError, exc.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

void trampoline_capsule_destructor(PyObject* capsule) {
    delete (TrampolineContext*)PyCapsule_GetPointer(capsule, "puppetry.trampoline");
}

} // namespace

std::shared_ptr<CompiledMacro> compile_python_macro(const json& macro_def, MacroRegistry& registry,
                                                      const std::vector<std::string>& all_macro_names) {
    std::string raw_body = macro_def.value("code", "");
    if (raw_body.empty()) raw_body = "pass";
    std::string name = macro_def.value("name", macro_def.value("id", std::string("macro")));

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
    if (macro_def.value("ignore_keyboard", false)) ignore_targets.push_back("keyboard");
    if (macro_def.value("ignore_mouse_buttons", false)) ignore_targets.push_back("mouse_buttons");
    if (macro_def.value("ignore_mouse_movement", false)) ignore_targets.push_back("mouse_movement");

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

    if (macro_def.value("simplified_names", false)) {
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
