#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"

#define LCD_H_RES              240
#define LCD_V_RES              240

#define LCD_HOST               SPI2_HOST
#define PIN_NUM_LCD_MOSI       38
#define PIN_NUM_LCD_PCLK       39
#define PIN_NUM_LCD_CS         40
#define PIN_NUM_LCD_DC         41
#define PIN_NUM_LCD_RST        42
#define PIN_NUM_BK_LIGHT        2

/* Colors (RGB565 raw) */
#define COLOR_BLACK            0x0000
#define COLOR_WHITE            0xFFFF
#define COLOR_RED              0xF800
#define COLOR_GREEN            0x07E0
#define COLOR_BLUE             0x001F
#define COLOR_YELLOW           0xFFE0
#define COLOR_CYAN             0x07FF
#define COLOR_ORANGE           0xFD20
#define COLOR_DARKGREY         0x31A6

esp_err_t st7789_init(esp_lcd_panel_handle_t *panel_handle);
void st7789_set_backlight(bool enable);
void st7789_draw_frame(esp_lcd_panel_handle_t panel_handle, int x_start, int y_start, int x_end, int y_end, const void *color_data);
void st7789_fill_color(esp_lcd_panel_handle_t panel_handle, uint16_t color_rgb565);

/* Graphics and UI functions using external PSRAM render buffer */
void st7789_show_welcome_screen(esp_lcd_panel_handle_t panel_handle, uint16_t *buf);
void st7789_draw_face_overlay(uint16_t *buf, int width, int height, int remaining_sec, const char *status_msg);
void st7789_show_result_screen(esp_lcd_panel_handle_t panel_handle, uint16_t *buf, bool granted, const char *user_name, float confidence);
void st7789_show_ai_processing_screen(esp_lcd_panel_handle_t panel_handle, uint16_t *buf);
