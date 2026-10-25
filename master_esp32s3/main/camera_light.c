#include "camera_light.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

_Static_assert(CAMERA_LIGHT_ACTIVE_LEVEL == 0 || CAMERA_LIGHT_ACTIVE_LEVEL == 1,
               "Camera light active level must be 0 or 1");
_Static_assert(GPIO_IS_VALID_OUTPUT_GPIO(CAMERA_LIGHT_GPIO),
               "Camera light requires a valid output GPIO");
_Static_assert(CAMERA_LIGHT_WARMUP_MS >= 5000,
               "Capture light must be on for at least 5 seconds before capture");

static bool initialized;
static bool light_on;
static TaskHandle_t owner;

esp_err_t camera_light_init(void)
{
    if (initialized) return ESP_ERR_INVALID_STATE;
    if (!GPIO_IS_VALID_OUTPUT_GPIO(CAMERA_LIGHT_GPIO)) return ESP_ERR_INVALID_ARG;
    // Preload the inactive latch before enabling the output driver.
    esp_err_t err = gpio_set_level(CAMERA_LIGHT_GPIO, !CAMERA_LIGHT_ACTIVE_LEVEL);
    if (err != ESP_OK) return err;
    const gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << CAMERA_LIGHT_GPIO,
        // Enable input as well so logs can report the actual digital pad level.
        // This does not measure the 5V load or prove that the LED lights up.
        .mode = GPIO_MODE_INPUT_OUTPUT,
        .pull_up_en = CAMERA_LIGHT_ACTIVE_LEVEL ? GPIO_PULLUP_DISABLE : GPIO_PULLUP_ENABLE,
        .pull_down_en = CAMERA_LIGHT_ACTIVE_LEVEL ? GPIO_PULLDOWN_ENABLE : GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    err = gpio_config(&cfg);
    if (err == ESP_OK) {
        initialized = true;
        ESP_LOGI("CAMERA_LIGHT", "%s: XY-MOS GPIO%d ready OFF, active=%d, pad=%d, modules=%d, warmup=%dms",
                 CAMERA_LIGHT_FIRMWARE_ID, CAMERA_LIGHT_GPIO, CAMERA_LIGHT_ACTIVE_LEVEL,
                 gpio_get_level(CAMERA_LIGHT_GPIO), CAMERA_LIGHT_MODULE_COUNT, CAMERA_LIGHT_WARMUP_MS);
    }
    return err;
}

esp_err_t camera_light_set(bool enabled)
{
    if (!initialized) return ESP_ERR_INVALID_STATE;
    // First runtime caller becomes the owner. No callbacks/background workers
    // may subsequently change illumination during a camera exposure.
    TaskHandle_t caller = xTaskGetCurrentTaskHandle();
    if (owner && owner != caller) return ESP_ERR_INVALID_STATE;
    owner = caller;
    if (light_on == enabled) return ESP_OK;
    esp_err_t err = gpio_set_level(CAMERA_LIGHT_GPIO,
                                  enabled ? CAMERA_LIGHT_ACTIVE_LEVEL : !CAMERA_LIGHT_ACTIVE_LEVEL);
    if (err == ESP_OK) {
        light_on = enabled;
        int expected = enabled ? CAMERA_LIGHT_ACTIVE_LEVEL : !CAMERA_LIGHT_ACTIVE_LEVEL;
        int actual = gpio_get_level(CAMERA_LIGHT_GPIO);
        ESP_LOGI("CAMERA_LIGHT", "GPIO%d %s requested=%d pad=%d (no LED/load feedback)",
                 CAMERA_LIGHT_GPIO, enabled ? "ON" : "OFF", expected, actual);
        if (actual != expected) {
            ESP_LOGE("CAMERA_LIGHT", "GPIO pad mismatch: check TRIG wiring, shorts and common GND");
        }
    } else {
        ESP_LOGE("CAMERA_LIGHT", "GPIO%d write failed: %s", CAMERA_LIGHT_GPIO, esp_err_to_name(err));
    }
    return err;
}
