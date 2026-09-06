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
project(nova_firmware_core_tests CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_compile_options(-Wall -Wextra -Wpedantic -Werror)

add_library(nova_core
    ../core/src/behaviour.cpp
)
target_include_directories(nova_core PUBLIC ../core/include)

enable_testing()

set(SUITES geometry behaviour)

foreach(suite IN LISTS SUITES)
    add_executable(test_${suite} test_${suite}.cpp)
    target_link_libraries(test_${suite} PRIVATE nova_core)
    target_include_directories(test_${suite} PRIVATE .)
    add_test(NAME ${suite} COMMAND test_${suite})
endforeach()
