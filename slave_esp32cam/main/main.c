#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "../../camera_quality.h"
#include "../../capture_protocol.h"
#include "esp_log.h"
#include "esp_idf_version.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_now.h"
#include "esp_camera.h"
#include "img_converters.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"

static const char *TAG = "SLAVE_ESP32CAM";

// ==============================================================================
// 1. CẤU HÌNH WIFI & SERVER
// ==============================================================================
#include "../../project_network.h"
#define SERVER_URL SERVER_BASE_URL "/api/upload_slave"
// Chân Camera AI-Thinker ESP32-CAM (gắn OV3660)
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

static uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static QueueHandle_t capture_queue;
static EventGroupHandle_t wifi_events;
#define WIFI_READY_BIT BIT0
#define SLAVE_FIRMWARE_ID "slave-camera-recovery-v2-sync"
#define CAMERA_STARTUP_ATTEMPTS 3
#define CAMERA_RETRY_DELAY_MS 500

#if !CONFIG_OV3660_SUPPORT
#error "Enable CONFIG_OV3660_SUPPORT for the Slave OV3660 camera"
#endif

// app_main initializes the camera before creating upload_image_task. After
// startup, only upload_image_task may capture, reinitialize or release it.
static bool camera_ready = false;

static esp_err_t init_camera_ov3660(bool software_jpeg)
{
    // Hold PWDN HIGH long enough to reset the sensor's previous power state.
    // esp_camera_init then enables XCLK and releases this same PWDN pin.
    const gpio_config_t power_down = {
        .pin_bit_mask = 1ULL << PWDN_GPIO_NUM,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&power_down);
    if (err != ESP_OK) return err;
    err = gpio_set_level(PWDN_GPIO_NUM, 1);
    if (err != ESP_OK) return err;
    vTaskDelay(pdMS_TO_TICKS(100));

    camera_config_t config = {0};
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;
    config.pin_d0       = Y2_GPIO_NUM;
    config.pin_d1       = Y3_GPIO_NUM;
    config.pin_d2       = Y4_GPIO_NUM;
    config.pin_d3       = Y5_GPIO_NUM;
    config.pin_d4       = Y6_GPIO_NUM;
    config.pin_d5       = Y7_GPIO_NUM;
    config.pin_d6       = Y8_GPIO_NUM;
    config.pin_d7       = Y9_GPIO_NUM;
    config.pin_xclk     = XCLK_GPIO_NUM;
    config.pin_pclk     = PCLK_GPIO_NUM;
    config.pin_vsync    = VSYNC_GPIO_NUM;
    config.pin_href     = HREF_GPIO_NUM;
    config.pin_sccb_sda = SIOD_GPIO_NUM;
    config.pin_sccb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn     = PWDN_GPIO_NUM;
    config.pin_reset    = RESET_GPIO_NUM;
    
    config.xclk_freq_hz = 20000000;
    config.pixel_format = software_jpeg ? PIXFORMAT_RGB565 : PIXFORMAT_JPEG;
    // Reduce raw frame size to limit DMA traffic during software JPEG capture.
    config.frame_size   = software_jpeg ? FRAMESIZE_QVGA : FRAMESIZE_VGA;
    config.jpeg_quality = ACCESS_JPEG_QUALITY;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

    err = esp_camera_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Lỗi khởi tạo Camera OV3660: 0x%x", err);
        // The camera driver releases its allocated state on probe failure.
        // Do not call deinit again on this path.
        return err;
    }

    camera_apply_quality(esp_camera_sensor_get(), TAG);
    ESP_LOGI(TAG, "OV gain ceiling restored to raw=0x%04x", ACCESS_OV_GAIN_CEILING_RAW);
    jpgSetRgb565BE(ACCESS_RGB565_BIG_ENDIAN);
    ESP_LOGI(TAG, "Camera: %s", software_jpeg ? "RGB565 QVGA -> JPEG90" : "JPEG VGA");
    return ESP_OK;
}

static esp_err_t check_camera_capture(void)
{
    vTaskDelay(pdMS_TO_TICKS(300));
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb != NULL) {
        ESP_LOGI(TAG, "Camera self-test OK: %ux%u, %u bytes",
                 (unsigned)fb->width, (unsigned)fb->height, (unsigned)fb->len);
        esp_camera_fb_return(fb);
        return ESP_OK;
    }

    ESP_LOGW(TAG, "JPEG capture timed out; trying RGB565 with software JPEG");
    esp_err_t err = esp_camera_deinit();
    if (err != ESP_OK) return err;
    err = init_camera_ov3660(true);
    if (err != ESP_OK) return err;
    vTaskDelay(pdMS_TO_TICKS(300));
    fb = esp_camera_fb_get();
    if (fb == NULL) {
        ESP_LOGE(TAG, "RGB565 capture also failed. Check camera ribbon, power and GPIO0/XCLK wiring.");
        esp_err_t cleanup = esp_camera_deinit();
        if (cleanup != ESP_OK) {
            ESP_LOGW(TAG, "Camera cleanup: %s", esp_err_to_name(cleanup));
        }
        return ESP_ERR_TIMEOUT;
    }
    ESP_LOGI(TAG, "RGB565 self-test OK: %ux%u, %u bytes",
             (unsigned)fb->width, (unsigned)fb->height, (unsigned)fb->len);
    esp_camera_fb_return(fb);
    return ESP_OK;
}

static bool prepare_camera(unsigned attempts)
{
    for (unsigned attempt = 1; attempt <= attempts; ++attempt) {
        ESP_LOGI(TAG, "[CAMERA INIT] Attempt %u/%u; PWDN=%d XCLK=%d SDA=%d SCL=%d",
                 attempt, attempts, PWDN_GPIO_NUM, XCLK_GPIO_NUM,
                 SIOD_GPIO_NUM, SIOC_GPIO_NUM);
        esp_err_t err = init_camera_ov3660(false);
        // Software JPEG helps only after the sensor was detected successfully;
        // changing pixel format cannot fix a failed SCCB probe (0x106).
        if (err == ESP_OK) err = check_camera_capture();
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "[CAMERA READY] Slave capture self-test passed");
            return true;
        }
        ESP_LOGW(TAG, "[CAMERA INIT] Attempt %u failed: %s (0x%x)",
                 attempt, esp_err_to_name(err), (unsigned)err);
        if (attempt < attempts) vTaskDelay(pdMS_TO_TICKS(CAMERA_RETRY_DELAY_MS));
    }
    gpio_set_level(PWDN_GPIO_NUM, 1);
    ESP_LOGE(TAG, "[CAMERA UNAVAILABLE] Check camera ribbon, 5V supply and GPIO0/XCLK; "
                  "Wi-Fi/ESP-NOW remain active. No image will be uploaded until recovery.");
    return false;
}

static void wifi_event_handler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "Đang bắt đầu kết nối tới SSID: %s ...", WIFI_SSID);
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_READY_BIT);
        wifi_event_sta_disconnected_t* disconn = (wifi_event_sta_disconnected_t*) event_data;
        ESP_LOGW(TAG, "WiFi mất kết nối! Mã lỗi lý do (Reason Code): %d", disconn->reason);
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, WIFI_READY_BIT);
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, ">>> ĐÃ KẾT NỐI WIFI THÀNH CÔNG! IP: " IPSTR " <<<", IP2STR(&event->ip_info.ip));
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
static void send_ack(uint8_t ack_type, const char *session_id)
{
    ControlMessage ack = {0};

    ack.msg_type = ack_type;

    strncpy(
        ack.session_id,
        session_id,
        sizeof(ack.session_id) - 1
    );

    ack.session_id[
        sizeof(ack.session_id) - 1
    ] = '\0';

    esp_err_t err = esp_now_send(
        broadcastAddress,
        (uint8_t *)&ack,
        sizeof(ControlMessage)
    );

    if (err != ESP_OK) {
        ESP_LOGW(
            TAG,
            "[ESP-NOW] Gui ACK that bai: %s",
            esp_err_to_name(err)
        );
    }
}
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
static void on_esp_now_recv(const esp_now_recv_info_t *recv_info, const uint8_t *data, int len)
#else
static void on_esp_now_recv(const uint8_t *mac_addr, const uint8_t *data, int len)
#endif
{
    if (!data || !capture_queue || len != sizeof(ControlMessage)) return;
    ControlMessage command;
    memcpy(&command, data, sizeof(command));
    command.session_id[sizeof(command.session_id) - 1] = '\0';
    if (command.msg_type != MSG_CMD_CAPTURE || command.session_id[0] == '\0') return;
    // Callback is serialized by the Wi-Fi task. Retransmits do not start another burst.
    static char last_session[32] = "";
    if (strcmp(last_session, command.session_id) == 0) {
        send_ack(MSG_ACK_RECEIVED, command.session_id);
        return;
    }
    if (xQueueSend(capture_queue, &command, 0) == pdTRUE) {
        memcpy(last_session, command.session_id, sizeof(last_session));
        send_ack(MSG_ACK_RECEIVED, command.session_id);
    }
}

/* =========================================================
 * SLAVE HEARTBEAT TASK
 * ========================================================= */

#define SLAVE_HEARTBEAT_URL SERVER_BASE_URL "/api/heartbeat/slave"

static void slave_heartbeat_task(void *pvParameters)
{
    while (1) {
        xEventGroupWaitBits(wifi_events, WIFI_READY_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
        // Keep heartbeat active during capture, on its own HTTP handle.

        esp_http_client_config_t config = {
            .url = SLAVE_HEARTBEAT_URL,
            .method = HTTP_METHOD_POST,
            .timeout_ms = 3000
        };

        esp_http_client_handle_t client = esp_http_client_init(&config);

        if (client != NULL) {

            esp_err_t err = esp_http_client_perform(client);

            if (err == ESP_OK) {

                int status_code = esp_http_client_get_status_code(client);

                if (status_code == 200) {
                    ESP_LOGI(TAG, "[HEARTBEAT] Slave ONLINE");
                } else {
                    ESP_LOGW(
                        TAG,
                        "[HEARTBEAT] Server HTTP %d",
                        status_code
                    );
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

        // Gui heartbeat moi 5 giay
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
static void upload_image_task(void *pvParameters)
{
    ControlMessage command;
    while (1) {
        if (xQueueReceive(capture_queue, &command, portMAX_DELAY) != pdTRUE) continue;
        EventBits_t bits = xEventGroupWaitBits(wifi_events, WIFI_READY_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(10000));
        if (!(bits & WIFI_READY_BIT)) {
            ESP_LOGW(TAG, "Khong co Wi-Fi cho session=%s", command.session_id);
            continue;
        }
        int64_t deadline = esp_timer_get_time() + ACCESS_CAPTURE_SESSION_MS * 1000LL;
        // A missing sensor must not reboot the board or prevent completion of
        // this session. Retry once here, owned by this task, without a timer.
        if (!camera_ready) camera_ready = prepare_camera(1);
        send_ack(camera_ready ? MSG_ACK_READY : MSG_ACK_UNAVAILABLE, command.session_id);
        if (camera_ready) camera_discard_frames(2);
        int uploaded = 0;
        int captured = 0;
        int capture_failures = 0;
        for (int i = 0; camera_ready && i < ACCESS_CAPTURE_FRAMES; ++i) {
            if (esp_timer_get_time() >= deadline) break;
            camera_fb_t *fb = esp_camera_fb_get();
            if (!fb) {
                ++capture_failures;
                ESP_LOGE(TAG, "Capture frame %d failed; check camera ribbon/power/XCLK", i);
                continue;
            }
            ++captured;
            uint8_t *converted = NULL;
            uint8_t *data = fb->buf;
            size_t length = fb->len;
            if (fb->format != PIXFORMAT_JPEG) {
                if (!frame2jpg(fb, ACCESS_SOFTWARE_JPEG_QUALITY, &converted, &length)) {
                    free(converted);
                    esp_camera_fb_return(fb);
                    ESP_LOGE(TAG, "Software JPEG failed");
                    continue;
                }
                data = converted;
            }
            char url[256];
            snprintf(url, sizeof(url), "%s?session_id=%s&frame_index=%d&final=%d",
                     SERVER_URL, command.session_id, i, i == ACCESS_CAPTURE_FRAMES - 1);
            int remaining_ms = (int)((deadline - esp_timer_get_time()) / 1000);
            if (remaining_ms <= 0) {
                free(converted);
                esp_camera_fb_return(fb);
                break;
            }
            esp_http_client_config_t cfg = {
                .url = url, .method = HTTP_METHOD_POST,
                .timeout_ms = remaining_ms < 12000 ? remaining_ms : 12000,
                .buffer_size_tx = 4096,
            };
            esp_http_client_handle_t client = esp_http_client_init(&cfg);
            if (client) {
                esp_http_client_set_header(client, "Content-Type", "image/jpeg");
                esp_http_client_set_post_field(client, (const char *)data, length);
                esp_err_t err = esp_http_client_perform(client);
                int status = esp_http_client_get_status_code(client);
                if (err == ESP_OK && status == 200) {
                    ++uploaded;
                    ESP_LOGI(TAG, "Frame %d/%d: %ux%u JPEG %u bytes uploaded", i + 1,
                             ACCESS_CAPTURE_FRAMES, (unsigned)fb->width, (unsigned)fb->height, (unsigned)length);
                } else {
                    ESP_LOGW(TAG, "Upload frame %d: %s HTTP=%d", i, esp_err_to_name(err), status);
                }
                esp_http_client_cleanup(client);
            }
            free(converted);
            esp_camera_fb_return(fb);
            vTaskDelay(pdMS_TO_TICKS(150));
        }
        if (camera_ready && captured == 0 && capture_failures > 0) {
            // Recover on the next command, never while a framebuffer is in use.
            esp_err_t cleanup = esp_camera_deinit();
            if (cleanup != ESP_OK) ESP_LOGW(TAG, "Camera cleanup: %s", esp_err_to_name(cleanup));
            camera_ready = false;
            gpio_set_level(PWDN_GPIO_NUM, 1);
            send_ack(MSG_ACK_UNAVAILABLE, command.session_id);
        }
        if (!camera_ready) {
            ESP_LOGW(TAG, "[CAPTURE SKIPPED] session=%s; camera unavailable; sending completion",
                     command.session_id);
        }
        // Completion is independent of capturing the last frame: never leave Master waiting forever.
        char completion_url[256];
        snprintf(completion_url, sizeof(completion_url),
                 SERVER_BASE_URL "/api/capture/complete?session_id=%s&camera=slave", command.session_id);
        esp_http_client_config_t completion_cfg = {
            .url = completion_url, .method = HTTP_METHOD_POST, .timeout_ms = 5000,
        };
        esp_http_client_handle_t completion = esp_http_client_init(&completion_cfg);
        if (completion) {
            esp_http_client_set_post_field(completion, "", 0);
            esp_err_t err = esp_http_client_perform(completion);
            ESP_LOGI(TAG, "[SESSION COMPLETE] %s: %s HTTP=%d", command.session_id,
                     esp_err_to_name(err), esp_http_client_get_status_code(completion));
            esp_http_client_cleanup(completion);
        }
        if (uploaded >= 2) send_ack(MSG_ACK_UPLOADED, command.session_id);
    }
}


void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());

    ESP_LOGI(TAG, "Khởi động Slave ESP32-CAM...");
    ESP_LOGI(TAG, "[FIRMWARE] %s", SLAVE_FIRMWARE_ID);
    camera_ready = prepare_camera(CAMERA_STARTUP_ATTEMPTS);
    init_wifi();

    capture_queue = xQueueCreate(1, sizeof(ControlMessage));
    ESP_ERROR_CHECK(capture_queue ? ESP_OK : ESP_ERR_NO_MEM);
    // Khởi tạo ESP-NOW
    ESP_ERROR_CHECK(esp_now_init());
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peerInfo));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_esp_now_recv));
    ESP_ERROR_CHECK(xTaskCreate(slave_heartbeat_task, "slave_heartbeat", 4096,
                               NULL, 3, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(xTaskCreate(upload_image_task, "upload_task", 8192,
                               NULL, 5, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
}
