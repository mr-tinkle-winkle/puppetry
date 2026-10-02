// Live integration test for the control socket -- a real unix socket
// client (no /dev/input or /dev/uinput needed) exercising FIRE/ABORT/
// PAUSE/RESUME exactly the way the `puppetry` CLI / GUI combo recorder
// would, and checking the exact JSON response shape.
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>
#include "control_socket.hpp"
#include "third_party/nlohmann/json.hpp"

using namespace puppetry;
using json = nlohmann::json;

static int test_count = 0;
#define CHECK(cond) do { \
    ++test_count; \
    if (!(cond)) { \
        std::fprintf(stderr, "FAILED: %s (line %d)\n", #cond, __LINE__); \
        std::exit(1); \
    } \
} while (0)

static json send_request(const std::string& path, const json& payload) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    if (connect(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        close(fd);
        throw std::runtime_error("connect failed");
    }
    std::string line = payload.dump() + "\n";
    send(fd, line.data(), line.size(), 0);
    char buf[4096];
    ssize_t n = recv(fd, buf, sizeof(buf), 0);
    close(fd);
    if (n <= 0) throw std::runtime_error("no response");
    return json::parse(std::string(buf, n));
}

int main() {
    Runtime rt;
    MacroRegistry registry;
    std::vector<std::unique_ptr<Macro>> macros;

    auto m = std::make_unique<Macro>();
    m->id = "m1";
    m->name = "Test Macro";
    m->enabled = true;
    macros.push_back(std::move(m));

    // $TMPDIR, not /tmp: inside a Nix build sandbox /tmp may not exist.
    const char* tmp = std::getenv("TMPDIR");
    std::string sock_path = std::string(tmp && *tmp ? tmp : "/tmp") + "/puppetry_test_control.sock";
    ControlSocketServer server(rt, registry, macros);
    server.start(sock_path);
    // Give the accept thread a beat to actually be listening -- listen()
    // already returned inside start() so this is generous, not required.
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    // Unknown macro name.
    auto r1 = send_request(sock_path, {{"cmd", "FIRE"}, {"name", "nope"}});
    CHECK(r1["ok"] == false);

    // PAUSE / RESUME toggle external_pause.
    auto r2 = send_request(sock_path, {{"cmd", "PAUSE"}});
    CHECK(r2["ok"] == true);
    CHECK(rt.external_pause.load() == true);
    auto r3 = send_request(sock_path, {{"cmd", "RESUME"}});
    CHECK(r3["ok"] == true);
    CHECK(rt.external_pause.load() == false);

    // VISUALIZER: the block toggle (`puppetry-overlay block`, its keybind, the GUI button).
    // The state file goes to $XDG_RUNTIME_DIR/puppetry -- point that at the temp dir.
    setenv("XDG_RUNTIME_DIR", tmp && *tmp ? tmp : "/tmp", 1);
    auto v1 = send_request(sock_path, {{"cmd", "VISUALIZER"}, {"state", "toggle"}});
    CHECK(v1["ok"] == true && rt.visualizer_blocked.load() == true);
    auto v2 = send_request(sock_path, {{"cmd", "VISUALIZER"}, {"state", "toggle"}});
    CHECK(v2["ok"] == true && rt.visualizer_blocked.load() == false);
    auto v3 = send_request(sock_path, {{"cmd", "VISUALIZER"}, {"state", "on"}});
    auto v4 = send_request(sock_path, {{"cmd", "VISUALIZER"}, {"state", "on"}});     // idempotent
    CHECK(v3["ok"] == true && v4["ok"] == true && rt.visualizer_blocked.load() == true);
    send_request(sock_path, {{"cmd", "VISUALIZER"}, {"state", "off"}});
    CHECK(rt.visualizer_blocked.load() == false);

    // Malformed JSON.
    {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        struct sockaddr_un addr;
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        strncpy(addr.sun_path, sock_path.c_str(), sizeof(addr.sun_path) - 1);
        connect(fd, (struct sockaddr*)&addr, sizeof(addr));
        std::string bad = "not json at all\n";
        send(fd, bad.data(), bad.size(), 0);
        char buf[256];
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
        close(fd);
        auto resp = json::parse(std::string(buf, n));
        CHECK(resp["ok"] == false);
    }

    // ABORT always ok=true.
    auto r4 = send_request(sock_path, {{"cmd", "ABORT"}});
    CHECK(r4["ok"] == true);
    std::this_thread::sleep_for(std::chrono::milliseconds(500)); // let abort_all's cleanup thread finish

    server.stop();
    std::printf("All %d checks passed.\n", test_count);
    return 0;
}
