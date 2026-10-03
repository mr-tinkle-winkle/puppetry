// Daemon entry point -- mirrors macro_daemon.py's `if __name__ ==
// "__main__"` block and main(). Runs as the systemd --user service;
// the `puppetry` CLI/GUI (PySide6, see gui/) talks to it purely over
// the control socket and never links against this binary.
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <iterator>
#include <sched.h>
#include <sys/prctl.h>
#include <set>
#include <memory>
#include <algorithm>
#include <thread>
#include <vector>
#include "config.hpp"
#include "control_socket.hpp"
#include "event_stream.hpp"
#include <csignal>
#include <sys/wait.h>
#include "dispatch.hpp"
#include "evdev_device.hpp"
#include "keycodes.hpp"
#include "macro.hpp"
#include "native_vm.hpp"
#include <unordered_map>
#include "pointer_accel.hpp"
#include "privacy.hpp"
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

// ---------------------------------------------------------------------
// Overlay helper (puppetry-overlay): OBS pages + the layered replay
// buffer file. Started only when overlay.json turns one of them on;
// restarted if it crashes; dies with the daemon (PDEATHSIG, and systemd
// stops the whole cgroup anyway).
// ---------------------------------------------------------------------
static bool overlay_wanted(const json& o) {
    for (const char* k : {"full", "controller", "simple", "replay"}) {
        if (o.contains(k) && o[k].is_object() && json_bool(o[k], "enabled", false)) return true;
    }
    return false;
}

static void run_overlay_helper() {
    const char* env = std::getenv("PUPPETRY_OVERLAY_CMD");
    std::string cmd = (env && *env) ? env : "puppetry-overlay";
    int failures = 0;
    while (failures < 20) {
        pid_t pid = fork();
        if (pid < 0) return;
        if (pid == 0) {
            prctl(PR_SET_PDEATHSIG, SIGTERM);
            execlp(cmd.c_str(), cmd.c_str(), "serve", (char*)nullptr);
            std::fprintf(stderr, "Couldn't start the overlay helper (%s): %s\n", cmd.c_str(), strerror(errno));
            _exit(127);
        }
        int status = 0;
        waitpid(pid, &status, 0);
        if (WIFEXITED(status) && (WEXITSTATUS(status) == 0 || WEXITSTATUS(status) == 127)) return;
        ++failures;
        std::fprintf(stderr, "Overlay helper exited (status %d); restarting in 3s\n", status);
        std::this_thread::sleep_for(std::chrono::seconds(3));
    }
}

int main(int argc, char** argv) {
    if (argc > 1 && std::string(argv[1]) == "--list") {
        list_devices_mode();
        return 0;
    }
    if (argc > 1 && std::string(argv[1]) == "--check") {
        return check_mode();
    }
    if (argc > 1 && std::string(argv[1]) == "--list-devices") {
        // Diagnostics: every input device the daemon can read, what it looks
        // like (keyboard / mouse / gamepad ...), and its vendor id.
        json out = json::array();
        for (const auto& d : list_input_devices()) {
            char vend[8];
            std::snprintf(vend, sizeof(vend), "%04x", device_vendor(d.path) & 0xffff);
            out.push_back({{"path", d.path}, {"name", d.name}, {"vendor", vend},
                           {"kind", describe_device(d.path)}, {"ours", is_our_virtual_device_name(d.name)}});
        }
        for (const auto& [path, why] : unreadable_input_devices())
            out.push_back({{"path", path}, {"unreadable", why}});
        std::printf("%s\n", out.dump(2).c_str());
        return 0;
    }

    if (argc > 1 && std::string(argv[1]) == "--dump-names") {
        // For the GUI's reference panels / alias pickers: the C++ tables
        // are the single source of truth, so the GUI never keeps its own
        // copy that could drift out of sync with what the daemon resolves.
        json out;
        out["simplified"] = json::object();
        for (const auto& [simple, real] : simplified_names_table()) out["simplified"][simple] = real;
        out["keys"] = json::array();
        out["codes"] = json::object();  // name -> code (the overlay helper maps stream codes back)
        for (const auto& [name, code] : key_name_to_code()) {
            out["keys"].push_back(name);
            out["codes"][name] = code;
        }
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

    // Optional real-time scheduling. Threads inherit their creator's policy,
    // so doing this here covers every macro thread, the device watchers and
    // the control socket.
    //
    // What it's for: wait() already lands within a microsecond of its
    // deadline at the median, but the TAIL is what gets noticed as playback
    // being slightly inconsistent -- and those outliers are whole
    // timeslices lost to other processes, which no amount of clever
    // sleeping can recover. Real-time priority is the only thing that
    // actually addresses them (Session 9 in the handoff records the
    // measurement that ruled out the alternative).
    //
    // SCHED_RR at priority 1 deliberately: the gentlest real-time setting
    // there is. It round-robins with any other RT task rather than
    // monopolizing, and the kernel's RT throttling (sched_rt_runtime_us,
    // 95% by default) guarantees normal processes still get time even if a
    // macro spins forever -- so a runaway macro can't lock the machine up.
    // Off by default, and silently skipped when the process isn't allowed
    // to ask (needs LimitRTPRIO from the service unit, which module.nix
    // grants).
    if (json_bool(state, "realtime_priority", false)) {
        struct sched_param sp {};
        sp.sched_priority = 1;
        if (sched_setscheduler(0, SCHED_RR, &sp) == 0) {
            std::printf("Scheduling: SCHED_RR priority 1 (real-time playback timing)\n");
        } else {
            std::fprintf(stderr, "Couldn't take real-time priority (%s) -- timing will be "
                                  "slightly less consistent under load. Needs LimitRTPRIO in the "
                                  "service unit.\n", strerror(errno));
        }
    }

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
    // /dev/uinput can lag behind login (module loading, udev permissions):
    // wait for it instead of exiting into a systemd restart loop.
    for (int tries = 0;; ++tries) {
        try {
            if (!rt.ui_keyboard.ok()) rt.ui_keyboard.create(kVirtualKeyboardName, keyboard_key_codes(), false);
            if (!rt.ui_mouse.ok()) rt.ui_mouse.create(kVirtualMouseName, mouse_button_codes(), true);
            break;
        } catch (const std::exception& exc) {
            if (tries == 0 || tries % 30 == 0)
                std::fprintf(stderr, "Failed to create virtual input devices: %s -- retrying every 2 s (is the "
                                      "uinput module loaded and are you in the uinput/input group?)\n", exc.what());
            if (std::getenv("PUPPETRY_NO_WAIT")) return 1;
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }

    std::printf("Abort hotkey code: %d\n", abort_code);

    // Not there yet (devices still coming up at login, a wireless receiver
    // asleep...): keep looking rather than exiting -- exiting only made systemd
    // restart us in a loop.
    for (int tries = 0; keyboard.how == ResolveHow::NotFound || mouse.how == ResolveHow::NotFound; ++tries) {
        if (tries == 0 || tries == 15) {
            std::fprintf(stderr, "Could not find the %s yet -- will keep looking every 2 s (set them with the "
                                  "GUI's Detect buttons if this never resolves). What this process can see:\n",
                         keyboard.how == ResolveHow::NotFound && mouse.how == ResolveHow::NotFound ? "keyboard or mouse"
                         : keyboard.how == ResolveHow::NotFound ? "keyboard" : "mouse");
            for (const auto& d : list_input_devices())
                std::fprintf(stderr, "  %s  %-40s  %s\n", d.path.c_str(), d.name.c_str(), describe_device(d.path).c_str());
            auto blocked = unreadable_input_devices();
            for (const auto& [path, why] : blocked)
                std::fprintf(stderr, "  %s  CAN'T OPEN: %s\n", path.c_str(), why.c_str());
            if (!blocked.empty())
                std::fprintf(stderr, "  -> %zu input device(s) can't be opened: permissions. The daemon's user needs "
                                      "to be in the 'input' group (log out and back in after adding it).\n",
                             blocked.size());
        }
        std::this_thread::sleep_for(std::chrono::seconds(2));
        if (keyboard.how == ResolveHow::NotFound) keyboard = resolve_device("keyboard", kb_saved_path, kb_saved_name);
        if (mouse.how == ResolveHow::NotFound) mouse = resolve_device("mouse", mouse_saved_path, mouse_saved_name);
        if (keyboard.how != ResolveHow::NotFound && mouse.how != ResolveHow::NotFound)
            std::printf("Found them: keyboard %s (%s), mouse %s (%s)\n", keyboard.path.c_str(), keyboard.name.c_str(),
                        mouse.path.c_str(), mouse.name.c_str());
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

    // Macro categories (macros.json "categories": [{"name", "enabled"}]):
    // a category switched off disables every macro in it, whatever the
    // profile says. Global, not per profile.
    std::unordered_map<std::string, bool> category_enabled;
    if (macros_doc.contains("categories") && macros_doc["categories"].is_array()) {
        for (const auto& c : macros_doc["categories"]) {
            if (c.is_object()) category_enabled[json_str(c, "name", "")] = json_bool(c, "enabled", true);
        }
    }

    std::vector<std::string> all_macro_names;
    for (const auto& macro_def : macro_defs) {
        all_macro_names.push_back(sanitize_macro_name(json_str(macro_def, "name", "")));
    }
    json custom_doc = load_custom_blocks();
    json custom_defs = (custom_doc.contains("blocks") && custom_doc["blocks"].is_array()) ? custom_doc["blocks"] : json::array();
    for (const auto& block_def : custom_defs) {
        all_macro_names.push_back(sanitize_macro_name(json_str(block_def, "name", "")));
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
            {
                auto cat = category_enabled.find(json_str(macro_def, "category", ""));
                if (cat != category_enabled.end() && !cat->second) m->enabled = false;
            }
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

    // Custom blocks: registered like macros (callable by name from any
    // macro), but they have no combo and never appear as triggerable.
    for (const auto& block_def : custom_defs) {
        std::string bname = json_str(block_def, "name", "");
        try {
            registry.set(sanitize_macro_name(bname), compile_macro_body(block_def, registry, all_macro_names));
        } catch (const std::exception& exc) {
            std::fprintf(stderr, "Skipping custom block '%s': %s\n", bname.c_str(), exc.what());
        }
    }

    // Built-in keybinds (overlay.json "hotkeys": {"screen": [...], "block": [...],
    // "share": [...]}, key names): each is a macro that runs a puppetry-overlay
    // subcommand, so it goes through the same combo matching as the user's own.
    {
        json ov = load_overlay_config();
        const char* env = std::getenv("PUPPETRY_OVERLAY_CMD");
        std::string tool = (env && *env) ? env : "puppetry-overlay";
        struct Builtin { const char* key; const char* name; const char* args; };
        for (const Builtin& b : {Builtin{"screen", "Puppetry: toggle the on-screen overlay", "screen toggle"},
                                 Builtin{"block", "Puppetry: block / unblock the input visualizer", "block toggle"},
                                 Builtin{"share", "Puppetry: show / hide the overlay for viewers (OBS)", "share toggle"}}) {
            if (!ov.contains("hotkeys") || !ov["hotkeys"].is_object() || !ov["hotkeys"].contains(b.key) ||
                !ov["hotkeys"][b.key].is_array() || ov["hotkeys"][b.key].empty())
                continue;
            auto m = std::make_unique<Macro>();
            m->id = std::string("__builtin_") + b.key;
            m->name = b.name;
            m->enabled = true;
            m->repeat_mode = parse_repeat_mode("none");
            m->trigger_edge = parse_trigger_edge("down");
            for (const auto& k : ov["hotkeys"][b.key]) {
                int code;
                if (k.is_string() && resolve_key_name(k.get<std::string>(), code)) m->combo.push_back(code);
            }
            if (m->combo.empty()) continue;
            json def = {{"id", m->id}, {"name", m->name}, {"python_on", false},
                        {"code", "command(\"'" + tool + "' " + b.args + "\")"}};
            try {
                m->func = compile_macro_body(def, registry, all_macro_names);
                std::printf("Keybind: %s\n", b.name);
                macros.push_back(std::move(m));
            } catch (const std::exception& exc) {
                std::fprintf(stderr, "Keybind '%s' skipped: %s\n", b.name, exc.what());
            }
        }
    }

    ControlSocketServer control(rt, registry, macros);
    try {
        control.start(control_socket_path().string());
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "Failed to start control socket: %s\n", exc.what());
        return 1;
    }

    // Live event stream for the overlay helper (inert with no clients).
    static EventStream event_stream;
    if (event_stream.start(event_socket_path().string(), &rt)) g_event_stream.store(&event_stream);
    // Blocking the input visualizer: the manual toggle (survives restarts) and ignored apps.
    privacy_load_manual(rt);
    std::thread([&rt] { run_privacy_watch(rt); }).detach();
    if (overlay_wanted(load_overlay_config())) {
        std::thread(run_overlay_helper).detach();
        std::printf("Overlay helper: starting (overlay.json)\n");
    }

    // Virtual controller (opt-in: an extra controller can shift player
    // numbers in games), for controller buttons and axis() in macros.
    if (json_bool(state, "virtual_controller", false)) {
        try {
            rt.ui_gamepad.create_gamepad(kVirtualGamepadName);
            rt.gamepad_enabled = true;
            std::printf("Virtual controller: on\n");
        } catch (const std::exception& exc) {
            std::fprintf(stderr, "Virtual controller: %s\n", exc.what());
        }
    }

    // Real controller: optional, and hot-pluggable -- looked for every few
    // seconds until one appears, watched until it's unplugged, then looked
    // for again. Its buttons work in combos, waitForPress(), getButtonsHeld();
    // its sticks/triggers in getAxis() and the overlay.
    {
        auto c_path = state.contains("controller_path") && state["controller_path"].is_string()
                          ? std::optional<std::string>(state["controller_path"].get<std::string>()) : std::nullopt;
        auto c_name = state.contains("controller_name") && state["controller_name"].is_string()
                          ? std::optional<std::string>(state["controller_name"].get<std::string>()) : std::nullopt;
        if (json_bool(state, "watch_controller", true)) {
            std::thread([&rt, &registry, &macros, c_path, c_name, abort_code] {
                bool announced_missing = false;
                while (true) {
                    ResolvedDevice c = resolve_device("controller", c_path, c_name);
                    if (c.how == ResolveHow::NotFound) {
                        if (!announced_missing) { std::printf("Controller: none found (will keep looking)\n"); announced_missing = true; }
                        std::this_thread::sleep_for(std::chrono::seconds(3));
                        continue;
                    }
                    if (!rt.claim_path(c.path)) {            // an extra-device watcher has it
                        std::this_thread::sleep_for(std::chrono::seconds(3));
                        continue;
                    }
                    InputDevice dev;
                    if (!dev.open(c.path)) {
                        rt.release_path(c.path);
                        std::this_thread::sleep_for(std::chrono::seconds(3));
                        continue;
                    }
                    std::printf("Controller: %s (%s)\n", c.path.c_str(), c.name.c_str());
                    announced_missing = false;
                    watch_device(rt, registry, macros, dev, "controller", abort_code);
                    rt.release_path(c.path);
                    // unplugged: release its held buttons and axes, then look again
                    {
                        std::lock_guard<std::mutex> lock(rt.held_mutex);
                        for (int code = 0x130; code <= 0x13e; ++code) rt.held.erase(code);
                        for (int code = 0x220; code <= 0x223; ++code) rt.held.erase(code);
                    }
                    {
                        std::lock_guard<std::mutex> lock(rt.axes_mutex);
                        rt.axes.clear();
                    }
                    std::printf("Controller disconnected\n");
                    std::this_thread::sleep_for(std::chrono::seconds(1));
                }
            }).detach();
        }
    }

    // Extra devices, watched alongside the main keyboard/mouse (read only:
    // their keys work in combos and show in the overlay, but ignore() can't
    // block them). Which ones: every Valve device (Steam Controller / Deck --
    // in desktop mode Steam turns the controller into its OWN keyboard and
    // mouse, which the main pair never sees) unless "watch_steam_devices" is
    // off; every device with "watch_all_devices"; and any listed in
    // "extra_devices" (by path or name). Re-scanned every 3 s (hotplug).
    {
        bool watch_steam = json_bool(state, "watch_steam_devices", true);
        bool watch_all = json_bool(state, "watch_all_devices", false);
        std::vector<std::string> listed;
        if (state.contains("extra_devices") && state["extra_devices"].is_array())
            for (const auto& e : state["extra_devices"]) if (e.is_string()) listed.push_back(e.get<std::string>());
        std::string kb_path = keyboard.path, ms_path = mouse.path;
        if (watch_steam || watch_all || !listed.empty()) {
            std::thread([&rt, &registry, &macros, watch_steam, watch_all, listed, kb_path, ms_path, abort_code] {
                auto active = std::make_shared<std::mutex>();
                auto open_paths = std::make_shared<std::set<std::string>>();
                while (true) {
                    for (const auto& d : list_input_devices()) {
                        if (is_our_virtual_device_name(d.name) || d.path == kb_path || d.path == ms_path) continue;
                        bool want = watch_all
                                 || (watch_steam && device_vendor(d.path) == 0x28de)
                                 || std::find(listed.begin(), listed.end(), d.path) != listed.end()
                                 || std::find(listed.begin(), listed.end(), d.name) != listed.end();
                        if (!want) continue;
                        {
                            std::lock_guard<std::mutex> lock(*active);
                            if (open_paths->count(d.path)) continue;
                            if (!rt.claim_path(d.path)) continue;      // the controller watcher has it
                            open_paths->insert(d.path);
                        }
                        // a gamepad node is the controller watcher's job when it picked it
                        bool pad = device_has_key(d.path, BTN_SOUTH) && device_has_abs(d.path, ABS_X);
                        std::string path = d.path, name = d.name;
                        std::thread([&rt, &registry, &macros, active, open_paths, path, name, pad, abort_code] {
                            InputDevice dev;
                            if (dev.open(path)) {
                                std::printf("Also watching: %s (%s)\n", path.c_str(), name.c_str());
                                watch_device(rt, registry, macros, dev, pad ? "extra_pad" : "extra", abort_code);
                                std::printf("Stopped watching: %s\n", path.c_str());
                            }
                            rt.release_path(path);
                            std::lock_guard<std::mutex> lock(*active);
                            open_paths->erase(path);
                        }).detach();
                    }
                    std::this_thread::sleep_for(std::chrono::seconds(3));
                }
            }).detach();
        }
    }

    // The main keyboard and mouse: watched for good. If one goes away (unplugged,
    // a receiver re-enumerating at login, a dock), look for it again and carry on
    // -- the daemon used to exit here, and systemd restarted it in a loop.
    auto keep_watching = [&rt, &registry, &macros, abort_code](std::string kind, ResolvedDevice first,
                                                               std::optional<std::string> saved_name) {
        ResolvedDevice cur = first;
        int misses = 0;
        while (true) {
            InputDevice dev;
            if (cur.how != ResolveHow::NotFound && dev.open(cur.path)) {
                std::printf("%s: %s (%s)\n", kind == "keyboard" ? "Keyboard" : "Mouse", cur.path.c_str(),
                            cur.name.c_str());
                watch_device(rt, registry, macros, dev, kind, abort_code);
                std::fprintf(stderr, "Lost the %s (%s) -- looking for it again\n", kind.c_str(), cur.path.c_str());
                saved_name = cur.name;
            } else if (cur.how != ResolveHow::NotFound) {
                std::fprintf(stderr, "Couldn't open the %s %s (%s) -- retrying\n", kind.c_str(), cur.path.c_str(),
                             strerror(errno));
            }
            std::this_thread::sleep_for(std::chrono::seconds(1));
            cur = resolve_device(kind, std::nullopt, saved_name);
            // give the same device ~10 s to come back before settling for another one
            if (cur.how != ResolveHow::NotFound && saved_name && cur.name != *saved_name && ++misses < 10)
                cur = {"", "", ResolveHow::NotFound};
            else if (cur.how != ResolveHow::NotFound)
                misses = 0;
        }
    };
    std::thread kb_thread(keep_watching, std::string("keyboard"), keyboard, kb_saved_name.has_value()
                          ? kb_saved_name : std::optional<std::string>(keyboard.name));
    std::thread mouse_thread(keep_watching, std::string("mouse"), mouse, mouse_saved_name.has_value()
                             ? mouse_saved_name : std::optional<std::string>(mouse.name));

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
