idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES esp32-camera esp_wifi esp_http_client esp_timer nvs_flash esp_event freertos mbedtls esp_driver_gpio
)
