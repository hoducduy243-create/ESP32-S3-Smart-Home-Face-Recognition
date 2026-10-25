#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "display_st7789.h"

static const char *TAG = "ST7789";
static SemaphoreHandle_t color_transfer_done;

static bool on_color_transfer_done(esp_lcd_panel_io_handle_t io,
                                   esp_lcd_panel_io_event_data_t *event, void *ctx)
{
    (void)io;
    (void)event;
    BaseType_t task_woken = pdFALSE;
    xSemaphoreGiveFromISR((SemaphoreHandle_t)ctx, &task_woken);
    return task_woken == pdTRUE;
}

#define SWAP16(c) (uint16_t)(((c) >> 8) | (((c) & 0xFF) << 8))

/* Standard 8x8 ASCII font (characters 32 ' ' to 126 '~') */
static const uint8_t font8x8_basic[95][8] = {
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, // ' ' (32)
    {0x18, 0x3C, 0x3C, 0x18, 0x18, 0x00, 0x18, 0x00}, // '!'
    {0x66, 0x66, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00}, // '"'
    {0x6C, 0x6C, 0xFE, 0x6C, 0xFE, 0x6C, 0x6C, 0x00}, // '#'
    {0x18, 0x3E, 0x60, 0x3C, 0x06, 0x7C, 0x18, 0x00}, // '$'
    {0x00, 0x63, 0x66, 0x0C, 0x18, 0x33, 0x63, 0x00}, // '%'
    {0x38, 0x6C, 0x38, 0x76, 0xDC, 0xCC, 0x76, 0x00}, // '&'
    {0x30, 0x30, 0x10, 0x20, 0x00, 0x00, 0x00, 0x00}, // '''
    {0x0C, 0x18, 0x30, 0x30, 0x30, 0x18, 0x0C, 0x00}, // '('
    {0x30, 0x18, 0x0C, 0x0C, 0x0C, 0x18, 0x30, 0x00}, // ')'
    {0x00, 0x66, 0x3C, 0xFF, 0x3C, 0x66, 0x00, 0x00}, // '*'
    {0x00, 0x18, 0x18, 0x7E, 0x18, 0x18, 0x00, 0x00}, // '+'
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x18, 0x18, 0x30}, // ','
    {0x00, 0x00, 0x00, 0x7E, 0x00, 0x00, 0x00, 0x00}, // '-'
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x18, 0x18, 0x00}, // '.'
    {0x06, 0x0C, 0x18, 0x30, 0x60, 0xC0, 0x80, 0x00}, // '/'
    {0x3C, 0x66, 0x6E, 0x76, 0x66, 0x66, 0x3C, 0x00}, // '0'
    {0x18, 0x38, 0x18, 0x18, 0x18, 0x18, 0x7E, 0x00}, // '1'
    {0x3C, 0x66, 0x06, 0x0C, 0x18, 0x30, 0x7E, 0x00}, // '2'
    {0x3C, 0x66, 0x06, 0x1C, 0x06, 0x66, 0x3C, 0x00}, // '3'
    {0x0C, 0x1C, 0x34, 0x64, 0x7E, 0x04, 0x04, 0x00}, // '4'
    {0x7E, 0x60, 0x7C, 0x06, 0x06, 0x66, 0x3C, 0x00}, // '5'
    {0x1C, 0x30, 0x60, 0x7C, 0x66, 0x66, 0x3C, 0x00}, // '6'
    {0x7E, 0x66, 0x06, 0x0C, 0x18, 0x18, 0x18, 0x00}, // '7'
    {0x3C, 0x66, 0x66, 0x3C, 0x66, 0x66, 0x3C, 0x00}, // '8'
    {0x3C, 0x66, 0x66, 0x3E, 0x06, 0x0C, 0x38, 0x00}, // '9'
    {0x00, 0x18, 0x18, 0x00, 0x18, 0x18, 0x00, 0x00}, // ':'
    {0x00, 0x18, 0x18, 0x00, 0x18, 0x18, 0x30, 0x00}, // ';'
    {0x0C, 0x18, 0x30, 0x60, 0x30, 0x18, 0x0C, 0x00}, // '<'
    {0x00, 0x00, 0x7E, 0x00, 0x7E, 0x00, 0x00, 0x00}, // '='
    {0x30, 0x18, 0x0C, 0x06, 0x0C, 0x18, 0x30, 0x00}, // '>'
    {0x3C, 0x66, 0x06, 0x0C, 0x18, 0x00, 0x18, 0x00}, // '?'
    {0x3C, 0x66, 0x6E, 0x6E, 0x60, 0x62, 0x3C, 0x00}, // '@'
    {0x18, 0x3C, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x00}, // 'A'
    {0x7C, 0x66, 0x66, 0x7C, 0x66, 0x66, 0x7C, 0x00}, // 'B'
    {0x3C, 0x66, 0x60, 0x60, 0x60, 0x66, 0x3C, 0x00}, // 'C'
    {0x78, 0x6C, 0x66, 0x66, 0x66, 0x6C, 0x78, 0x00}, // 'D'
    {0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x7E, 0x00}, // 'E'
    {0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x60, 0x00}, // 'F'
    {0x3C, 0x66, 0x60, 0x6E, 0x66, 0x66, 0x3C, 0x00}, // 'G'
    {0x66, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x66, 0x00}, // 'H'
    {0x3C, 0x18, 0x18, 0x18, 0x18, 0x18, 0x3C, 0x00}, // 'I'
    {0x0E, 0x06, 0x06, 0x06, 0x06, 0x66, 0x3C, 0x00}, // 'J'
    {0x66, 0x6C, 0x78, 0x70, 0x78, 0x6C, 0x66, 0x00}, // 'K'
    {0x60, 0x60, 0x60, 0x60, 0x60, 0x60, 0x7E, 0x00}, // 'L'
    {0x63, 0x77, 0x7F, 0x6B, 0x63, 0x63, 0x63, 0x00}, // 'M'
    {0x66, 0x76, 0x7E, 0x7E, 0x6E, 0x66, 0x66, 0x00}, // 'N'
    {0x3C, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00}, // 'O'
    {0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60, 0x60, 0x00}, // 'P'
    {0x3C, 0x66, 0x66, 0x66, 0x6A, 0x6C, 0x36, 0x00}, // 'Q'
    {0x7C, 0x66, 0x66, 0x7C, 0x6C, 0x66, 0x66, 0x00}, // 'R'
    {0x3C, 0x66, 0x60, 0x3C, 0x06, 0x66, 0x3C, 0x00}, // 'S'
    {0x7E, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0x00}, // 'T'
    {0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00}, // 'U'
    {0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x18, 0x00}, // 'V'
    {0x63, 0x63, 0x63, 0x6B, 0x7F, 0x77, 0x63, 0x00}, // 'W'
    {0x66, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x66, 0x00}, // 'X'
    {0x66, 0x66, 0x66, 0x3C, 0x18, 0x18, 0x18, 0x00}, // 'Y'
    {0x7E, 0x06, 0x0C, 0x18, 0x30, 0x60, 0x7E, 0x00}, // 'Z'
    {0x3C, 0x30, 0x30, 0x30, 0x30, 0x30, 0x3C, 0x00}, // '['
    {0x60, 0x30, 0x18, 0x0C, 0x06, 0x03, 0x01, 0x00}, // '\'
    {0x3C, 0x0C, 0x0C, 0x0C, 0x0C, 0x0C, 0x3C, 0x00}, // ']'
    {0x18, 0x3C, 0x66, 0x00, 0x00, 0x00, 0x00, 0x00}, // '^'
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x00}, // '_'
    {0x30, 0x18, 0x0C, 0x00, 0x00, 0x00, 0x00, 0x00}, // '`'
    {0x00, 0x00, 0x3C, 0x06, 0x3E, 0x66, 0x3E, 0x00}, // 'a'
    {0x60, 0x60, 0x7C, 0x66, 0x66, 0x66, 0x7C, 0x00}, // 'b'
    {0x00, 0x00, 0x3C, 0x66, 0x60, 0x66, 0x3C, 0x00}, // 'c'
    {0x06, 0x06, 0x3E, 0x66, 0x66, 0x66, 0x3E, 0x00}, // 'd'
    {0x00, 0x00, 0x3C, 0x66, 0x7E, 0x60, 0x3C, 0x00}, // 'e'
    {0x1C, 0x30, 0x30, 0x78, 0x30, 0x30, 0x30, 0x00}, // 'f'
    {0x00, 0x00, 0x3E, 0x66, 0x66, 0x3E, 0x06, 0x3C}, // 'g'
    {0x60, 0x60, 0x7C, 0x66, 0x66, 0x66, 0x66, 0x00}, // 'h'
    {0x18, 0x00, 0x38, 0x18, 0x18, 0x18, 0x3C, 0x00}, // 'i'
    {0x0C, 0x00, 0x0C, 0x0C, 0x0C, 0x0C, 0x6C, 0x38}, // 'j'
    {0x60, 0x60, 0x66, 0x6C, 0x78, 0x6C, 0x66, 0x00}, // 'k'
    {0x38, 0x18, 0x18, 0x18, 0x18, 0x18, 0x3C, 0x00}, // 'l'
    {0x00, 0x00, 0x66, 0x7F, 0x7F, 0x6B, 0x63, 0x00}, // 'm'
    {0x00, 0x00, 0x7C, 0x66, 0x66, 0x66, 0x66, 0x00}, // 'n'
    {0x00, 0x00, 0x3C, 0x66, 0x66, 0x66, 0x3C, 0x00}, // 'o'
    {0x00, 0x00, 0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60}, // 'p'
    {0x00, 0x00, 0x3E, 0x66, 0x66, 0x3E, 0x06, 0x06}, // 'q'
    {0x00, 0x00, 0x7C, 0x66, 0x60, 0x60, 0x60, 0x00}, // 'r'
    {0x00, 0x00, 0x3E, 0x60, 0x3C, 0x06, 0x7C, 0x00}, // 's'
    {0x18, 0x18, 0x7E, 0x18, 0x18, 0x18, 0x0E, 0x00}, // 't'
    {0x00, 0x00, 0x66, 0x66, 0x66, 0x66, 0x3E, 0x00}, // 'u'
    {0x00, 0x00, 0x66, 0x66, 0x66, 0x3C, 0x18, 0x00}, // 'v'
    {0x00, 0x00, 0x63, 0x6B, 0x7F, 0x36, 0x36, 0x00}, // 'w'
    {0x00, 0x00, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x00}, // 'x'
    {0x00, 0x00, 0x66, 0x66, 0x66, 0x3E, 0x06, 0x3C}, // 'y'
    {0x00, 0x00, 0x7E, 0x0C, 0x18, 0x30, 0x7E, 0x00}, // 'z'
    {0x0E, 0x18, 0x18, 0x70, 0x18, 0x18, 0x0E, 0x00}, // '{'
    {0x18, 0x18, 0x18, 0x00, 0x18, 0x18, 0x18, 0x00}, // '|'
    {0x70, 0x18, 0x18, 0x0E, 0x18, 0x18, 0x70, 0x00}, // '}'
    {0x76, 0xDC, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, // '~'
};

/* ========================================================================= */
/* DRAW HELPERS                                                              */
/* ========================================================================= */

static void draw_char(uint16_t *buf, int width, int height, int x, int y, char c, uint16_t color_swapped, int scale)
{
    if (c < 32 || c > 126) c = ' ';
    const uint8_t *bitmap = font8x8_basic[c - 32];

    for (int row = 0; row < 8; row++) {
        uint8_t b = bitmap[row];
        for (int col = 0; col < 8; col++) {
            if (b & (0x80 >> col)) {
                for (int dy = 0; dy < scale; dy++) {
                    for (int dx = 0; dx < scale; dx++) {
                        int px = x + col * scale + dx;
                        int py = y + row * scale + dy;
                        if (px >= 0 && px < width && py >= 0 && py < height) {
                            buf[py * width + px] = color_swapped;
                        }
                    }
                }
            }
        }
    }
}

static void draw_string(uint16_t *buf, int width, int height, int x, int y, const char *str, uint16_t color_swapped, int scale)
{
    int cur_x = x;
    while (*str) {
        draw_char(buf, width, height, cur_x, y, *str, color_swapped, scale);
        cur_x += 8 * scale;
        str++;
    }
}

static void draw_string_centered(uint16_t *buf, int width, int height, int y, const char *str, uint16_t color_swapped, int scale)
{
    int len = (int)strlen(str);
    int str_width = len * 8 * scale;
    int x = (width - str_width) / 2;
    if (x < 0) x = 0;
    draw_string(buf, width, height, x, y, str, color_swapped, scale);
}

static void fill_rect(uint16_t *buf, int width, int height, int x0, int y0, int x1, int y1, uint16_t color_swapped)
{
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 >= width) x1 = width - 1;
    if (y1 >= height) y1 = height - 1;

    for (int y = y0; y <= y1; y++) {
        for (int x = x0; x <= x1; x++) {
            buf[y * width + x] = color_swapped;
        }
    }
}

/* ========================================================================= */
/* ST7789 HARDWARE CONTROL                                                   */
/* ========================================================================= */

esp_err_t st7789_init(esp_lcd_panel_handle_t *panel_handle)
{
    if (!panel_handle) return ESP_ERR_INVALID_ARG;
    if (color_transfer_done) return ESP_ERR_INVALID_STATE;
    color_transfer_done = xSemaphoreCreateBinary();
    if (!color_transfer_done) return ESP_ERR_NO_MEM;
    if (PIN_NUM_BK_LIGHT >= 0) {
        gpio_config_t bk_gpio_config = {
            .mode = GPIO_MODE_OUTPUT,
            .pin_bit_mask = 1ULL << PIN_NUM_BK_LIGHT
        };
        gpio_config(&bk_gpio_config);
        gpio_set_level(PIN_NUM_BK_LIGHT, 0); // Start OFF until PIR triggers
    }

    spi_bus_config_t buscfg = {
        .sclk_io_num = PIN_NUM_LCD_PCLK,
        .mosi_io_num = PIN_NUM_LCD_MOSI,
        .miso_io_num = -1,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = LCD_H_RES * LCD_V_RES * sizeof(uint16_t),
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    esp_lcd_panel_io_handle_t io_handle = NULL;
    esp_lcd_panel_io_spi_config_t io_config = {
        .dc_gpio_num = PIN_NUM_LCD_DC,
        .cs_gpio_num = PIN_NUM_LCD_CS,
        .pclk_hz = 40 * 1000 * 1000,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 1,
        .on_color_trans_done = on_color_transfer_done,
        .user_ctx = color_transfer_done,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));

    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = PIN_NUM_LCD_RST,
        .bits_per_pixel = 16,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(io_handle, &panel_config, panel_handle));

    ESP_ERROR_CHECK(esp_lcd_panel_reset(*panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(*panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(*panel_handle, true));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(*panel_handle, false)); // Start OFF

    ESP_LOGI(TAG, "Ready: SPI 40MHz, wait for transfer before reusing frame buffer");
    return ESP_OK;
}

void st7789_set_backlight(bool enable)
{
    if (PIN_NUM_BK_LIGHT >= 0) {
        gpio_set_level(PIN_NUM_BK_LIGHT, enable ? 1 : 0);
    }
}

void st7789_draw_frame(esp_lcd_panel_handle_t panel_handle, int x_start, int y_start, int x_end, int y_end, const void *color_data)
{
    ESP_ERROR_CHECK(color_transfer_done ? ESP_OK : ESP_ERR_INVALID_STATE);
    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel_handle, x_start, y_start, x_end, y_end, color_data));
    // esp_lcd queues color data asynchronously. Do not let the next JPEG decode
    // overwrite its RGB565 buffer while SPI/DMA is still reading that buffer.
    ESP_ERROR_CHECK(xSemaphoreTake(color_transfer_done, pdMS_TO_TICKS(2000))
                    == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT);
}

void st7789_fill_color(esp_lcd_panel_handle_t panel_handle, uint16_t color_rgb565)
{
    static uint16_t line_buffer[LCD_H_RES];
    uint16_t c_swapped = SWAP16(color_rgb565);
    for (int i = 0; i < LCD_H_RES; i++) {
        line_buffer[i] = c_swapped;
    }
    for (int y = 0; y < LCD_V_RES; y++) {
        st7789_draw_frame(panel_handle, 0, y, LCD_H_RES, y + 1, line_buffer);
    }
}

/* ========================================================================= */
/* SCREENS & OVERLAYS USING EXTERNAL PSRAM BUFFER                            */
/* ========================================================================= */

void st7789_show_welcome_screen(esp_lcd_panel_handle_t panel_handle, uint16_t *buf)
{
    if (!buf) return;

    uint16_t c_black = SWAP16(COLOR_BLACK);
    uint16_t c_yellow = SWAP16(COLOR_YELLOW);
    uint16_t c_cyan   = SWAP16(COLOR_CYAN);
    uint16_t c_white  = SWAP16(COLOR_WHITE);

    // Nền đen
    for (int i = 0; i < LCD_H_RES * LCD_V_RES; i++) {
        buf[i] = c_black;
    }

    // Khung viền bo góc trang trí
    fill_rect(buf, LCD_H_RES, LCD_V_RES, 10, 10, LCD_H_RES - 11, 12, c_cyan);
    fill_rect(buf, LCD_H_RES, LCD_V_RES, 10, LCD_V_RES - 13, LCD_H_RES - 11, LCD_V_RES - 11, c_cyan);
    fill_rect(buf, LCD_H_RES, LCD_V_RES, 10, 10, 12, LCD_V_RES - 11, c_cyan);
    fill_rect(buf, LCD_H_RES, LCD_V_RES, LCD_H_RES - 13, 10, LCD_H_RES - 11, LCD_V_RES - 11, c_cyan);

    // Tiêu đề: XIN CHAO
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 45, "XIN CHAO", c_yellow, 3);

    // Đường gạch ngang trang trí
    fill_rect(buf, LCD_H_RES, LCD_V_RES, 40, 78, LCD_H_RES - 41, 80, c_cyan);

    // Dòng thông báo hướng dẫn
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 100, "Vui long dat", c_white, 2);
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 122, "khuon mat vao", c_white, 2);
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 144, "khung trong", c_white, 2);

    // Nổi bật thời gian: [ 20 GIAY ]
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 178, "[ 20 GIAY ]", c_yellow, 2);

    st7789_draw_frame(panel_handle, 0, 0, LCD_H_RES, LCD_V_RES, buf);
}

void st7789_draw_face_overlay(uint16_t *buf, int width, int height, int remaining_sec, const char *status_msg)
{
    if (!buf) return;

    uint16_t c_bracket = (remaining_sec <= 3) ? SWAP16(COLOR_GREEN) : SWAP16(COLOR_CYAN);
    uint16_t c_yellow  = SWAP16(COLOR_YELLOW);
    uint16_t c_black   = SWAP16(COLOR_BLACK);
    uint16_t c_dark    = SWAP16(0x18E3);

    // 1. Tọa độ khung căn chỉnh khuôn mặt: (x: 45 -> 195, y: 25 -> 195) (150x170 px)
    int bx0 = 45, by0 = 25, bx1 = 195, by1 = 195;
    int arm_len = 24; // Chiều dài góc
    int thick = 3;    // Độ dày viền góc

    // Góc trên - trái ╔
    fill_rect(buf, width, height, bx0, by0, bx0 + arm_len, by0 + thick - 1, c_bracket);
    fill_rect(buf, width, height, bx0, by0, bx0 + thick - 1, by0 + arm_len, c_bracket);

    // Góc trên - phải ╗
    fill_rect(buf, width, height, bx1 - arm_len, by0, bx1, by0 + thick - 1, c_bracket);
    fill_rect(buf, width, height, bx1 - thick + 1, by0, bx1, by0 + arm_len, c_bracket);

    // Góc dưới - trái ╚
    fill_rect(buf, width, height, bx0, by1 - thick + 1, bx0 + arm_len, by1, c_bracket);
    fill_rect(buf, width, height, bx0, by1 - arm_len, bx0 + thick - 1, by1, c_bracket);

    // Góc dưới - phải ╝
    fill_rect(buf, width, height, bx1 - arm_len, by1 - thick + 1, bx1, by1, c_bracket);
    fill_rect(buf, width, height, bx1 - thick + 1, by1 - arm_len, bx1, by1, c_bracket);

    // 2. Điểm chữ thập ngắm tâm nhẹ
    int cx = 120, cy = 110;
    fill_rect(buf, width, height, cx - 6, cy, cx + 6, cy, c_bracket);
    fill_rect(buf, width, height, cx, cy - 6, cx, cy + 6, c_bracket);

    // 3. Thanh tiêu đề phía trên
    fill_rect(buf, width, height, 0, 0, width - 1, 18, c_black);
    if (status_msg && strlen(status_msg) > 0) {
        draw_string_centered(buf, width, height, 5, status_msg, c_yellow, 1);
    } else {
        draw_string_centered(buf, width, height, 5, "CAN MAT VAO KHUNG", c_yellow, 1);
    }

    // 4. Khối đếm ngược 20s hiển thị ở đáy
    fill_rect(buf, width, height, 0, 204, width - 1, height - 1, c_black);

    char timer_str[32];
    snprintf(timer_str, sizeof(timer_str), "%ds", remaining_sec);
    uint16_t time_color = (remaining_sec <= 5) ? SWAP16(COLOR_RED) : c_yellow;
    draw_string_centered(buf, width, height, 206, timer_str, time_color, 2);

    // 5. Thanh tiến trình (Progress Bar: 20s -> 0s)
    int pb_x0 = 25, pb_x1 = width - 26, pb_y0 = 227, pb_y1 = 231;
    fill_rect(buf, width, height, pb_x0, pb_y0, pb_x1, pb_y1, c_dark);

    int max_w = pb_x1 - pb_x0;
    int cur_w = (remaining_sec * max_w) / 20;
    if (cur_w > 0) {
        fill_rect(buf, width, height, pb_x0, pb_y0, pb_x0 + cur_w, pb_y1, (remaining_sec <= 5) ? SWAP16(COLOR_RED) : SWAP16(COLOR_GREEN));
    }
}

void st7789_show_ai_processing_screen(esp_lcd_panel_handle_t panel_handle, uint16_t *buf)
{
    if (!buf) return;

    uint16_t c_navy  = SWAP16(0x0014);
    uint16_t c_cyan  = SWAP16(COLOR_CYAN);
    uint16_t c_white = SWAP16(COLOR_WHITE);

    for (int i = 0; i < LCD_H_RES * LCD_V_RES; i++) {
        buf[i] = c_navy;
    }

    fill_rect(buf, LCD_H_RES, LCD_V_RES, 20, 20, LCD_H_RES - 21, LCD_V_RES - 21, SWAP16(COLOR_BLACK));
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 70, "DANG XAC THUC AI", c_cyan, 2);
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 110, "Doi chieu anh da dang ky", c_white, 1);
    draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 140, "Vui long cho giay lat", c_white, 1);

    st7789_draw_frame(panel_handle, 0, 0, LCD_H_RES, LCD_V_RES, buf);
}

void st7789_show_result_screen(esp_lcd_panel_handle_t panel_handle, uint16_t *buf, bool granted, const char *user_name, float confidence)
{
    if (!buf) return;

    uint16_t bg_color = granted ? SWAP16(0x0400) : SWAP16(0x8000); // Dark Green hoặc Dark Red
    uint16_t c_white  = SWAP16(COLOR_WHITE);
    uint16_t c_yellow = SWAP16(COLOR_YELLOW);

    for (int i = 0; i < LCD_H_RES * LCD_V_RES; i++) {
        buf[i] = bg_color;
    }

    if (granted) {
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 40, "HOP LE!", c_yellow, 3);
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 80, "MO KHOA CUA", c_white, 2);

        char name_buf[48];
        snprintf(name_buf, sizeof(name_buf), "%s", (user_name && strlen(user_name) > 0) ? user_name : "Thanh vien");
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 120, name_buf, c_yellow, 2);

        char conf_buf[32];
        snprintf(conf_buf, sizeof(conf_buf), "Diem khop: %.3f", confidence);
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 160, conf_buf, c_white, 1);
    } else {
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 45, "TU CHOI!", c_white, 3);
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 90, "CANH BAO NGUOI LA", c_yellow, 2);
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 130, "Khong the mo cua", c_white, 1);

        char conf_buf[32];
        snprintf(conf_buf, sizeof(conf_buf), "Diem khop: %.3f", confidence);
        draw_string_centered(buf, LCD_H_RES, LCD_V_RES, 160, conf_buf, c_white, 1);
    }

    st7789_draw_frame(panel_handle, 0, 0, LCD_H_RES, LCD_V_RES, buf);
}
