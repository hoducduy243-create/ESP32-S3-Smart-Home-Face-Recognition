#pragma once

#include <stdbool.h>

#include "esp_camera.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// Starting profile for indoor access capture; validate under installed lighting.
#define ACCESS_CAPTURE_FRAMES 3
#define ACCESS_CAPTURE_SESSION_MS 40000
#define ACCESS_RESULT_TIMEOUT_MS 55000
#define ACCESS_JPEG_QUALITY 10 // sensor scale: lower means less compression
#define ACCESS_SOFTWARE_JPEG_QUALITY 90 // software encoder scale: higher is better
#define ACCESS_RGB565_BIG_ENDIAN true
#define ACCESS_MASTER_PREVIEW_SIZE FRAMESIZE_240X240
#define ACCESS_MASTER_CAPTURE_SIZE FRAMESIZE_VGA
#define ACCESS_PREVIEW_INTERVAL_MS 50
// OV3660/OV5640 drivers write the argument straight into 0x3A18/0x3A19.
// Their own initialization tables use 0x00F8. The generic GAINCEILING_8X
// enum is 2, NOT the register value for 8x, and makes these sensors too dark.
#define ACCESS_OV_GAIN_CEILING_RAW 0x00F8

static inline void camera_apply_quality(sensor_t *s, const char *tag)
{
    if (!s) return;
#define CAMERA_SET(name, value) do { \
    if (s->name && s->name(s, value) != 0) { \
        ESP_LOGW(tag, "Camera setting unavailable: %s", #name); \
    } \
} while (0)
    CAMERA_SET(set_vflip, 1);
    CAMERA_SET(set_hmirror, 1);
    CAMERA_SET(set_special_effect, 0);
    CAMERA_SET(set_brightness, 0);
    CAMERA_SET(set_contrast, 1); // restore the contrast used before the regression
    CAMERA_SET(set_saturation, 0);
    // Keep vendor ISP sharpening/denoise defaults instead of forcing manual
    // sharpening. Both camera init paths reset the sensor before this profile.
    CAMERA_SET(set_whitebal, 1);
    CAMERA_SET(set_awb_gain, 1);
    CAMERA_SET(set_wb_mode, 0);
    CAMERA_SET(set_exposure_ctrl, 1);
    CAMERA_SET(set_aec2, 0); // no night-mode extension; add light for moving subjects
    CAMERA_SET(set_ae_level, 0);
    CAMERA_SET(set_gain_ctrl, 1);
    if (s->id.PID == OV5640_PID || s->id.PID == OV3660_PID) {
        CAMERA_SET(set_gainceiling, (gainceiling_t)ACCESS_OV_GAIN_CEILING_RAW);
    } else {
        CAMERA_SET(set_gainceiling, GAINCEILING_8X);
    }
    CAMERA_SET(set_bpc, 1);
    CAMERA_SET(set_wpc, 1);
    CAMERA_SET(set_raw_gma, 1);
    CAMERA_SET(set_lenc, 1);
#undef CAMERA_SET
}

static inline void camera_discard_frames(unsigned count)
{
    for (unsigned i = 0; i < count; ++i) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) break;
        esp_camera_fb_return(fb);
        vTaskDelay(pdMS_TO_TICKS(80));
    }
}
