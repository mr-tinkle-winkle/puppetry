// The live event stream the overlay helper reads: a real unix-socket client
// receives the hello line (held keys), published real/macro events in the
// documented format, and macro output emitted through UinputDevice's hook.
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>
#include "event_stream.hpp"
#include "runtime.hpp"
#include "uinput_device.hpp"

using namespace puppetry;

static int test_count = 0;
#define CHECK(cond) do { ++test_count; if (!(cond)) { \
    std::fprintf(stderr, "FAILED: %s (line %d)\n", #cond, __LINE__); std::exit(1); } } while (0)

static std::string read_lines(int fd, int want_lines) {
    std::string got;
    char buf[512];
    for (int i = 0; i < 100; ++i) {
        ssize_t n = recv(fd, buf, sizeof(buf), MSG_DONTWAIT);
        if (n > 0) got.append(buf, (size_t)n);
        int lines = 0;
        for (char c : got) lines += c == '\n';
        if (lines >= want_lines) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return got;
}

int main() {
    std::string path = "/tmp/puppetry_test_events_" + std::to_string(getpid()) + ".sock";
    Runtime rt;
    rt.held.insert(30);
    EventStream es;
    CHECK(es.start(path, &rt));
    CHECK(!es.active());

    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un addr = {};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    CHECK(connect(fd, (struct sockaddr*)&addr, sizeof(addr)) == 0);
    std::string hello = read_lines(fd, 1);
    CHECK(hello.rfind("h ", 0) == 0);
    CHECK(hello.find(" 30\n") != std::string::npos);
    CHECK(es.active());

    es.publish('r', 1700000000123456LL, 'k', 30, 1);
    es.publish('r', 1700000000124000LL, 'm', -3, 7);
    std::string lines = read_lines(fd, 2);
    CHECK(lines == "r 1700000000123456 k 30 1\nr 1700000000124000 m -3 7\n");

    // macro output through the UinputDevice hook
    g_event_stream.store(&es);
    UinputDevice sink;
    sink.open_sink("/dev/null");
    sink.key_frame(48, 1);
    sink.rel_frame(5, 0);
    sink.wheel_frame(-2);
    std::string out = read_lines(fd, 3);
    CHECK(out.find(" k 48 1\n") != std::string::npos && out[0] == 'm');
    CHECK(out.find(" m 5 0\n") != std::string::npos);
    CHECK(out.find(" w -2 0\n") != std::string::npos);

    // a vanished client is dropped, never blocks
    close(fd);
    for (int i = 0; i < 5; ++i) es.publish('r', 1, 'k', 1, 1);
    CHECK(!es.active());
    g_event_stream.store(nullptr);
    es.stop();
    std::printf("test_event_stream: %d checks passed\n", test_count);
    return 0;
}
