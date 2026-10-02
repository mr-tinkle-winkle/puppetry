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
#include "privacy.hpp"
#include <regex>
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

    // blocked (an ignored app / the manual toggle): one "s ... p 1 0" line, then
    // NOTHING -- real input and macro output alike -- until "p 0"
    es.set_paused(true);
    es.set_paused(true);                       // no duplicate line
    CHECK(read_lines(fd, 1).find(" p 1 0\n") != std::string::npos);
    es.publish('r', 5, 'k', 30, 1);
    sink.key_frame(31, 1);
    {
        // a client connecting while blocked gets an empty hello + the pause line
        int fd2 = socket(AF_UNIX, SOCK_STREAM, 0);
        CHECK(connect(fd2, (struct sockaddr*)&addr, sizeof(addr)) == 0);
        std::string h2 = read_lines(fd2, 2);
        CHECK(h2.find(" 30") == std::string::npos && h2.find("s ") != std::string::npos && h2.find(" p 1 0") != std::string::npos);
        close(fd2);
    }
    es.set_paused(false);
    es.publish('r', 6, 'k', 32, 1);
    std::string after = read_lines(fd, 2);
    CHECK(after.find(" p 0 0\n") != std::string::npos && after.find("r 6 k 32 1\n") != std::string::npos
          && after.find(" 31 ") == std::string::npos && after.find("r 5 ") == std::string::npos);

    // ignored-app rules
    {
        json ov = json::parse(R"({"ignored_apps": [{"match": "class", "value": " KeePassXC ", "when": "focused"},
                                                     {"match": "title", "value": "Bitwarden", "when": "open"},
                                                     {"value": ""}, "junk"]})");
        auto rules = parse_ignored_apps(ov);
        CHECK(rules.size() == 2 && rules[0].value == "keepassxc" && !rules[0].when_open && rules[1].when_open
              && rules[1].match == "title");
        CHECK(ignored_app_matches(rules[0], "org.keepassxc.KeePassXC", "x"));
        CHECK(!ignored_app_matches(rules[0], "firefox", "KeePassXC in the title only"));
        CHECK(ignored_app_matches(rules[1], "firefox", "Vault - Bitwarden - Mozilla Firefox"));
        std::string re = icase_substring_regex("Key.X+");
        CHECK(std::regex_match(std::string("org.keY.x+Pass"), std::regex(re)));
        CHECK(!std::regex_match(std::string("keyAx+"), std::regex(re)));    // the dot is literal
        CHECK(parse_ignored_apps(json::object()).empty());
    }

    // a vanished client is dropped, never blocks
    close(fd);
    for (int i = 0; i < 5; ++i) es.publish('r', 1, 'k', 1, 1);
    CHECK(!es.active());
    g_event_stream.store(nullptr);
    es.stop();
    std::printf("test_event_stream: %d checks passed\n", test_count);
    return 0;
}
