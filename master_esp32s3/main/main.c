#include <stdio.h>
#include <string.h>
#include <stdbool.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_now.h"
#include "esp_camera.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"

#include "driver/gpio.h"
#include "jpeg_decoder.h"
#include "display_st7789.h"
#include "camera_light.h"
#include "cJSON.h"
#include "../../camera_quality.h"
#include "../../capture_protocol.h"

static const char *TAG = "MASTER_ESP32S3";

/* =========================================================
 * WIFI + SERVER
 * ========================================================= */
#include "../../project_network.h"
#define SERVER_URL    SERVER_BASE_URL "/api/upload_master"

/* =========================================================
 * GPIO
 * ========================================================= */
#define RELAY_PIN 1
#define PIR_PIN GPIO_NUM_14

/* =========================================================
 * CAMERA OV5640
 * ========================================================= */
#define PWDN_GPIO_NUM     -1
#define RESET_GPIO_NUM    -1

#define XCLK_GPIO_NUM     15
#define SIOD_GPIO_NUM      4
#define SIOC_GPIO_NUM      5

#define Y9_GPIO_NUM       16
#define Y8_GPIO_NUM       17
#define Y7_GPIO_NUM       18
#define Y6_GPIO_NUM       12
#define Y5_GPIO_NUM       10
#define Y4_GPIO_NUM        8
#define Y3_GPIO_NUM        9
#define Y2_GPIO_NUM       11

#define VSYNC_GPIO_NUM     6
#define HREF_GPIO_NUM      7
#define PCLK_GPIO_NUM     13

// Catch accidental reuse of any currently assigned Master peripheral pin.
#define MASTER_USED_GPIO_MASK ((1ULL << RELAY_PIN) | (1ULL << PIR_PIN) | \
    (1ULL << XCLK_GPIO_NUM) | (1ULL << SIOD_GPIO_NUM) | (1ULL << SIOC_GPIO_NUM) | \
    (1ULL << Y9_GPIO_NUM) | (1ULL << Y8_GPIO_NUM) | (1ULL << Y7_GPIO_NUM) | \
    (1ULL << Y6_GPIO_NUM) | (1ULL << Y5_GPIO_NUM) | (1ULL << Y4_GPIO_NUM) | \
    (1ULL << Y3_GPIO_NUM) | (1ULL << Y2_GPIO_NUM) | (1ULL << VSYNC_GPIO_NUM) | \
    (1ULL << HREF_GPIO_NUM) | (1ULL << PCLK_GPIO_NUM) | \
    (1ULL << PIN_NUM_LCD_MOSI) | (1ULL << PIN_NUM_LCD_PCLK) | \
    (1ULL << PIN_NUM_LCD_CS) | (1ULL << PIN_NUM_LCD_DC) | \
    (1ULL << PIN_NUM_LCD_RST) | (1ULL << PIN_NUM_BK_LIGHT))
_Static_assert((MASTER_USED_GPIO_MASK & (1ULL << CAMERA_LIGHT_GPIO)) == 0,
               "Camera light GPIO conflicts with a Master peripheral");

/* =========================================================
 * ESP-NOW
 * ========================================================= */
/* Broadcast */
static uint8_t broadcastAddress[] = {
    0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF
};

/* =========================================================
 * GLOBAL VARIABLES
 * ========================================================= */
static esp_lcd_panel_handle_t panel_handle = NULL;
static uint16_t *lcd_render_buf = NULL;

static QueueHandle_t slave_status_queue;
static char http_response_buffer[4096] = "";
static size_t http_response_len = 0;
static bool http_response_overflow = false;

#define SCREEN_TIMEOUT_US (5ULL * 60ULL * 1000000ULL) // 5 phút đếm ngược
#define PIR_REARM_LOW_US 1000000LL // Require 1 s LOW after a completed session.
#define PREVIEW_COUNTDOWN_MS 20000

_Static_assert(CAMERA_LIGHT_WARMUP_MS < PREVIEW_COUNTDOWN_MS,
               "Light warmup must fit inside the preview countdown");

/* =========================================================
 * CAMERA INIT
 * ========================================================= */
static esp_err_t master_set_frame_size(framesize_t size)
{
    sensor_t *sensor = esp_camera_sensor_get();
    if (!sensor || !sensor->set_framesize) return ESP_ERR_INVALID_STATE;
    if (sensor->status.framesize == size) return ESP_OK;
    if (sensor->set_framesize(sensor, size) != 0) {
        ESP_LOGE(TAG, "Khong doi duoc kich thuoc camera: %d", size);
        return ESP_FAIL;
    }
    // Let auto exposure converge and drain frames queued in the previous mode.
    // No caller may hold a framebuffer while changing the sensor dimensions.
    vTaskDelay(pdMS_TO_TICKS(200));
    camera_discard_frames(3);
    ESP_LOGI(TAG, "Camera mode: %s", size == ACCESS_MASTER_PREVIEW_SIZE ?
             "preview 240x240" : "capture 640x480");
    return ESP_OK;
}

static esp_err_t init_camera_ov5640(void)
{
    camera_config_t config = {0};

    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;

    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;

    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;

    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href  = HREF_GPIO_NUM;

    config.pin_sccb_sda = SIOD_GPIO_NUM;
    config.pin_sccb_scl = SIOC_GPIO_NUM;

    config.pin_pwdn  = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;

    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    // Allocate framebuffers at the maximum capture size before switching down.
    config.frame_size   = ACCESS_MASTER_CAPTURE_SIZE;
    config.jpeg_quality = ACCESS_JPEG_QUALITY;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Lỗi khởi tạo Camera OV5640: 0x%x", err);
        return err;
    }

    camera_apply_quality(esp_camera_sensor_get(), TAG);
    ESP_LOGI(TAG, "Camera OV5640: preview 240x240, capture 640x480, gain ceiling raw=0x%04x",
             ACCESS_OV_GAIN_CEILING_RAW);
    return master_set_frame_size(ACCESS_MASTER_PREVIEW_SIZE);
}

/* =========================================================
 * WIFI EVENT
 * ========================================================= */
static EventGroupHandle_t wifi_events;
#define WIFI_READY_BIT BIT0

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_READY_BIT);
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, WIFI_READY_BIT);
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Master đã kết nối WiFi! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        uint8_t primary_channel;
        wifi_second_chan_t secondary_channel;
        if (esp_wifi_get_channel(&primary_channel, &secondary_channel) == ESP_OK) {
            ESP_LOGI(TAG, "[NETWORK] SSID=%s; channel=%u; server=%s",
                     WIFI_SSID, (unsigned)primary_channel, SERVER_BASE_URL);
        }
    }
}

static void init_wifi(void)
{
    wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(wifi_events != NULL ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
}

/* =========================================================
 * ESP-NOW CALLBACK
 * ========================================================= */
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
static void on_esp_now_recv(const esp_now_recv_info_t *recv_info, const uint8_t *data, int len)
#else
static void on_esp_now_recv(const uint8_t *mac_addr, const uint8_t *data, int len)
#endif
{
    if (!data || !slave_status_queue || len != sizeof(ControlMessage)) return;
    ControlMessage msg;
    memcpy(&msg, data, sizeof(msg));
    msg.session_id[sizeof(msg.session_id) - 1] = '\0';
    if (msg.msg_type < MSG_ACK_RECEIVED || msg.msg_type > MSG_ACK_UNAVAILABLE) return;
    // The Wi-Fi callback never waits, touches the camera or modifies a session.
    // The access task consumes status messages and checks their session IDs.
    xQueueSend(slave_status_queue, &msg, 0);
}

typedef struct {
    bool received;
    bool ready;
    bool unavailable;
} SlaveCaptureState;

static void collect_slave_status(const char *session_id, SlaveCaptureState *state)
{
    ControlMessage msg;
    while (xQueueReceive(slave_status_queue, &msg, 0) == pdTRUE) {
        if (strcmp(msg.session_id, session_id) != 0) continue;
        state->received = true;
        if (msg.msg_type == MSG_ACK_READY || msg.msg_type == MSG_ACK_UPLOADED) {
            state->ready = true;
            state->unavailable = false;
        } else if (msg.msg_type == MSG_ACK_UNAVAILABLE) {
            state->unavailable = true;
        }
        ESP_LOGI(TAG, "[SLAVE STATUS] session=%s type=%u ready=%d unavailable=%d",
                 session_id, (unsigned)msg.msg_type, state->ready, state->unavailable);
    }
}

static void notify_slave_capture(const char *session_id)
{
    ControlMessage msg = {.msg_type = MSG_CMD_CAPTURE};
    snprintf(msg.session_id, sizeof(msg.session_id), "%s", session_id);
    esp_err_t err = esp_now_send(broadcastAddress, (const uint8_t *)&msg, sizeof(msg));
    if (err != ESP_OK) ESP_LOGW(TAG, "Slave capture notification: %s", esp_err_to_name(err));
}

static void refresh_slave_capture(const char *session_id, SlaveCaptureState *state,
                                  int *notifications_sent, int64_t *last_notification_us)
{
    collect_slave_status(session_id, state);
    int64_t now = esp_timer_get_time();
    if (!state->received && *notifications_sent < 3 && now - *last_notification_us >= 80000LL) {
        notify_slave_capture(session_id);
        ++*notifications_sent;
        *last_notification_us = now;
    }
}

/* =========================================================
 * HTTP EVENT HANDLER
 * ========================================================= */
/* =========================================================
 * MASTER HEARTBEAT TASK
 * ========================================================= */

#define HEARTBEAT_URL SERVER_BASE_URL "/api/heartbeat/master"

static void master_heartbeat_task(void *pvParameters)
{
    while (1) {
        xEventGroupWaitBits(wifi_events, WIFI_READY_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
        // A separate HTTP handle keeps liveness independent of capture/upload.
        // No response callback here uses the access task's JSON buffer.

        esp_http_client_config_t config = {
            .url = HEARTBEAT_URL,
            .method = HTTP_METHOD_POST,
            .timeout_ms = 3000
        };

        esp_http_client_handle_t client = esp_http_client_init(&config);

        if (client != NULL) {

            esp_err_t err = esp_http_client_perform(client);

            if (err == ESP_OK) {
                int status_code = esp_http_client_get_status_code(client);

                if (status_code == 200) {
                    ESP_LOGI(TAG, "[HEARTBEAT] Master ONLINE");
                } else {
                    ESP_LOGW(TAG, "[HEARTBEAT] Server HTTP %d", status_code);
                }

            } else {
                ESP_LOGW(
                    TAG,
                    "[HEARTBEAT] Khong ket noi duoc Server: %s",
                    esp_err_to_name(err)
                );
            }

            esp_http_client_cleanup(client);
        }

        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_DATA && evt->data_len > 0) {
        size_t available = sizeof(http_response_buffer) - 1 - http_response_len;
        size_t count = (size_t)evt->data_len < available ? (size_t)evt->data_len : available;
        memcpy(http_response_buffer + http_response_len, evt->data, count);
        http_response_len += count;
        http_response_buffer[http_response_len] = '\0';
        if (count < (size_t)evt->data_len) http_response_overflow = true;
    }
    return ESP_OK;
}

/* =========================================================
 * HARDWARE INIT (PIR & RELAY)
 * ========================================================= */
static void init_hardware(void)
{
    // PIR HC-SR501
    gpio_config_t pir_config = {
        .pin_bit_mask = (1ULL << PIR_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&pir_config));

    // Relay GPIO
    gpio_config_t relay_cfg = {
        .pin_bit_mask = (1ULL << RELAY_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&relay_cfg));
    gpio_set_level(RELAY_PIN, 0);

    ESP_LOGI(TAG, "Khởi tạo PIR (GPIO%d) và Relay (GPIO%d) thành công", PIR_PIN, RELAY_PIN);
}

/* =========================================================
 * RENDER CAMERA PREVIEW TO LCD
 * ========================================================= */
static bool decode_and_render_preview(camera_fb_t *fb, int remaining_sec, const char *status_msg)
{
    if (!fb || !lcd_render_buf || !panel_handle) {
        return false;
    }

    esp_jpeg_image_cfg_t jpeg_cfg = {
        .indata = fb->buf,
        .indata_size = fb->len,
        .outbuf = (uint8_t *)lcd_render_buf,
        .outbuf_size = LCD_H_RES * LCD_V_RES * sizeof(uint16_t),
        .out_format = JPEG_IMAGE_FORMAT_RGB565,
        .out_scale = JPEG_IMAGE_SCALE_0,
        .flags = {
            .swap_color_bytes = 1,
        }
    };
    esp_jpeg_image_output_t out_img;
    // Metadata on queued frames can lag a resolution switch. Check the JPEG
    // header itself before decoding into the smaller TFT buffer.
    if (esp_jpeg_get_image_info(&jpeg_cfg, &out_img) != ESP_OK ||
        out_img.width != LCD_H_RES || out_img.height != LCD_V_RES) {
        return false;
    }
    esp_err_t ret = esp_jpeg_decode(&jpeg_cfg, &out_img);
    if (ret != ESP_OK) {
        return false;
    }

    // Decode the native 240x240 preview directly, without VGA resize/copy loops.
    st7789_draw_face_overlay(lcd_render_buf, LCD_H_RES, LCD_V_RES, remaining_sec, status_msg);
    st7789_draw_frame(panel_handle, 0, 0, LCD_H_RES, LCD_V_RES, lcd_render_buf);
    return true;
}

/* =========================================================
 * ACCESS CONTROL TASK
 * ========================================================= */
typedef struct {
    bool final;
    bool unlock;
    bool legacy_server;
    int slave_frames;
    float confidence;
    char user_name[64];
    char camera[8];
} AccessDecision;

// Only the access task uses this HTTP buffer. Heartbeats have no data callback.
static bool request_access(esp_http_client_method_t method, const char *url,
                           const char *data, size_t length, const char *session_id,
                           int timeout_ms, AccessDecision *decision, int64_t deadline)
{
    int remaining_ms = (int)((deadline - esp_timer_get_time()) / 1000);
    if (remaining_ms <= 0) return false;
    if (timeout_ms > remaining_ms) timeout_ms = remaining_ms;
    http_response_len = 0;
    http_response_buffer[0] = '\0';
    http_response_overflow = false;
    esp_http_client_config_t cfg = {
        .url = url, .method = method, .event_handler = http_event_handler,
        .timeout_ms = timeout_ms, .buffer_size_tx = 4096,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) return false;
    if (method == HTTP_METHOD_POST) {
        esp_http_client_set_header(client, "Content-Type", data ? "image/jpeg" : "application/octet-stream");
        esp_http_client_set_post_field(client, data ? data : "", length);
    }
    esp_err_t err = esp_http_client_perform(client);
    int http_status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || http_status != 200 || http_response_overflow) {
        ESP_LOGW(TAG, "[SESSION] HTTP=%d ESP=%s overflow=%d", http_status,
                 esp_err_to_name(err), http_response_overflow);
        return false;
    }
    cJSON *reply = cJSON_Parse(http_response_buffer);
    cJSON *status = cJSON_GetObjectItemCaseSensitive(reply, "status");
    cJSON *session = cJSON_GetObjectItemCaseSensitive(reply, "session_id");
    cJSON *protocol = cJSON_GetObjectItemCaseSensitive(reply, "protocol_version");
    cJSON *policy = cJSON_GetObjectItemCaseSensitive(reply, "decision_policy");
    cJSON *mode = cJSON_GetObjectItemCaseSensitive(reply, "capture_mode");
    cJSON *authority = cJSON_GetObjectItemCaseSensitive(reply, "authorization_camera");
    bool valid = cJSON_IsString(status) && cJSON_IsString(session) &&
        strcmp(session->valuestring, session_id) == 0 && cJSON_IsNumber(protocol) &&
        protocol->valueint == 3 && cJSON_IsString(policy) &&
        strcmp(policy->valuestring, "either_camera") == 0 &&
        (mode == NULL || (cJSON_IsString(mode) && strcmp(mode->valuestring, "master_primary") == 0));
    if (valid) {
        // Older protocol-3 servers omit capture_mode. They can still receive
        // photos and enforce the same identity checks; do not stop before upload.
        decision->legacy_server = mode == NULL;
        cJSON *slave_frames = cJSON_GetObjectItemCaseSensitive(reply, "slave_frames");
        if (!cJSON_IsNumber(slave_frames)) {
            slave_frames = cJSON_GetObjectItemCaseSensitive(reply, "slave_frame_count");
        }
        if (cJSON_IsNumber(slave_frames)) decision->slave_frames = slave_frames->valueint;
        static bool warned_legacy_server = false;
        if (decision->legacy_server && !warned_legacy_server) {
            ESP_LOGW(TAG, "[SERVER LEGACY] Missing capture_mode; uploads enabled. "
                          "Restart updated cloud_server for master_primary and current recognition settings.");
            warned_legacy_server = true;
        }
        if (data && length > 0) {
            ESP_LOGI(TAG, "[UPLOAD OK] HTTP=200 session=%s JPEG=%u bytes status=%s",
                     session_id, (unsigned)length, status->valuestring);
        }
    }
    if (valid && strcmp(status->valuestring, "UNLOCK") == 0) {
        valid = cJSON_IsString(authority) &&
            (strcmp(authority->valuestring, "master") == 0 || strcmp(authority->valuestring, "slave") == 0);
        if (valid) {
            decision->final = true;
            decision->unlock = true;
            snprintf(decision->camera, sizeof(decision->camera), "%s", authority->valuestring);
        }
    } else if (valid && strcmp(status->valuestring, "DENIED") == 0) {
        decision->final = true;
        decision->unlock = false;
    } else if (valid && strcmp(status->valuestring, "PENDING") != 0) {
        valid = false;
    }
    if (valid && decision->final) {
        cJSON *name = cJSON_GetObjectItemCaseSensitive(reply, "user_name");
        cJSON *score = cJSON_GetObjectItemCaseSensitive(reply, "confidence");
        snprintf(decision->user_name, sizeof(decision->user_name), "%s",
                 cJSON_IsString(name) ? name->valuestring : "Thanh vien");
        decision->confidence = cJSON_IsNumber(score) ? (float)score->valuedouble : 0;
        ESP_LOGI(TAG, "[SESSION RESULT] %s", http_response_buffer);
    }
    if (!valid) ESP_LOGE(TAG, "[SERVER RESPONSE INVALID] Need protocol=3, matching session, "
                             "either_camera policy and supported mode; relay stays OFF");
    cJSON_Delete(reply);
    return valid;
}

static int update_capture_countdown(int64_t *countdown_end_us, int64_t *light_on_us)
{
    const int64_t warmup_us = CAMERA_LIGHT_WARMUP_MS * 1000LL;
    const int64_t scheduled_on_us = *countdown_end_us - warmup_us;
    if (*light_on_us == 0 && esp_timer_get_time() >= scheduled_on_us) {
        ESP_ERROR_CHECK(camera_light_set(true));
        *light_on_us = esp_timer_get_time();
        // A slow framebuffer/SPI operation must never shorten the final 5 s.
        // Shift only by scheduling lateness, rather than adding another warmup
        // after the complete 20-second countdown.
        *countdown_end_us = *light_on_us + warmup_us;
        ESP_LOGI(TAG, "[LIGHT COUNTDOWN] TFT=5s; %d LED modules ON; scheduling delay=%lu ms",
                 CAMERA_LIGHT_MODULE_COUNT,
                 (unsigned long)((*light_on_us - scheduled_on_us) / 1000));
        // Show 5 immediately on the last completed preview, before another
        // potentially blocking framebuffer acquisition can skip this UI state.
        st7789_draw_face_overlay(lcd_render_buf, LCD_H_RES, LCD_V_RES,
                                 CAMERA_LIGHT_WARMUP_MS / 1000, "DEN SANG - GIU YEN");
        st7789_draw_frame(panel_handle, 0, 0, LCD_H_RES, LCD_V_RES, lcd_render_buf);
    }
    int64_t remaining_us = *countdown_end_us - esp_timer_get_time();
    return remaining_us > 0 ? (int)((remaining_us + 999999) / 1000000) : 0;
}

static void wait_for_capture_light(int64_t light_ready_us)
{
    // Normally already satisfied by the final five preview seconds. Keep the
    // monotonic-time guard without displaying a second five-second countdown.
    while (esp_timer_get_time() < light_ready_us) {
        TickType_t interval = pdMS_TO_TICKS(50);
        vTaskDelay(interval > 0 ? interval : 1);
    }
}

static void run_dual_capture(int64_t light_on_us)
{
    // The access task turned on the LEDs when the preview reached 5 seconds.
    // Preserve that timestamp: turning on/restarting the warmup here adds 5 s.
    ESP_ERROR_CHECK(light_on_us > 0 ? ESP_OK : ESP_ERR_INVALID_STATE);
    const int64_t light_ready_us = light_on_us + CAMERA_LIGHT_WARMUP_MS * 1000LL;
    wait_for_capture_light(light_ready_us);
    ESP_LOGI(TAG, "[LIGHT READY] On for %lu ms; starting capture preparation",
             (unsigned long)((esp_timer_get_time() - light_on_us) / 1000));
    st7789_show_ai_processing_screen(panel_handle, lcd_render_buf);
    EventBits_t bits = xEventGroupWaitBits(wifi_events, WIFI_READY_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(10000));
    if (!(bits & WIFI_READY_BIT)) {
        ESP_ERROR_CHECK(camera_light_set(false));
        ESP_LOGW(TAG, "Khong co Wi-Fi; giu khoa cua");
        st7789_show_result_screen(panel_handle, lcd_render_buf, false, "Mat Wi-Fi", 0);
        vTaskDelay(pdMS_TO_TICKS(3000));
        return;
    }
    char session_id[32];
    snprintf(session_id, sizeof(session_id), "SES_%08lx%08lx_%lu",
             (unsigned long)esp_random(), (unsigned long)esp_random(),
             (unsigned long)(esp_timer_get_time() / 1000));
    SlaveCaptureState slave = {0};
    collect_slave_status(session_id, &slave); // Discard delayed ACKs from older sessions.
    AccessDecision decision = {0};
    char url[256];
    // Warmup finishes BEFORE creating the server session or triggering Slave:
    // neither the server capture timeout nor Slave's burst loses these 5 s.
    int64_t deadline = esp_timer_get_time() + ACCESS_RESULT_TIMEOUT_MS * 1000LL;
    snprintf(url, sizeof(url), SERVER_BASE_URL "/api/capture/start?session_id=%s&mode=master_primary", session_id);
    if (request_access(HTTP_METHOD_POST, url, NULL, 0, session_id, 5000, &decision, deadline) && !decision.final) {
        bool camera_ready = master_set_frame_size(ACCESS_MASTER_CAPTURE_SIZE) == ESP_OK;
        if (camera_ready) {
            // Even when the sensor was already in VGA, queued buffers may have
            // been exposed during the warmup. Drain both framebuffers.
            camera_discard_frames(2);
            ESP_LOGI(TAG, "[CAPTURE] Light settled; capturing new frames with XY-MOS ON");
        }
        // Prepare Master first, then notify Slave at the start of the SAME
        // capture burst. Master never waits for a Slave ACK before capturing.
        const int64_t capture_started_us = esp_timer_get_time();
        notify_slave_capture(session_id);
        int notifications_sent = 1;
        int64_t last_notification_us = capture_started_us;
        ESP_LOGI(TAG, "[CAPTURE START] Master + optional Slave; session=%s", session_id);
        // Slave can still authorize when Master capture fails.
        for (int i = 0; camera_ready && i < ACCESS_CAPTURE_FRAMES && !decision.final; ++i) {
            if (esp_timer_get_time() >= deadline) break;
            refresh_slave_capture(session_id, &slave, &notifications_sent, &last_notification_us);
            camera_fb_t *fb = NULL;
            // Reject queued images exposed before the shared capture notice.
            // Bound the drain so a broken camera cannot loop indefinitely.
            for (int drain = 0; drain < 4 && esp_timer_get_time() < deadline; ++drain) {
                fb = esp_camera_fb_get();
                if (!fb) break;
                int64_t frame_us = (int64_t)fb->timestamp.tv_sec * 1000000LL + fb->timestamp.tv_usec;
                if (frame_us >= capture_started_us && frame_us >= light_ready_us) break;
                esp_camera_fb_return(fb);
                fb = NULL;
            }
            if (!fb) {
                ESP_LOGW(TAG, "Master frame %d failed; van cho ket qua Slave", i);
                continue;
            }
            int64_t frame_us = (int64_t)fb->timestamp.tv_sec * 1000000LL + fb->timestamp.tv_usec;
            // Retry during the burst; never sleep before Master's first frame.
            refresh_slave_capture(session_id, &slave, &notifications_sent, &last_notification_us);
            ESP_LOGI(TAG, "[CAPTURE] Master frame %d/%d, XY-MOS ON for %lu ms at frame start, JPEG=%u bytes",
                     i + 1, ACCESS_CAPTURE_FRAMES,
                     (unsigned long)((frame_us - light_on_us) / 1000), (unsigned)fb->len);
            // Completion also declares whether a working Slave joined. Do not
            // finalize the last upload before that participation is known.
            snprintf(url, sizeof(url), SERVER_URL "?session_id=%s&frame_index=%d&final=0",
                     session_id, i);
            request_access(HTTP_METHOD_POST, url, (const char *)fb->buf, fb->len,
                           session_id, 12000, &decision, deadline);
            esp_camera_fb_return(fb);
            vTaskDelay(pdMS_TO_TICKS(150));
        }
        // Master no longer needs illumination while waiting for server/Slave.
        ESP_ERROR_CHECK(camera_light_set(false));
        collect_slave_status(session_id, &slave);
        // When the Master camera itself is broken, allow a bounded Slave
        // fallback even if its READY packet has not arrived yet.
        bool slave_expected = (slave.ready || !camera_ready) && !slave.unavailable;
        ESP_LOGI(TAG, "[MASTER COMPLETE] Slave %s; Master recognition is independent",
                 slave_expected ? "ready" : "absent/unavailable");
        // No ACK wait: missing/broken Slave must not hold Master's verdict.
        if (decision.legacy_server && !slave_expected && decision.slave_frames == 0 && !decision.final) {
            // Compatibility with servers predating optional-Slave sessions:
            // close the absent Slave branch before completing Master.
            snprintf(url, sizeof(url), SERVER_BASE_URL "/api/capture/complete?session_id=%s&camera=slave", session_id);
            request_access(HTTP_METHOD_POST, url, NULL, 0, session_id, 5000, &decision, deadline);
        }
        snprintf(url, sizeof(url), SERVER_BASE_URL "/api/capture/complete?session_id=%s&camera=master&slave_expected=%d",
                 session_id, slave_expected);
        if (!decision.final) request_access(HTTP_METHOD_POST, url, NULL, 0, session_id, 5000, &decision, deadline);
        snprintf(url, sizeof(url), SERVER_BASE_URL "/api/capture/result?session_id=%s", session_id);
        while (!decision.final && esp_timer_get_time() < deadline) {
            refresh_slave_capture(session_id, &slave, &notifications_sent, &last_notification_us);
            request_access(HTTP_METHOD_GET, url, NULL, 0, session_id, 5000, &decision, deadline);
            if (!decision.final) vTaskDelay(pdMS_TO_TICKS(300));
        }
    } else {
        // A failed start cannot reuse a cached result to open the relay.
        decision.unlock = false;
    }
    ESP_ERROR_CHECK(camera_light_set(false));
    static char last_unlocked_session[32] = "";
    bool unlock = decision.final && decision.unlock && strcmp(last_unlocked_session, session_id) != 0;
    if (unlock) {
        snprintf(last_unlocked_session, sizeof(last_unlocked_session), "%s", session_id);
        ESP_LOGI(TAG, "[UNLOCK] camera=%s session=%s user=%s cosine=%.3f",
                 decision.camera, session_id, decision.user_name, decision.confidence);
        gpio_set_level(RELAY_PIN, 1);
    } else {
        ESP_LOGW(TAG, "[DENIED/TIMEOUT] session=%s; giu khoa cua", session_id);
    }
    st7789_show_result_screen(panel_handle, lcd_render_buf, unlock,
                              unlock ? decision.user_name : "Chua xac thuc", decision.confidence);
    vTaskDelay(pdMS_TO_TICKS(3000));
    gpio_set_level(RELAY_PIN, 0);
}

static void access_control_task(void *pvParameters)
{
    int64_t screen_off_deadline_us = 0;
    bool screen_is_on = false;
    bool pir_armed = true;
    int64_t pir_low_since_us = 0;

    // Bind runtime light ownership to this task before waiting for motion.
    ESP_ERROR_CHECK(camera_light_set(false));

    // Ban đầu tắt màn hình
    st7789_set_backlight(false);
    esp_lcd_panel_disp_on_off(panel_handle, false);
    st7789_fill_color(panel_handle, COLOR_BLACK);

    ESP_LOGI(TAG, "Access Control Task sẵn sàng. Chờ PIR phát hiện chuyển động...");

    while (1) {
        bool pir_detected = (gpio_get_level(PIR_PIN) == 1);
        int64_t pir_sample_us = esp_timer_get_time();
        if (pir_detected) {
            pir_low_since_us = 0;
            if (screen_is_on) screen_off_deadline_us = pir_sample_us + SCREEN_TIMEOUT_US;
        } else if (!pir_armed) {
            if (pir_low_since_us == 0) pir_low_since_us = pir_sample_us;
            if (pir_sample_us - pir_low_since_us >= PIR_REARM_LOW_US) {
                pir_armed = true;
                ESP_LOGI(TAG, "[PIR] Da LOW on dinh; san sang phien tiep theo");
            }
        }

        if (pir_detected && pir_armed) {
            // Consume this motion event. A sustained HIGH cannot run another
            // light/capture/unlock cycle immediately after this one finishes.
            pir_armed = false;
            pir_low_since_us = 0;
            // Reset the session's illumination; the same task switches it on
            // at 5 seconds remaining and off when capture finishes or fails.
            ESP_ERROR_CHECK(camera_light_set(false));
            bool preview_ready = master_set_frame_size(ACCESS_MASTER_PREVIEW_SIZE) == ESP_OK;
            if (!preview_ready) ESP_LOGW(TAG, "Master camera unavailable; Slave recognition remains active");
            // Khi PIR phát hiện người: Reset thời gian sáng màn hình thêm 5 phút
            screen_off_deadline_us = esp_timer_get_time() + SCREEN_TIMEOUT_US;

            if (!screen_is_on) {
                ESP_LOGI(TAG, ">>> [PIR] Phát hiện người -> BẬT MÀN HÌNH TFT (Bộ đếm: 5 phút) <<<");
                screen_is_on = true;
                esp_lcd_panel_disp_on_off(panel_handle, true);
                st7789_set_backlight(true);
            } else {
                ESP_LOGI(TAG, "[PIR] Quét thấy chuyển động tiếp -> Reset lại bộ đếm 5 phút");
            }

            // =========================================================
            // BƯỚC 1: HIỆN THÔNG BÁO CHÀO MỪNG TRONG 2.5 GIÂY
            // "Xin Chào. Vui lòng đặt khuôn mặt vào khung trong vòng 20s"
            // =========================================================
            ESP_LOGI(TAG, "[UI] Hiển thị thông điệp Chào Mừng & Hướng dẫn 20s...");
            st7789_show_welcome_screen(panel_handle, lcd_render_buf);

            for (int w = 0; w < 25; w++) {
                vTaskDelay(pdMS_TO_TICKS(100));
                if (gpio_get_level(PIR_PIN) == 1) {
                    screen_off_deadline_us = esp_timer_get_time() + SCREEN_TIMEOUT_US;
                }
            }

            // =========================================================
            // BƯỚC 2: MỞ CAMERA LIVE PREVIEW TRÊN TFT & ĐẾM NGƯỢC 20S
            // =========================================================
            ESP_LOGI(TAG, "[LIVE PREVIEW] Mở camera live trên ST7789 & đếm ngược 20s...");
            int64_t preview_start_us = esp_timer_get_time();
            int64_t countdown_end_us = preview_start_us + PREVIEW_COUNTDOWN_MS * 1000LL;
            int64_t light_on_us = 0;
            unsigned preview_frames = 0;
            int last_remaining_sec = -1;

            for (;;) {
                TickType_t frame_start = xTaskGetTickCount();
                if (gpio_get_level(PIR_PIN) == 1) {
                    screen_off_deadline_us = esp_timer_get_time() + SCREEN_TIMEOUT_US;
                }

                int remaining_sec = update_capture_countdown(&countdown_end_us, &light_on_us);
                if (remaining_sec == 0) break;

                camera_fb_t *fb = preview_ready ? esp_camera_fb_get() : NULL;
                // Frame acquisition may block across the 5-second boundary.
                // Refresh time and light state before rendering this frame.
                remaining_sec = update_capture_countdown(&countdown_end_us, &light_on_us);
                if (remaining_sec == 0) {
                    if (fb) esp_camera_fb_return(fb);
                    break;
                }
                const char *status = light_on_us ? "DEN SANG - GIU YEN" : "CAN MAT VAO KHUNG";
                if (remaining_sec != last_remaining_sec) {
                    ESP_LOGI(TAG, "[COUNTDOWN] TFT=%ds; XY-MOS=%s",
                             remaining_sec, light_on_us ? "ON" : "OFF");
                    last_remaining_sec = remaining_sec;
                }
                if (fb) {
                    if (decode_and_render_preview(fb, remaining_sec, status)) {
                        ++preview_frames;
                    }
                    esp_camera_fb_return(fb);
                } else {
                    memset(lcd_render_buf, 0, LCD_H_RES * LCD_V_RES * sizeof(uint16_t));
                    st7789_draw_face_overlay(lcd_render_buf, LCD_H_RES, LCD_V_RES, remaining_sec,
                                             light_on_us ? status : (preview_ready ? "CHO KHUNG HINH..." : "DUNG CAMERA SLAVE"));
                    st7789_draw_frame(panel_handle, 0, 0, LCD_H_RES, LCD_V_RES, lcd_render_buf);
                    vTaskDelay(pdMS_TO_TICKS(200));
                }

                TickType_t elapsed = xTaskGetTickCount() - frame_start;
                TickType_t interval = pdMS_TO_TICKS(ACCESS_PREVIEW_INTERVAL_MS);
                vTaskDelay(elapsed < interval ? interval - elapsed : 1);
            }
            ESP_LOGI(TAG, "[PREVIEW] %.1f FPS, native 240x240",
                     preview_frames * 1000000.0 / (esp_timer_get_time() - preview_start_us));

            // =========================================================
            // BƯỚC 3: HẾT 20S -> BẮT ĐẦU CHỤP BẰNG CẢ 2 CAMERA
            // =========================================================
            st7789_draw_face_overlay(lcd_render_buf, LCD_H_RES, LCD_V_RES, 0, "DANG CHUP...");
            st7789_draw_frame(panel_handle, 0, 0, LCD_H_RES, LCD_V_RES, lcd_render_buf);
            ESP_LOGI(TAG, ">>> [CAPTURE] TFT=0s; den da sang %lu ms, bat dau phien chup <<<",
                     (unsigned long)((esp_timer_get_time() - light_on_us) / 1000));

            // One session for both cameras; only this task drives the relay.
            run_dual_capture(light_on_us);
            if (master_set_frame_size(ACCESS_MASTER_PREVIEW_SIZE) != ESP_OK) {
                ESP_LOGE(TAG, "Khong khoi phuc duoc preview; se thu lai o luot ke tiep");
            }

            // Cooldown 1 giây
            vTaskDelay(pdMS_TO_TICKS(1000));
        }

        // =========================================================
        // KIỂM TRA TIMEOUT 5 PHÚT NẾU PIR KHÔNG THẤY CHUYỂN ĐỘNG
        // =========================================================
        if (screen_is_on) {
            int64_t now_us = esp_timer_get_time();
            if (now_us >= screen_off_deadline_us) {
                ESP_LOGI(TAG, ">>> [PIR TIMEOUT] Quá 5 phút không có chuyển động -> Tắt màn hình TFT <<<");
                screen_is_on = false;
                st7789_set_backlight(false);
                esp_lcd_panel_disp_on_off(panel_handle, false);
                st7789_fill_color(panel_handle, COLOR_BLACK);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

/* =========================================================
 * APP MAIN
 * ========================================================= */
void app_main(void)
{
    ESP_LOGI(TAG, "Firmware %s; source=%s", CAMERA_LIGHT_FIRMWARE_ID, __FILE__);
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    } else {
        ESP_ERROR_CHECK(ret);
    }

    // PIR & Relay
    ESP_ERROR_CHECK(camera_light_init());
    init_hardware();

    // Display ST7789
    ESP_ERROR_CHECK(st7789_init(&panel_handle));
    st7789_fill_color(panel_handle, COLOR_BLACK);

    // Cấp phát Render Buffer cho Live Preview trong PSRAM
    lcd_render_buf = (uint16_t *)heap_caps_malloc(LCD_H_RES * LCD_V_RES * sizeof(uint16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!lcd_render_buf) {
        ESP_LOGW(TAG, "Không thể cấp phát PSRAM cho render buffer, dùng internal RAM");
        lcd_render_buf = (uint16_t *)malloc(LCD_H_RES * LCD_V_RES * sizeof(uint16_t));
    }

    ESP_ERROR_CHECK(lcd_render_buf ? ESP_OK : ESP_ERR_NO_MEM);

    // Camera OV5640
    esp_err_t camera_err = init_camera_ov5640();
    if (camera_err != ESP_OK) {
        ESP_LOGE(TAG, "Master camera init: %s; tiep tuc de Slave co the xac thuc", esp_err_to_name(camera_err));
    }

    // WiFi
    init_wifi();

    // ESP-NOW
    slave_status_queue = xQueueCreate(8, sizeof(ControlMessage));
    ESP_ERROR_CHECK(slave_status_queue ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_now_init());
    esp_now_peer_info_t peerInfo = {0};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peerInfo));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_esp_now_recv));
    // Khoi dong Heartbeat Task cho Master
    BaseType_t heartbeat_created = xTaskCreate(
       master_heartbeat_task,
       "master_heartbeat",
       4096,    
       NULL,
       3,
       NULL
    );
    ESP_ERROR_CHECK(heartbeat_created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);

    // Access Control Task
    ESP_ERROR_CHECK(xTaskCreate(access_control_task, "access_task", 8192, NULL, 5, NULL)
                    == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
}
