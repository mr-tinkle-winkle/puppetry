#pragma once
// Control socket server -- mirrors macro_daemon.py's
// _handle_control_client()/_control_server() EXACTLY (same JSON-line
// protocol: FIRE/ABORT/PAUSE/RESUME, same {"ok": ...} response shape),
// so the existing `puppetry --name=.../--abort` CLI and the new
// PySide6 GUI's combo recorder (which still only needs a plain
// Python socket + json client, same as the old GUI's
// send_control_command() -- no reason for that side to be native code)
// keep working against this daemon unmodified.
#include <atomic>
#include <string>
#include <thread>
#include "macro.hpp"
#include "runtime.hpp"

namespace puppetry {

class ControlSocketServer {
public:
    ControlSocketServer(Runtime& rt, MacroRegistry& registry, std::vector<std::unique_ptr<Macro>>& macros);
    ~ControlSocketServer();

    // Binds `path` (removing any stale socket file first) and starts
    // accepting connections on a background thread. Throws
    // std::runtime_error on bind failure.
    void start(const std::string& path);
    void stop();

private:
    void accept_loop();
    std::string handle_line(const std::string& line);

    Runtime& rt_;
    MacroRegistry& registry_;
    std::vector<std::unique_ptr<Macro>>& macros_;
    int listen_fd_ = -1;
    std::string socket_path_;
    std::thread accept_thread_;
    std::atomic<bool> running_{false};
};

} // namespace puppetry
