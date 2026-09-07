# ESP-IDF project definition.
#
# This is the DEVICE build. It requires ESP-IDF v5.2 or later and the Xtensa
# toolchain, neither of which is installed in this repository's CI -- see
# firmware/README.md. The host tests live in test/ and build with plain CMake
# and no toolchain at all; that split is deliberate and is why almost all of
# the logic sits in core/.
#
#   . $IDF_PATH/export.sh
#   idf.py set-target esp32s3
#   idf.py build flash monitor
#
# v5.2 is the floor because main/ uses the i2c_master driver, which replaced
# the legacy driver/i2c.h API in that release.

cmake_minimum_required(VERSION 3.20)

# core/ is compiled into the app as an ESP-IDF component, unchanged: the same
# translation units the host tests exercise are the ones that run on the
# device. A separate device-side copy would defeat the entire arrangement.
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/components")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nova)
