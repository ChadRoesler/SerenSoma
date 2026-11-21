/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once
#include "stackchan_camera.h"
#include <cstdint>
#include <lvgl.h>
#include <driver/i2c_master.h>
#include <string_view>

namespace hal_bridge {

struct TouchPoint_t {
    int num = 0;
    int x   = -1;
    int y   = -1;
};

struct Data_t {
    TouchPoint_t touchPoint;
    bool isXiaozhiMode              = false;
    bool isXiaozhiModeToggleEnabled = false;
};

struct XiaozhiConfig_t {
    uint32_t idleShutdownTimeSeconds = 600;
    bool allowShutdownWhenCharging   = false;
    uint8_t idleRandomMovementLevel  = 2;
    bool startAiAgentOnBoot          = false;
};

void lock();
void unlock();
Data_t& get_data();

void set_touch_point(int num, int x, int y);
TouchPoint_t get_touch_point();

bool is_xiaozhi_mode();
void set_xiaozhi_mode(bool mode);
void toggle_xiaozhi_chat_state();

void disply_lvgl_lock();
void disply_lvgl_unlock();
lv_disp_t* display_get_lvgl_display();

void xiaozhi_board_init();
void start_xiaozhi_app();
void setup_display_ui();
bool is_xiaozhi_ready();
bool is_xiaozhi_idle();
XiaozhiConfig_t get_xiaozhi_config();
void set_xiaozhi_config(const XiaozhiConfig_t& config);

i2c_master_bus_handle_t board_get_i2c_bus();
StackChanCamera* board_get_camera();
/**
 * The camera costs about 36 KB of internal RAM while it's on, so it's on only for a photo.
 * These two are the only way to turn it on and off, and they carry the red light with them,
 * so the light can't disagree with the sensor.
 */
StackChanCamera* camera_open();
void camera_close();
int board_get_battery_level();
bool board_is_battery_charging();
/**
 * What the power chip (AXP2101) measures, read from it when asked rather than cached:
 * /api/status wants the value at that moment, and a trend is whoever asks keeping them.
 * The chip measures voltages, not current. `raw` is the twelve bytes this was decoded
 * from (status 0x00-0x01, then ADC 0x34-0x3D), so a wrong decode can be seen and fixed.
 */
struct PowerDetail {
    int battery_mv       = 0;
    int usb_mv           = 0;
    int system_mv        = 0;
    int chip_temp_dc     = 0;      // tenths of a degree C
    bool usb_good        = false;  // the chip's own "input is usable", not derived
    bool battery_present = false;
    bool input_limited   = false;  // the chip is holding back because the input sags or is at its limit
    int direction        = 0;      // 0 standby, 1 into the battery, 2 out of it
    int phase            = 0;      // 0 trickle, 1 pre-charge, 2 constant current, 3 constant voltage, 4 done, 5 not charging
    uint8_t raw[12]      = {0};
    // The body's battery monitor (INA226), when there is one and it answers: the only current reading.
    bool monitor_ok      = false;
    int monitor_mv       = 0;
    int monitor_qma      = 0;      // quarter-milliamps, signed as the chip reads them
};
bool board_read_power_detail(PowerDetail& out);
void board_set_backlight_brightness(uint8_t brightness, bool permanent = false);
uint8_t board_get_backlight_brightness();
void board_set_speaker_volume(uint8_t volume, bool permanent = false);
uint8_t board_get_speaker_volume();
uint8_t board_live_speaker_volume();       // what the codec is playing at now, a night cap included
uint8_t board_saved_backlight_brightness();
int soma_setting(const char* key, int fallback);   // Soma's own, saved on the robot (NVS "soma")
void soma_set_setting(const char* key, int value);

void app_play_sound(const std::string_view& sound);
bool app_is_speaking();   // the voice channel is playing a reply right now

}  // namespace hal_bridge
