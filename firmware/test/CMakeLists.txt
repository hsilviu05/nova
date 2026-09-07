# Host tests for the firmware core.
#
# The core has no ESP-IDF dependency, which is the point: the logic that
# decides how NOVA behaves compiles and runs on a laptop and in CI, with no
# board attached and no toolchain to install.
#
#   cmake -S test -B build && cmake --build build && ctest --test-dir build
#
# The ESP-IDF layer in main/ is NOT built here. It cannot be: it needs the
# Xtensa toolchain. Keeping the boundary sharp enough that this file works
# without one is a design constraint, not an accident.

cmake_minimum_required(VERSION 3.20)
# C as well as C++: cJSON is C.
project(nova_firmware_core_tests C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_compile_options(-Wall -Wextra -Wpedantic -Werror)
set(CMAKE_C_STANDARD 11)

# cJSON is vendored for the host build only. On the device ESP-IDF supplies
# its own copy, so this is not an added dependency there -- it exists so the
# tests exercise the same parser the firmware will run.
add_library(cjson STATIC ../third_party/cJSON/cJSON.c)
target_include_directories(cjson PUBLIC ../third_party/cJSON)
# Third-party code is not held to this project's warning settings.
target_compile_options(cjson PRIVATE -w)

add_library(nova_core
    ../core/src/behaviour.cpp
    ../core/src/pca9685.cpp
    ../core/src/protocol.cpp
)
target_include_directories(nova_core PUBLIC ../core/include)
target_link_libraries(nova_core PUBLIC cjson)

enable_testing()

set(SUITES geometry behaviour protocol pca9685 outbox)

foreach(suite IN LISTS SUITES)
    add_executable(test_${suite} test_${suite}.cpp)
    target_link_libraries(test_${suite} PRIVATE nova_core)
    target_include_directories(test_${suite} PRIVATE .)
    # The protocol fixtures are generated from the Python schema; the test
    # reads them from the source tree rather than embedding a copy.
    target_compile_definitions(test_${suite}
        PRIVATE FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")
    add_test(NAME ${suite} COMMAND test_${suite})
endforeach()
