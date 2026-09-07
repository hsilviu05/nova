idf_component_register(
    SRCS
        "app_main.cpp"
        "i2c_bus.cpp"
        "servos.cpp"
        "distance.cpp"
        "nvs_store.cpp"
        "wifi.cpp"
        "ws_link.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        nova_core
        driver
        esp_wifi
        esp_event
        esp_netif
        nvs_flash
        esp_timer
        esp-tls
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Wpedantic)
