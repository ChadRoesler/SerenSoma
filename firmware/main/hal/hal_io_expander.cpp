/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "hal.h"
#include "board/hal_bridge.h"
#include "drivers/PY32IOExpander_Class/PY32IOExpander_Class.hpp"
#include <mooncake_log.h>
#include <memory>
#include <mutex>
#include <algorithm>

static const std::string_view _tag = "HAL-IOE";

static std::unique_ptr<m5::PY32IOExpander_Class> _io_expander;

void Hal::io_expander_init()
{
    mclog::tagInfo(_tag, "init");

    auto i2c_bus        = hal_bridge::board_get_i2c_bus();
    _io_expander        = std::make_unique<m5::PY32IOExpander_Class>(i2c_bus);
    uint32_t start_tick = GetHAL().millis();

    // PY32 IO Expander may boot slowly, wait for it
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(200));

        if (GetHAL().millis() - start_tick > 1200) {
            mclog::tagError(_tag, "init timeout");
            _io_expander.reset();
            break;
        }

        if (_io_expander->begin()) {
            break;
        }
        mclog::tagInfo(_tag, "init failed, retrying...");
    }

    if (_io_expander) {
        // VM EN
        _io_expander->setDirection(0, true);  // Output
        _io_expander->setPullMode(0, true);   // Pull-up
        GetHAL().setServoPowerEnabled(true);
        vTaskDelay(pdMS_TO_TICKS(200));

        // RGB
        _io_expander->setDirection(13, true);   // Output
        _io_expander->setPullMode(13, true);    // Pull-up
        _io_expander->setDriveMode(13, false);  // Push-pull
        _io_expander->setLedCount(12);
        vTaskDelay(pdMS_TO_TICKS(200));
        GetHAL().showRgbColor(0, 0, 0);
        vTaskDelay(pdMS_TO_TICKS(50));
        GetHAL().showRgbColor(0, 0, 0);

        mclog::tagInfo(_tag, "init done");
    }
}

void Hal::setServoPowerEnabled(bool enabled)
{
    if (!_io_expander) {
        return;
    }
    _io_expander->digitalWrite(0, enabled ? true : false);
}

namespace {
constexpr int kRgbCount = 12;
struct RgbLayer_t {
    uint8_t wanted[3] = {0, 0, 0};  // what was last asked for
    uint8_t held[3]   = {0, 0, 0};
    bool isHeld       = false;
};
RgbLayer_t rgb_layers[kRgbCount];
std::mutex rgb_mutex;  // the camera light must not lose a race to a light show
int rgb_dimmer = 100;

// 16 survives the LEDs' 5-bit red and blue: a dimmed camera light is dim, never dark.
constexpr int kHeldFloor = 16;

uint8_t dimmed(uint8_t v, bool held)
{
    int out = v * rgb_dimmer / 100;
    if (held && v > 0) {
        out = std::max(out, std::min<int>(v, kHeldFloor));
    }
    return static_cast<uint8_t>(out);
}
}  // namespace

void Hal::setRgbColor(uint8_t index, uint8_t r, uint8_t g, uint8_t b)
{
    if (!_io_expander || index >= kRgbCount) {
        return;
    }
    std::lock_guard<std::mutex> lock(rgb_mutex);
    auto& led     = rgb_layers[index];
    led.wanted[0] = r;
    led.wanted[1] = g;
    led.wanted[2] = b;
    if (!led.isHeld) {
        _io_expander->setLedColor(index, dimmed(r, false), dimmed(g, false), dimmed(b, false));
    }
}

void Hal::holdRgb(uint8_t index, uint8_t r, uint8_t g, uint8_t b)
{
    if (!_io_expander || index >= kRgbCount) {
        return;
    }
    std::lock_guard<std::mutex> lock(rgb_mutex);
    auto& led   = rgb_layers[index];
    led.held[0] = r;
    led.held[1] = g;
    led.held[2] = b;
    led.isHeld  = true;
    _io_expander->setLedColor(index, dimmed(r, true), dimmed(g, true), dimmed(b, true));
}

void Hal::releaseRgb(uint8_t index)
{
    if (!_io_expander || index >= kRgbCount) {
        return;
    }
    std::lock_guard<std::mutex> lock(rgb_mutex);
    auto& led  = rgb_layers[index];
    led.isHeld = false;
    _io_expander->setLedColor(index, dimmed(led.wanted[0], false), dimmed(led.wanted[1], false), dimmed(led.wanted[2], false));
}

void Hal::setRgbDimmer(int percent)
{
    std::lock_guard<std::mutex> lock(rgb_mutex);
    percent = std::max(0, std::min(100, percent));
    if (percent == rgb_dimmer) {
        return;
    }
    rgb_dimmer = percent;
    if (!_io_expander) {
        return;
    }
    for (int i = 0; i < kRgbCount; i++) {
        const auto& led = rgb_layers[i];
        const auto* c   = led.isHeld ? led.held : led.wanted;
        _io_expander->setLedColor(i, dimmed(c[0], led.isHeld), dimmed(c[1], led.isHeld), dimmed(c[2], led.isHeld));
    }
    _io_expander->refreshLeds();
}

int Hal::rgbDimmer()
{
    std::lock_guard<std::mutex> lock(rgb_mutex);
    return rgb_dimmer;
}

void Hal::shownRgb(uint8_t index, uint8_t& r, uint8_t& g, uint8_t& b)
{
    r = g = b = 0;
    if (index >= kRgbCount) {
        return;
    }
    std::lock_guard<std::mutex> lock(rgb_mutex);
    const auto& led = rgb_layers[index];
    const auto* c   = led.isHeld ? led.held : led.wanted;
    r               = dimmed(c[0], led.isHeld);
    g               = dimmed(c[1], led.isHeld);
    b               = dimmed(c[2], led.isHeld);
}

void Hal::refreshRgb()
{
    if (!_io_expander) {
        return;
    }
    _io_expander->refreshLeds();
}

void Hal::showRgbColor(uint8_t r, uint8_t g, uint8_t b)
{
    for (int i = 0; i < 12; i++) {
        setRgbColor(i, r, g, b);
    }
    refreshRgb();
}
