/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "hal.h"
#include "touch_log.h"
#include <atomic>
#include <cstdio>
#include <esp_heap_caps.h>
#include "drivers/Si12T/Si12T.h"
#include "board/hal_bridge.h"
#include <mooncake_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

static const std::string_view _tag = "HAL-HeadTouch";

// 触摸状态
enum class TouchState { IDLE, TOUCHED, SWIPING };

// 配置参数
struct TouchConfig {
    uint8_t touch_threshold = 1;
    int16_t swipe_threshold = 40;  // 使用百分比，范围-100到100
};

// 触摸数据
struct TouchData {
    uint8_t intensity[3];
    uint32_t timestamp;

    // 计算位置（返回-100到100的整数）
    int16_t get_position() const
    {
        uint16_t total = intensity[0] + intensity[1] + intensity[2];
        if (total == 0) return 0;

        int32_t weighted = intensity[0] * (-100) + intensity[1] * 0 + intensity[2] * 100;
        return static_cast<int16_t>(weighted / total);
    }

    uint8_t get_max_intensity() const
    {
        uint8_t max_val = intensity[0];
        if (intensity[1] > max_val) max_val = intensity[1];
        if (intensity[2] > max_val) max_val = intensity[2];
        return max_val;
    }

    bool is_touched() const
    {
        return get_max_intensity() >= 1;
    }
};

// 手势识别器类
class GestureRecognizer {
public:
    GestureRecognizer() : current_state(TouchState::IDLE), initial_position(0)
    {
    }

    // 更新状态机，返回识别到的手势
    HeadPetGesture update(const TouchData& data)
    {
        HeadPetGesture gesture = HeadPetGesture::None;

        switch (current_state) {
            case TouchState::IDLE:
                if (data.is_touched()) {
                    current_state    = TouchState::TOUCHED;
                    initial_position = data.get_position();
                    gesture          = HeadPetGesture::Press;
                    // mclog::tagInfo(_tag, "Touch detected at position: {}", initial_position);
                }
                break;

            case TouchState::TOUCHED:
                if (!data.is_touched()) {
                    current_state = TouchState::IDLE;
                    gesture       = HeadPetGesture::Release;
                } else {
                    // Check for swipe
                    int16_t current_pos = data.get_position();
                    int16_t delta       = current_pos - initial_position;

                    if (delta > config.swipe_threshold) {
                        current_state = TouchState::SWIPING;
                        gesture       = HeadPetGesture::SwipeForward;
                        // mclog::tagInfo(_tag, "Swipe forward detected, delta: {}", delta);
                    } else if (delta < -config.swipe_threshold) {
                        current_state = TouchState::SWIPING;
                        gesture       = HeadPetGesture::SwipeBackward;
                        // mclog::tagInfo(_tag, "Swipe backward detected, delta: {}", delta);
                    }
                }
                break;

            case TouchState::SWIPING:
                if (!data.is_touched()) {
                    current_state = TouchState::IDLE;
                    gesture       = HeadPetGesture::Release;
                }
                break;
        }

        return gesture;
    }

    void set_config(const TouchConfig& cfg)
    {
        config = cfg;
    }

private:
    TouchConfig config;
    TouchState current_state;
    int16_t initial_position;
};

// ---------------------------------------------------------------- the raw feed
namespace {
struct TouchSample_t {
    uint32_t ms;
    uint8_t zones[3];
    uint8_t gesture;
};
constexpr uint32_t kTouchSamples = 2048;
TouchSample_t* _touch_samples    = nullptr;
std::atomic<uint32_t> _touch_count{0};  // the sensor task and the screen both add
}  // namespace

void touch_log::add(uint32_t ms, const uint8_t zones[3], int gesture)
{
    if (_touch_samples == nullptr) {
        _touch_samples = static_cast<TouchSample_t*>(heap_caps_malloc(kTouchSamples * sizeof(TouchSample_t), MALLOC_CAP_SPIRAM));
        if (_touch_samples == nullptr) {
            return;
        }
    }
    TouchSample_t& s = _touch_samples[_touch_count.fetch_add(1) % kTouchSamples];
    s.ms             = ms;
    s.zones[0]       = zones[0];
    s.zones[1]       = zones[1];
    s.zones[2]       = zones[2];
    s.gesture        = static_cast<uint8_t>(gesture);
}

size_t touch_log::read(uint32_t since_ms, char* out, size_t capacity)
{
    if (_touch_samples == nullptr) {
        return 0;
    }
    const uint32_t count = _touch_count.load();
    size_t used          = 0;
    for (uint32_t i = count > kTouchSamples ? count - kTouchSamples : 0; i < count; i++) {
        const TouchSample_t& s = _touch_samples[i % kTouchSamples];
        if (s.ms <= since_ms) {
            continue;
        }
        if (capacity - used < 40) {
            break;
        }
        used += std::snprintf(out + used, capacity - used, "%lu %u %u %u %u\n", (unsigned long)s.ms, s.zones[0], s.zones[1],
                              s.zones[2], s.gesture);
    }
    return used;
}

// ---------------------------------------------------------------- the screen's touches
namespace {
screen_touch::Touch_t _screen_touches[screen_touch::kKept];
std::atomic<uint32_t> _screen_count{0};
}  // namespace

void screen_touch::add(uint32_t ms, int x0, int y0, int x1, int y1, const char* gesture)
{
    Touch_t& t = _screen_touches[_screen_count.load() % kKept];
    t          = {ms, static_cast<int16_t>(x0), static_cast<int16_t>(y0), static_cast<int16_t>(x1), static_cast<int16_t>(y1),
                  gesture};
    _screen_count.fetch_add(1);  // after it's whole, so a reader never sees half of one
}

int screen_touch::recent(Touch_t* out)
{
    const uint32_t count = _screen_count.load();
    int n                = 0;
    for (uint32_t i = count > kKept ? count - kKept : 0; i < count; i++) {
        out[n++] = _screen_touches[i % kKept];
    }
    return n;
}

// ---------------------------------------------------------------- the bench
namespace {
si12t_handle_t _si12t = nullptr;  // set once in head_touch_init; the driver's calls take the bus lock
bool _sens_high       = false;
int _sens_level       = 3;
}  // namespace

bool touch_bench::probe(Probe_t& out)
{
    if (_si12t == nullptr) {
        return false;
    }
    uint8_t outputs[3] = {0};
    esp_err_t ret      = si12t_read_outputs(_si12t, outputs);
    ret |= si12t_read_registers(_si12t, SI12T_SENSITIVITY1_ADDR, out.sensitivity, 6);
    ret |= si12t_read_registers(_si12t, SI12T_CTRL1_ADDR, &out.ctrl1, 1);
    ret |= si12t_read_registers(_si12t, SI12T_CTRL2_ADDR, &out.ctrl2, 1);
    ret |= si12t_read_registers(_si12t, SI12T_CH_HOLD1_ADDR, out.hold, 2);
    for (int i = 0; i < 12; i++) {
        out.channels[i] = (outputs[i / 4] >> ((i % 4) * 2)) & 0x03;
    }
    out.high  = _sens_high;
    out.level = _sens_level;
    return ret == ESP_OK;
}

bool touch_bench::set_sensitivity(bool high, int level)
{
    if (_si12t == nullptr || level < 0 || level > 7) {
        return false;
    }
    if (si12t_set_sensitivity(_si12t, high ? SI12T_TYPE_HIGH : SI12T_TYPE_LOW,
                              static_cast<si12t_sensitivity_level_t>(level)) != ESP_OK) {
        return false;
    }
    _sens_high  = high;
    _sens_level = level;
    mclog::tagInfo(_tag, "bench: sensitivity {} level {}", high ? "high" : "low", level);
    return true;
}

static void _head_touch_update_task(void* param)
{
    mclog::tagInfo(_tag, "start update task");

    si12t_handle_t si12t = (si12t_handle_t)param;
    uint8_t touch_result = 0;
    TouchData data;

    GestureRecognizer recognizer;
    HeadPetGesture gesture;

    vTaskDelay(pdMS_TO_TICKS(200));

    while (1) {
        // Read data
        si12t_read_touch_result(si12t, &touch_result);
        si12t_parse_touch_result_to(touch_result, data.intensity);
        data.timestamp = xTaskGetTickCount();

        // Update and fire event
        gesture = recognizer.update(data);
        // the reading after a touch ends is kept too, so a touch's length can be measured
        static bool was_touched = false;
        if (data.is_touched() || was_touched || gesture != HeadPetGesture::None) {
            touch_log::add(GetHAL().millis(), data.intensity, static_cast<int>(gesture));
        }
        was_touched = data.is_touched();
        if (gesture != HeadPetGesture::None) {
            GetHAL().onHeadPetGesture.emit(gesture);
        }

        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

void Hal::head_touch_init()
{
    mclog::tagInfo(_tag, "init");

    auto i2c_bus = hal_bridge::board_get_i2c_bus();

    si12t_config_t si12t_cfg = {
        .i2c_bus  = i2c_bus,
        .dev_addr = SI12T_GND_ADDRESS,
    };
    static si12t_handle_t si12t;
    si12t_init(&si12t_cfg, &si12t);
    si12t_setup(si12t, SI12T_TYPE_LOW, SI12T_SENSITIVITY_LEVEL_3);
    _si12t = si12t;

    // xTaskCreateWithCaps(_head_touch_update_task, "headtouch", 1024 * 6, si12t, 2, NULL, MALLOC_CAP_SPIRAM);
    xTaskCreatePinnedToCoreWithCaps(_head_touch_update_task, "headtouch", 1024 * 6, si12t, 2, NULL, 1,
                                    MALLOC_CAP_SPIRAM);
}
