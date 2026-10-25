idf_component_register(
    SRCS "main.c" "display_st7789.c" "camera_light.c"
    INCLUDE_DIRS "."
    REQUIRES esp32-camera esp_lcd esp_wifi esp_http_client esp_timer nvs_flash esp_event freertos driver mbedtls esp_jpeg json
)
