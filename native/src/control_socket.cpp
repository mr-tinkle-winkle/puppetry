#include "control_socket.hpp"
#include "privacy.hpp"
#include "primitives.hpp"
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace puppetry {

using json = nlohmann::json;

ControlSocketServer::ControlSocketServer(Runtime& rt, MacroRegistry& registry,
                                          std::vector<std::unique_ptr<Macro>>& macros)
    : rt_(rt), registry_(registry), macros_(macros) {}

ControlSocketServer::~ControlSocketServer() { stop(); }

void ControlSocketServer::start(const std::string& path) {
    ::unlink(path.c_str()); // stale socket file from a previous run

    listen_fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd_ < 0) throw std::runtime_error("socket() failed: " + std::string(strerror(errno)));

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (path.size() >= sizeof(addr.sun_path)) {
        throw std::runtime_error("control socket path too long: " + path);
    }
    strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);

    if (::bind(listen_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        throw std::runtime_error("bind() failed: " + std::string(strerror(errno)));
    }
    ::chmod(path.c_str(), 0600); // this user only -- macros can run shell commands

    if (::listen(listen_fd_, 16) < 0) {
        throw std::runtime_error("listen() failed: " + std::string(strerror(errno)));
    }

    socket_path_ = path;
    running_.store(true);
    accept_thread_ = std::thread(&ControlSocketServer::accept_loop, this);
}

void ControlSocketServer::stop() {
    if (!running_.exchange(false)) return;
    if (listen_fd_ >= 0) {
        ::shutdown(listen_fd_, SHUT_RDWR);
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
    if (accept_thread_.joinable()) accept_thread_.join();
    if (!socket_path_.empty()) ::unlink(socket_path_.c_str());
}

void ControlSocketServer::accept_loop() {
    while (running_.load()) {
        int client_fd = ::accept(listen_fd_, nullptr, nullptr);
        if (client_fd < 0) {
            if (!running_.load()) break;
            continue;
        }
        std::thread([this, client_fd] {
            std::string buf;
            char chunk[4096];
            // One line in, one line out, then close -- same contract as
            // the Python version's _handle_control_client().
            while (buf.find('\n') == std::string::npos) {
                ssize_t n = ::recv(client_fd, chunk, sizeof(chunk), 0);
                if (n <= 0) { ::close(client_fd); return; }
                buf.append(chunk, n);
            }
            std::string line = buf.substr(0, buf.find('\n'));
            std::string response = handle_line(line) + "\n";
            ::send(client_fd, response.data(), response.size(), 0);
            ::close(client_fd);
        }).detach();
    }
}

std::string ControlSocketServer::handle_line(const std::string& line) {
    json payload;
    try {
        payload = json::parse(line);
    } catch (const json::exception&) {
        return json{{"ok", false}, {"error", "malformed request"}}.dump();
    }

    std::string cmd = json_str(payload, "cmd", "");
    json resp;

    if (cmd == "ABORT") {
        abort_all(rt_);
        resp = {{"ok", true}};
    } else if (cmd == "PAUSE") {
        rt_.external_pause.store(true);
        resp = {{"ok", true}};
    } else if (cmd == "RESUME") {
        rt_.external_pause.store(false);
        resp = {{"ok", true}};
    } else if (cmd == "VISUALIZER") {
        // block / unblock the input visualizer: {"cmd": "VISUALIZER", "state": "toggle"|"block"|"unblock"|"status"}
        std::string st = json_str(payload, "state", "status");
        if (st == "toggle") privacy_set_manual(rt_, -1);
        else if (st == "block" || st == "on") privacy_set_manual(rt_, 1);
        else if (st == "unblock" || st == "off") privacy_set_manual(rt_, 0);
        resp = {{"ok", true}, {"blocked", rt_.visualizer_blocked.load() || rt_.app_blocked.load()},
                {"manual", rt_.visualizer_blocked.load()}, {"app_blocked", rt_.app_blocked.load()}};
    } else if (cmd == "FIRE") {
        std::string name = json_str(payload, "name", "");
        std::vector<std::string> args;
        if (payload.contains("args") && payload["args"].is_array()) {
            for (const auto& a : payload["args"]) {
                args.push_back(a.is_string() ? a.get<std::string>() : a.dump());
            }
        }
        Macro* match = nullptr;
        for (const auto& m : macros_) {
            if (m->name == name) { match = m.get(); break; }
        }
        if (!match) {
            resp = {{"ok", false}, {"error", "no macro named '" + name + "'"}};
        } else if (!match->enabled) {
            resp = {{"ok", false}, {"error", "'" + name + "' is disabled in the active profile"}};
        } else {
            try {
                trigger_macro(rt_, registry_, *match, args);
                resp = {{"ok", true}};
            } catch (const std::exception& exc) {
                resp = {{"ok", false}, {"error", std::string("'") + name + "' failed to start: " + exc.what()}};
            }
        }
    } else {
        resp = {{"ok", false}, {"error", "unknown command"}};
    }

    return resp.dump();
}

} // namespace puppetry
