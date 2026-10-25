#pragma once

#include <stdbool.h>
#include "esp_err.h"
#include "driver/gpio.h"

// One XY-MOS switches both 5V LED modules wired in parallel, using external power.
// Verify GPIO21 is exposed and unused on the installed board before wiring.
#define CAMERA_LIGHT_GPIO GPIO_NUM_21
#define CAMERA_LIGHT_ACTIVE_LEVEL 1
#define CAMERA_LIGHT_MODULE_COUNT 2
#define CAMERA_LIGHT_FIRMWARE_ID "capture-light-v6-compatible-upload"
// The final 5 seconds of the 20-second preview provide this warmup.
// Recognition frames must still start after the complete interval.
#define CAMERA_LIGHT_WARMUP_MS 5000

// Initialize once in app_main; set only from access_control_task afterwards.
// Steady illumination during capture: no LEDC timer/channel is taken from XCLK.
esp_err_t camera_light_init(void);
esp_err_t camera_light_set(bool enabled);
