// Daemon entry point -- mirrors macro_daemon.py's `if __name__ ==
// "__main__"` block and main(). Runs as the systemd --user service;
// the `puppetry` CLI/GUI (PySide6, see gui/) talks to it purely over
// the control socket and never links against this binary.
#include <cstdio>
#include <iostream>
#include <iterator>
#include <sys/prctl.h>
#include <thread>
#include <vector>
#include "config.hpp"
#include "control_socket.hpp"
#include "dispatch.hpp"
#include "evdev_device.hpp"
#include "keycodes.hpp"
#include "macro.hpp"
#include "native_vm.hpp"
#include "pointer_accel.hpp"
#include "python_embed.hpp"
#include "runtime.hpp"
#include "simplified_names.hpp"

using namespace puppetry;

static void list_devices_mode() {
    std::printf("%-20s %s\n", "PATH", "NAME");
    for (const auto& dev : list_input_devices()) {
        std::printf("%-20s %s\n", dev.path.c_str(), dev.name.c_str());
    }
}

// Builds every uinput key/button code list -- mirrors _KEY_CODES /
// _MOUSE_BUTTON_CODES exactly, including the deliberate exclusion of
// the full BTN_* namespace (gamepad/joystick codes) that would trip
// udev's ID_INPUT_JOYSTICK classifier.
static std::vector<int> keyboard_key_codes() {
    std::vector<int> codes;
    for (const auto& [name, code] : key_name_to_code()) {
        if (name.rfind("KEY_", 0) == 0) codes.push_back(code);
    }
    return codes;
}

static std::vector<int> mouse_button_codes() {
    static const std::vector<std::string> wanted = {"BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE",
                                                      "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK"};
    std::vector<int> codes;
    for (const auto& name : wanted) {
        int code;
        if (resolve_key_name(name, code)) codes.push_back(code);
    }
    return codes;
}

static std::shared_ptr<CompiledMacro> compile_macro_body(const json& macro_def, MacroRegistry& registry,
                                                          const std::vector<std::string>& all_macro_names) {
    if (json_bool(macro_def, "python_on", true)) {
        return compile_python_macro(macro_def, registry, all_macro_names);
    }
    return compile_native_macro(macro_def, registry);
}

// `puppetry-daemon --check`: reads ONE macro's JSON object on stdin,
// compiles it with exactly the backend the daemon would use (embedded
// Python or the native fast path), prints "OK" or the error, and exits
// 0/1. The editor calls this on Save, so a macro that the daemon would
// reject never gets saved -- the only fully reliable check for
// python_off macros, whose grammar Python itself doesn't know.
static int check_mode() {
    std::string input((std::istreambuf_iterator<char>(std::cin)), std::istreambuf_iterator<char>());
    json macro_def;
    try {
        macro_def = json::parse(input);
    } catch (const std::exception& exc) {
        std::printf("invalid JSON: %s\n", exc.what());
        return 1;
    }
    Runtime rt; // never creates devices -- compiling doesn't touch them
    int rc = 0;
    MacroRegistry registry;
    {
        std::shared_ptr<CompiledMacro> compiled;
        try {
            if (json_bool(macro_def, "python_on", true)) {
                python_embed_init(rt);
                compiled = compile_python_macro(macro_def, registry, {});
            } else {
                compiled = compile_native_macro(macro_def, registry);
            }
            std::printf("OK\n");
        } catch (const std::exception& exc) {
            std::printf("%s\n", exc.what());
            rc = 1;
        }
    }
    python_embed_shutdown();
    return rc;
}

int main(int argc, char** argv) {
    if (argc > 1 && std::string(argv[1]) == "--list") {
        list_devices_mode();
        return 0;
    }
    if (argc > 1 && std::string(argv[1]) == "--check") {
        return check_mode();
    }
    if (argc > 1 && std::string(argv[1]) == "--dump-names") {
        // For the GUI's reference panels / alias pickers: the C++ tables
        // are the single source of truth, so the GUI never keeps its own
        // copy that could drift out of sync with what the daemon resolves.
        json out;
        out["simplified"] = json::object();
        for (const auto& [simple, real] : simplified_names_table()) out["simplified"][simple] = real;
        out["keys"] = json::array();
        for (const auto& [name, code] : key_name_to_code()) out["keys"].push_back(name);
        std::printf("%s\n", out.dump().c_str());
        return 0;
    }

    // Timer slack 1ns (default 50us): the kernel may otherwise delay
    // every sleep's wakeup by up to 50us to batch timers -- pure latency
    // for a daemon whose whole job is timing. Inherited by every thread
    // created after this point (all of them).
    prctl(PR_SET_TIMERSLACK, 1UL, 0, 0, 0);

    ensure_config_exists();
    json state = load_state();

    int abort_code;
    {
        std::string abort_key = json_str(state, "abort_key", "KEY_PAUSE");
        if (abort_key.empty() || !resolve_key_name(abort_key, abort_code)) {
            resolve_key_name("KEY_PAUSE", abort_code);
        }
    }

    auto kb_saved_path = state.contains("keyboard_path") && state["keyboard_path"].is_string()
                             ? std::optional<std::string>(state["keyboard_path"].get<std::string>()) : std::nullopt;
    auto kb_saved_name = state.contains("keyboard_name") && state["keyboard_name"].is_string()
                             ? std::optional<std::string>(state["keyboard_name"].get<std::string>()) : std::nullopt;
    auto mouse_saved_path = state.contains("mouse_path") && state["mouse_path"].is_string()
                                ? std::optional<std::string>(state["mouse_path"].get<std::string>()) : std::nullopt;
    auto mouse_saved_name = state.contains("mouse_name") && state["mouse_name"].is_string()
                                ? std::optional<std::string>(state["mouse_name"].get<std::string>()) : std::nullopt;

    // Resolve real devices BEFORE creating our own virtual output
    // devices, same ordering rationale as the Python version (our
    // virtual keyboard/mouse must never be a candidate during
    // auto-detect).
    ResolvedDevice keyboard = resolve_device("keyboard", kb_saved_path, kb_saved_name);
    ResolvedDevice mouse = resolve_device("mouse", mouse_saved_path, mouse_saved_name);

    // Pointer acceleration off for our own virtual mouse, BEFORE creating
    // it: KDE applies a device's saved settings when the device appears,
    // so writing the config first means a freshly-created virtual mouse
    // is already unaccelerated -- nothing to fix by hand in System
    // Settings, and synthetic motion lands where the macro asked. Opt out
    // with "disable_pointer_accel": false in state.json (Settings has a
    // checkbox for it). See pointer_accel.hpp for why this can't be done
    // on the uinput side.
    if (json_bool(state, "disable_pointer_accel", true)) {
        switch (ensure_flat_pointer_accel(kVirtualMouseName, kVirtualVendorId, kVirtualProductId)) {
            case AccelResult::Wrote:
                std::printf("Pointer acceleration: disabled for %s (wrote kcminputrc)\n", kVirtualMouseName);
                break;
            case AccelResult::AlreadySet:
                std::printf("Pointer acceleration: already disabled for %s\n", kVirtualMouseName);
                break;
            case AccelResult::NotKde:
                break; // not a KDE install -- nothing to write, nothing to say
            case AccelResult::Failed:
                std::fprintf(stderr, "Couldn't write kcminputrc to disable pointer acceleration -- "
                                      "synthetic mouse motion may be accelerated.\n");
                break;
        }
    }

    Runtime rt;
    try {
        rt.ui_keyboard.create(kVirtualKeyboardName, keyboard_key_codes(), false);
        rt.ui_mouse.create(kVirtualMouseName, mouse_button_codes(), true);
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "Failed to create virtual input devices: %s\n", exc.what());
        return 1;
    }

    if (keyboard.how != ResolveHow::NotFound) {
        std::printf("Keyboard: %s (%s)\n", keyboard.path.c_str(), keyboard.name.c_str());
    }
    if (mouse.how != ResolveHow::NotFound) {
        std::printf("Mouse: %s (%s)\n", mouse.path.c_str(), mouse.name.c_str());
    }
    std::printf("Abort hotkey code: %d\n", abort_code);

    if (keyboard.how == ResolveHow::NotFound || mouse.how == ResolveHow::NotFound) {
        std::fprintf(stderr, "Could not determine keyboard/mouse device, automatically or from "
                              "state.json -- set them manually via the GUI's Detect buttons, or "
                              "state.json directly.\n");
        return 1;
    }

    state["keyboard_path"] = keyboard.path;
    state["keyboard_name"] = keyboard.name;
    state["mouse_path"] = mouse.path;
    state["mouse_name"] = mouse.name;
    save_state(state);

    std::string active_profile = json_str(state, "active_profile", "profile_1");
    json profile;
    try {
        profile = load_profile(active_profile);
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "Couldn't read profile '%s' (%s) -- running with every macro disabled.\n",
                     active_profile.c_str(), exc.what());
        profile = json::object();
    }
    std::printf("Loaded profile: %s\n", json_str(profile, "name", active_profile).c_str());
    json enabled_map = (profile.contains("enabled") && profile["enabled"].is_object()) ? profile["enabled"] : json::object();

    json macros_doc = load_macros();
    json macro_defs = (macros_doc.contains("macros") && macros_doc["macros"].is_array()) ? macros_doc["macros"] : json::array();

    std::vector<std::string> all_macro_names;
    for (const auto& macro_def : macro_defs) {
        all_macro_names.push_back(sanitize_macro_name(json_str(macro_def, "name", "")));
    }

    python_embed_init(rt);

    MacroRegistry registry;
    std::vector<std::unique_ptr<Macro>> macros;
    for (const auto& macro_def : macro_defs) {
        try {
            auto m = std::make_unique<Macro>();
            m->id = json_str(macro_def, "id", "");
            m->name = json_str(macro_def, "name", m->id);
            m->enabled = json_bool(enabled_map, m->id.c_str(), false);
            m->repeat_mode = parse_repeat_mode(json_str(macro_def, "repeat_mode", "none"));
            m->trigger_edge = parse_trigger_edge(json_str(macro_def, "trigger_edge", "down"));
            if (macro_def.contains("combo") && macro_def["combo"].is_array()) {
                for (const auto& key_name : macro_def["combo"]) {
                    int code;
                    if (key_name.is_string() && resolve_key_name(key_name.get<std::string>(), code)) m->combo.push_back(code);
                }
            }
            m->func = compile_macro_body(macro_def, registry, all_macro_names);
            registry.set(sanitize_macro_name(m->name), m->func);
            macros.push_back(std::move(m));
        } catch (const std::exception& exc) {
            std::fprintf(stderr, "Skipping macro '%s': %s\n",
                          json_str(macro_def, "name", json_str(macro_def, "id", "?")).c_str(), exc.what());
        }
    }

    ControlSocketServer control(rt, registry, macros);
    try {
        control.start(control_socket_path().string());
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "Failed to start control socket: %s\n", exc.what());
        return 1;
    }

    InputDevice keyboard_dev, mouse_dev;
    if (!keyboard_dev.open(keyboard.path) || !mouse_dev.open(mouse.path)) {
        std::fprintf(stderr, "Failed to open resolved input devices for reading.\n");
        return 1;
    }

    std::thread kb_thread(watch_device, std::ref(rt), std::ref(registry), std::ref(macros),
                          std::ref(keyboard_dev), "keyboard", abort_code);
    std::thread mouse_thread(watch_device, std::ref(rt), std::ref(registry), std::ref(macros),
                             std::ref(mouse_dev), "mouse", abort_code);

    kb_thread.join();
    mouse_thread.join();

    // Shutdown ordering matters: drop every compiled macro (which may
    // hold a live PyObject*) before tearing down the interpreter -- see
    // PythonMacroBody's destructor comment.
    control.stop();
    macros.clear();
    registry.clear();
    python_embed_shutdown();
    return 0;
}
