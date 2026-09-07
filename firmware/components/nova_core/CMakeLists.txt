# core/ as an ESP-IDF component.
#
# The sources are referenced in place rather than copied. The point of the
# core/main split is that the code the host tests prove correct is the code
# that ships; a device-side duplicate would let the two drift, and the drift
# would be invisible until it was a robot behaving oddly on someone's desk.
#
# cJSON comes from ESP-IDF's own bundled copy via the `json` requirement, so
# third_party/cJSON is a host-test artifact only and adds nothing to the
# device image.

idf_component_register(
    SRCS
        "${CMAKE_CURRENT_LIST_DIR}/../../core/src/behaviour.cpp"
        "${CMAKE_CURRENT_LIST_DIR}/../../core/src/connection.cpp"
        "${CMAKE_CURRENT_LIST_DIR}/../../core/src/pca9685.cpp"
        "${CMAKE_CURRENT_LIST_DIR}/../../core/src/protocol.cpp"
    INCLUDE_DIRS
        "${CMAKE_CURRENT_LIST_DIR}/../../core/include"
    REQUIRES
        json
)

# The same warning settings the host build uses. A warning that fails CI on a
# laptop and is tolerated on the device would make the device build the weaker
# of the two, which is backwards.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Wpedantic -Werror)
