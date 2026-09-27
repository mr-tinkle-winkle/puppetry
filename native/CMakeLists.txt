cmake_minimum_required(VERSION 3.16)
project(puppetry_daemon CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(PYTHON REQUIRED python3-embed)
find_package(Threads REQUIRED)

# ---------------------------------------------------------------------
# Optimization settings.
#
# Release already means -O3 -DNDEBUG. The two additions here:
#
# LTO, on when the toolchain supports it. This matters more than usual
# for this codebase because the hot paths deliberately live in small
# functions in SEPARATE translation units -- kd() -> key_frame() ->
# write_all(), wait_fn() -> wait_until(), the native VM's per-op
# dispatch. Without cross-TU inlining each of those stays a real call
# through the static library.
#
# -march=native, OFF by default: it produces a binary that only runs on
# the machine that built it, which is wrong for a package (and for a Nix
# build shared between machines). Worth turning on for a personal build:
#   cmake -DPUPPETRY_MARCH_NATIVE=ON ..
# ---------------------------------------------------------------------
option(PUPPETRY_LTO "Link-time optimization in Release builds" ON)
option(PUPPETRY_MARCH_NATIVE "Build for THIS cpu only (-march=native)" OFF)

set(PUPPETRY_IPO OFF)
if(PUPPETRY_LTO AND CMAKE_BUILD_TYPE STREQUAL "Release")
    include(CheckIPOSupported)
    check_ipo_supported(RESULT ipo_ok OUTPUT ipo_why)
    if(ipo_ok)
        set(PUPPETRY_IPO ON)
    else()
        message(STATUS "LTO unavailable, building without it: ${ipo_why}")
    endif()
endif()
# Applies to every target below, tests and bench included -- so the
# benchmark measures the same code the daemon actually runs, and a
# mixed LTO/non-LTO link can't come up.
set(CMAKE_INTERPROCEDURAL_OPTIMIZATION ${PUPPETRY_IPO})

add_library(puppetry_flags INTERFACE)
if(PUPPETRY_MARCH_NATIVE)
    target_compile_options(puppetry_flags INTERFACE -march=native)
endif()

# ---------------------------------------------------------------------
# Key/button name table -- generated at configure/build time from this
# system's own kernel headers (see tools/gen_keycodes.py for why this
# isn't a static committed table).
# ---------------------------------------------------------------------
find_path(LINUX_INPUT_EVENT_CODES_H "linux/input-event-codes.h")
if(NOT LINUX_INPUT_EVENT_CODES_H)
    message(FATAL_ERROR "linux/input-event-codes.h not found -- needed to generate the key/button name table")
endif()

set(KEYCODES_HEADER "${CMAKE_CURRENT_BINARY_DIR}/generated/keycodes_generated.hpp")
add_custom_command(
    OUTPUT "${KEYCODES_HEADER}"
    COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/generated"
    COMMAND Python3::Interpreter "${CMAKE_CURRENT_SOURCE_DIR}/tools/gen_keycodes.py"
            "${LINUX_INPUT_EVENT_CODES_H}/linux/input-event-codes.h" "${KEYCODES_HEADER}"
    DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/tools/gen_keycodes.py"
    COMMENT "Generating keycodes_generated.hpp from this system's kernel headers"
)
add_custom_target(generate_keycodes DEPENDS "${KEYCODES_HEADER}")
find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(CORE_SOURCES
    src/config.cpp
    src/control_socket.cpp
    src/dispatch.cpp
    src/evdev_device.cpp
    src/macro.cpp
    src/native_vm.cpp
    src/pointer_accel.cpp
    src/primitives.cpp
    src/python_embed.cpp
    src/simplified_names.cpp
    src/transcriber.cpp
    src/uinput_device.cpp
)

add_library(puppetry_core STATIC ${CORE_SOURCES})
add_dependencies(puppetry_core generate_keycodes)
target_include_directories(puppetry_core PUBLIC
    src
    "${CMAKE_CURRENT_BINARY_DIR}/generated"
    "${CMAKE_CURRENT_SOURCE_DIR}"
    ${PYTHON_INCLUDE_DIRS}
)
target_compile_options(puppetry_core PUBLIC ${PYTHON_CFLAGS_OTHER})
target_link_libraries(puppetry_core PUBLIC Threads::Threads ${PYTHON_LIBRARIES} puppetry_flags)
target_link_directories(puppetry_core PUBLIC ${PYTHON_LIBRARY_DIRS})

add_executable(puppetry-daemon src/main.cpp)
target_link_libraries(puppetry-daemon PRIVATE puppetry_core)

add_executable(puppetry-transcribe src/transcribe_main.cpp)
target_link_libraries(puppetry-transcribe PRIVATE puppetry_core)

# ---------------------------------------------------------------------
# Tests -- all runnable without real /dev/input or /dev/uinput access
# (see the project handoff for exactly what each one does and doesn't
# prove). `ctest` or the individual binaries both work.
# ---------------------------------------------------------------------
enable_testing()
function(puppetry_test name source)
    add_executable(${name} ${source})
    target_link_libraries(${name} PRIVATE puppetry_core)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

puppetry_test(test_core tests/test_core.cpp)
puppetry_test(test_control_socket tests/test_control_socket.cpp)
puppetry_test(test_python_embed tests/test_python_embed.cpp)
puppetry_test(test_native_vm tests/test_native_vm.cpp)
puppetry_test(test_mixed_calls tests/test_mixed_calls.cpp)
puppetry_test(test_timing tests/test_timing.cpp)
# Timing-sensitive: meaningful on a desktop, but a loaded build machine can
# deschedule it. Labelled so packaged builds can skip it (ctest -LE timing).
set_tests_properties(test_timing PROPERTIES LABELS timing)
puppetry_test(test_transcriber tests/test_transcriber.cpp)

# Timing benchmark -- built, but NOT a ctest test (numbers depend on the
# machine). ./bench_cps [iterations]
add_executable(bench_cps tests/bench_cps.cpp)
target_link_libraries(bench_cps PRIVATE puppetry_core)

install(TARGETS puppetry-daemon puppetry-transcribe RUNTIME DESTINATION bin)
