/*
 * SPDX-License-Identifier: MIT
 */
#include "hal_appendages.h"
#include "soma_config.h"
#include <cJSON.h>
#include <driver/gpio.h>
#include <driver/ledc.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <mutex>
#include <string>
#include <vector>

namespace {

constexpr const char* kTag = "APPENDAGES";

constexpr size_t kMaxAppendages = 8;
constexpr size_t kMaxName       = 24;

// The pins nothing in the body uses (docs/appendages.md, the audit and the schematics, 18 Nov 2026).
// G17 and G18 are Port C, which the LLM module takes when it is in the stack; nothing here can
// know that, so the list says what the body leaves free and no more.
constexpr int kFreePins[] = {1, 2, 8, 9, 17, 18};

// A flashlight needs a PWM timer of its own, because two lights at different levels run at
// different rates. Nothing else in this firmware uses LEDC, so two is a choice, not a limit hit.
constexpr ledc_timer_t kLightTimers[]     = {LEDC_TIMER_3, LEDC_TIMER_2};
constexpr ledc_channel_t kLightChannels[] = {LEDC_CHANNEL_6, LEDC_CHANNEL_7};
constexpr size_t kMaxLights               = 2;
constexpr uint32_t kDutyFull              = 1u << 12;

// Every "on" ends by itself. A light asked for with no time gets the default; nothing gets more than the cap.
constexpr int kDefaultMs = 5000;
constexpr int kMaxMs     = 10 * 60 * 1000;

/*
 * What "level" means: a share of plain torch's LIGHT, measured, not a share of the time the pin
 * is high. Measured on 19 Nov 2026 with the robot's own ambient light sensor (GET /api/ambient)
 * and a stock light in torch mode aimed at it:
 *
 *   - Every pulse loses about 48 us of light at its start (the chip turning on). So a pulse of
 *     50 us or less makes none, 60 us makes 3 us of light, 100 us makes 52. kLit is that table.
 *   - The gap between pulses can be any length: a 100 us pulse still gave its 50 us of light
 *     with 50 ms between pulses. There is no cold-start dead zone.
 *   - A gap shorter than about 60 us is not seen at all: the light stays on through it. So no
 *     pulse here runs longer than 850 us of a 1 ms cycle; above that it is plain torch.
 *
 * And from Chad's eyes the night before, in a dark office: 0.3 to 3% of torch is comfortable to
 * have pointed at you, 5% and up is far too bright.
 *
 * So level 100 is the enable line held high. Level 1 is 0.6% of torch, and the levels between
 * climb by equal ratios, which is what looks like even steps. Three ways of making the light:
 *   5.2% and up      1 kHz, the pulse as long as the light wanted needs
 *   1.04% to 5.2%    a 100 us pulse (52 us of light), the gap grown, down to 200 Hz
 *   under 1.04%      200 Hz, the pulse shortened toward 80 us. Slower than 200 Hz would be
 *                    steadier, but it starts to flicker when the eye moves.
 * The bottom is the least exact part: at 80 us, 5 us of drift in the chip's turn-on is a sixth
 * of the light.
 */
constexpr double kFloorLight = 0.006;
constexpr double kSlowestUs  = 5000.0;  // 200 Hz
constexpr double kPulseUs    = 100.0;
constexpr double kPulseLitUs = 52.0;    // the light a 100 us pulse makes
constexpr double kLongestUs  = 850.0;   // of a 1 ms cycle

struct Lit_t {
    double pulse_us;
    double light_us;
};
// pulse length to the light it makes, both in microseconds, as measured
constexpr Lit_t kLit[] = {{60, 3},    {70, 18},   {80, 30},   {90, 41},   {100, 52},  {120, 74},
                          {150, 109}, {200, 160}, {419, 395}, {706, 685}, {1000, 1000}};

/** @brief The pulse that makes this much light, by the table; outside it, the nearest end. */
double pulse_for(double light_us)
{
    const size_t n = sizeof(kLit) / sizeof(kLit[0]);
    if (light_us <= kLit[0].light_us) {
        return kLit[0].pulse_us;
    }
    for (size_t i = 1; i < n; i++) {
        if (light_us <= kLit[i].light_us) {
            const double part = (light_us - kLit[i - 1].light_us) / (kLit[i].light_us - kLit[i - 1].light_us);
            return kLit[i - 1].pulse_us + part * (kLit[i].pulse_us - kLit[i - 1].pulse_us);
        }
    }
    return kLit[n - 1].pulse_us;
}

struct Drive_t {
    bool steady   = false;
    int hz        = 0;
    uint32_t duty = 0;
    int pulse_us  = 0;
    double on     = 0;  // the part of the time the line is high, 0..1
    double light  = 0;  // the share of plain torch's light this is meant to give, 0..1; 0 for a chop
};

Drive_t drive_for(int level)
{
    Drive_t d;
    d.light         = level >= 100 ? 1.0 : kFloorLight * std::pow(1.0 / kFloorLight, (level - 1) / 99.0);
    double period   = 1000.0;
    double pulse    = kPulseUs;
    if (d.light * 1000.0 >= kPulseLitUs) {
        pulse = pulse_for(d.light * 1000.0);
    } else if (kPulseLitUs / d.light <= kSlowestUs) {
        period = kPulseLitUs / d.light;
    } else {
        period = kSlowestUs;
        pulse  = pulse_for(d.light * kSlowestUs);
    }
    if (level >= 100 || pulse > kLongestUs) {
        d.steady = true;
        d.on     = 1.0;
        d.light  = 1.0;
        return d;
    }
    d.hz       = static_cast<int>(std::lround(1e6 / period));
    d.on       = pulse * d.hz / 1e6;
    d.duty     = static_cast<uint32_t>(std::lround(d.on * kDutyFull));
    d.pulse_us = static_cast<int>(std::lround(pulse));
    return d;
}

struct Appendage_t {
    std::string name;
    std::string kind;
    std::string side;
    std::vector<int> pins;
    bool ok = false;
    std::string error;

    // flashlight
    int slot                 = -1;
    esp_timer_handle_t timer = nullptr;
    bool on                  = false;
    int asked                = 0;  // the level asked for
    int level                = 0;  // the level it runs at, after the night ceiling
    Drive_t drive;
    bool raw       = false;  // a "chop" in hand, not a level
    int64_t off_at = 0;      // esp_timer time, microseconds
};

// Filled once in start(), never resized after: the timers hold indexes into it.
std::vector<Appendage_t> _all;
std::mutex _lock;  // the server's task and the timer task both come here
int _light_cap = 100;

std::string trim(const std::string& s)
{
    size_t a = 0;
    size_t b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r')) {
        ++a;
    }
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r')) {
        --b;
    }
    return s.substr(a, b - a);
}

bool whole_number(const std::string& text, int& out)
{
    if (text.empty()) {
        return false;
    }
    char* end        = nullptr;
    const long value = std::strtol(text.c_str(), &end, 10);
    if (end == nullptr || *end != '\0' || value < 0 || value > 255) {
        return false;
    }
    out = static_cast<int>(value);
    return true;
}

bool is_free_pin(int pin)
{
    return std::find(std::begin(kFreePins), std::end(kFreePins), pin) != std::end(kFreePins);
}

void refuse(Appendage_t& a, const std::string& why)
{
    if (a.ok || a.error.empty()) {
        a.error = why;
    }
    a.ok = false;
}

/** @brief One saved line, "name: kind key=value ...", into an appendage that has claimed nothing yet. */
Appendage_t read_line(const std::string& line)
{
    Appendage_t a;
    const size_t colon = line.find(':');
    a.name             = trim(line.substr(0, colon));
    std::string rest   = colon == std::string::npos ? "" : trim(line.substr(colon + 1));
    a.ok               = true;

    if (a.name.empty() || a.name.size() > kMaxName ||
        a.name.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789_-") != std::string::npos) {
        refuse(a, "a name is 1 to 24 of a-z, 0-9, _ and -");
    }

    std::vector<std::string> words;
    size_t at = 0;
    while (at < rest.size()) {
        const size_t space = rest.find_first_of(" \t", at);
        const size_t end   = space == std::string::npos ? rest.size() : space;
        if (end > at) {
            words.push_back(rest.substr(at, end - at));
        }
        at = end + 1;
    }
    if (words.empty()) {
        refuse(a, "no kind: a line is \"name: kind key=value ...\"");
        return a;
    }
    a.kind = words[0];
    if (a.kind != "flashlight") {
        refuse(a, "this build has no kind \"" + a.kind + "\"; it has: flashlight");
        return a;
    }

    int en = -1;
    for (size_t i = 1; i < words.size(); i++) {
        const size_t eq         = words[i].find('=');
        const std::string key   = words[i].substr(0, eq);
        const std::string value = eq == std::string::npos ? "" : words[i].substr(eq + 1);
        if (key == "en") {
            if (!whole_number(value, en)) {
                refuse(a, "en= wants a pin number");
            }
        } else if (key == "side") {
            a.side = value;
        } else {
            refuse(a, "a flashlight takes en= and side=, not \"" + key + "\" (the mod's flash= is not built yet)");
        }
    }
    if (en < 0) {
        refuse(a, "a flashlight needs en=, the pin on its yellow wire");
    } else {
        a.pins.push_back(en);
        if (!is_free_pin(en)) {
            refuse(a, "pin " + std::to_string(en) + " is not free: the body leaves 1, 2, 8, 9, 17 and 18");
        }
    }
    return a;
}

void light_low(Appendage_t& a)
{
    if (a.slot >= 0) {
        ledc_stop(LEDC_LOW_SPEED_MODE, kLightChannels[a.slot], 0);
    }
    a.on     = false;
    a.raw    = false;
    a.off_at = 0;
}

void on_light_timer(void* arg)
{
    const size_t index = reinterpret_cast<size_t>(arg);
    std::lock_guard<std::mutex> guard(_lock);
    // A new "on" that arrived while this was waiting for the lock has its own, later, off.
    if (index < _all.size() && esp_timer_get_time() >= _all[index].off_at - 1000) {
        light_low(_all[index]);
    }
}

/** @brief Pin low, then a PWM channel on it stopped low, then the timer that ends every "on". */
bool light_setup(Appendage_t& a, size_t index)
{
    const auto pin = static_cast<gpio_num_t>(a.pins[0]);
    gpio_set_level(pin, 0);
    gpio_set_direction(pin, GPIO_MODE_OUTPUT);

    ledc_timer_config_t timer = {};
    timer.speed_mode          = LEDC_LOW_SPEED_MODE;
    timer.duty_resolution     = LEDC_TIMER_12_BIT;
    timer.timer_num           = kLightTimers[a.slot];
    timer.freq_hz             = 1000;
    timer.clk_cfg             = LEDC_AUTO_CLK;
    if (ledc_timer_config(&timer) != ESP_OK) {
        return false;
    }
    ledc_channel_config_t channel = {};
    channel.gpio_num              = pin;
    channel.speed_mode            = LEDC_LOW_SPEED_MODE;
    channel.channel               = kLightChannels[a.slot];
    channel.timer_sel             = kLightTimers[a.slot];
    channel.duty                  = 0;
    if (ledc_channel_config(&channel) != ESP_OK) {
        return false;
    }
    ledc_stop(LEDC_LOW_SPEED_MODE, kLightChannels[a.slot], 0);

    esp_timer_create_args_t args = {};
    args.callback                = on_light_timer;
    args.arg                     = reinterpret_cast<void*>(index);
    args.name                    = "light-off";
    return esp_timer_create(&args, &a.timer) == ESP_OK;
}

/** @brief Put the drive on the pin. The timed off must already be armed. */
bool light_drive(Appendage_t& a, const Drive_t& d)
{
    const ledc_channel_t channel = kLightChannels[a.slot];
    esp_err_t err                = ESP_OK;
    if (d.steady) {
        err = ledc_stop(LEDC_LOW_SPEED_MODE, channel, 1);
    } else {
        err = ledc_set_freq(LEDC_LOW_SPEED_MODE, kLightTimers[a.slot], d.hz);
        if (err == ESP_OK) {
            err = ledc_set_duty(LEDC_LOW_SPEED_MODE, channel, d.duty);
        }
        if (err == ESP_OK) {
            err = ledc_update_duty(LEDC_LOW_SPEED_MODE, channel);
        }
    }
    return err == ESP_OK;
}

/** @brief Light on with this drive for ms, or refuse and leave it dark. Never on without its timed off. */
const char* light_start(Appendage_t& a, const Drive_t& d, int ms, std::string& error)
{
    esp_timer_stop(a.timer);
    if (esp_timer_start_once(a.timer, static_cast<uint64_t>(ms) * 1000) != ESP_OK) {
        light_low(a);
        error = "could not arm the timed off, so the light stays dark";
        return "500 Internal Server Error";
    }
    if (!light_drive(a, d)) {
        esp_timer_stop(a.timer);
        light_low(a);
        error = "the pin would not take that; the light is off";
        return "500 Internal Server Error";
    }
    a.on     = true;
    a.drive  = d;
    a.off_at = esp_timer_get_time() + static_cast<int64_t>(ms) * 1000;
    return nullptr;
}

int number_in(const cJSON* body, const char* key, int fallback)
{
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(body, key);
    return cJSON_IsNumber(item) ? item->valueint : fallback;
}

void add_state(cJSON* into, const Appendage_t& a)
{
    cJSON* state = cJSON_AddObjectToObject(into, "state");
    cJSON_AddBoolToObject(state, "on", a.on);
    if (!a.on) {
        return;
    }
    if (!a.raw) {
        cJSON_AddNumberToObject(state, "level", a.level);
        if (a.asked != a.level) {
            cJSON_AddNumberToObject(state, "asked", a.asked);  // the night ceiling held it lower
        }
    }
    cJSON_AddBoolToObject(state, "steady", a.drive.steady);
    if (a.drive.light > 0) {
        // what the level is meant to give, as a share of plain torch's light
        cJSON_AddNumberToObject(state, "lightPercent", std::round(a.drive.light * 1000.0) / 10.0);
    }
    cJSON_AddNumberToObject(state, "onPercent", std::round(a.drive.on * 1000.0) / 10.0);
    if (!a.drive.steady) {
        cJSON_AddNumberToObject(state, "hz", a.drive.hz);
        cJSON_AddNumberToObject(state, "pulseUs", a.drive.pulse_us);
    }
    const int64_t left = a.off_at - esp_timer_get_time();
    cJSON_AddNumberToObject(state, "msLeft", static_cast<double>(left > 0 ? left / 1000 : 0));
}

const char* light_act(Appendage_t& a, const std::string& action, const cJSON* body, std::string& error)
{
    if (action == "off") {
        esp_timer_stop(a.timer);
        light_low(a);
        return nullptr;
    }
    const int ms = number_in(body, "ms", kDefaultMs);
    if (ms < 50 || ms > kMaxMs) {
        error = "ms is how long it stays on: 50 to 600000, or leave it out for 5000";
        return "400 Bad Request";
    }
    if (action == "on" || action == "level") {
        const int asked = number_in(body, "level", a.asked > 0 ? a.asked : 1);
        if (asked < 1 || asked > 100) {
            error = "level is 1 to 100: 1 is the dimmest it goes (0.6% of torch), 100 is plain torch";
            return "400 Bad Request";
        }
        const int level = std::max(1, std::min(asked, _light_cap));
        if (_light_cap <= 0) {
            esp_timer_stop(a.timer);
            light_low(a);
            error = "the night ceiling for lights is 0, so it stays off";
            return "409 Conflict";
        }
        const char* status = light_start(a, drive_for(level), ms, error);
        if (status == nullptr) {
            a.asked = asked;
            a.level = level;
            a.raw   = false;
        }
        return status;
    }
    if (action == "chop") {
        // The bench's own drive, for measuring the chip: a rate and an on-time by hand, no level
        // and no night ceiling. It is how the level table above was found.
        const int hz   = number_in(body, "hz", 0);
        const int duty = number_in(body, "duty", 0);
        if (hz < 20 || hz > 10000 || duty < 1 || duty > 99) {
            error = "to chop: hz 20 to 10000 and duty 1 to 99 (percent high)";
            return "400 Bad Request";
        }
        Drive_t d;
        d.hz               = hz;
        d.on               = duty / 100.0;
        d.duty             = kDutyFull * duty / 100;
        d.pulse_us         = static_cast<int>(std::lround(d.on * 1e6 / hz));
        const char* status = light_start(a, d, ms, error);
        if (status == nullptr) {
            a.raw = true;
        }
        return status;
    }
    error = "a flashlight knows: on, off, level, chop";
    return "400 Bad Request";
}

}  // namespace

void appendages::start()
{
    std::lock_guard<std::mutex> guard(_lock);
    if (!_all.empty()) {
        return;
    }
    const std::string saved = soma_config::appendages();
    size_t at               = 0;
    while (at < saved.size() && _all.size() < kMaxAppendages) {
        const size_t nl        = saved.find('\n', at);
        const size_t end       = nl == std::string::npos ? saved.size() : nl;
        const std::string line = trim(saved.substr(at, end - at));
        at                     = end + 1;
        if (!line.empty()) {
            _all.push_back(read_line(line));
        }
    }
    if (_all.empty()) {
        ESP_LOGI(kTag, "none configured");
        return;
    }

    // A name or a pin belongs to one appendage. Two claiming it means both are refused.
    for (size_t i = 0; i < _all.size(); i++) {
        for (size_t j = 0; j < _all.size(); j++) {
            if (i == j) {
                continue;
            }
            if (_all[i].name == _all[j].name) {
                refuse(_all[i], "the name \"" + _all[i].name + "\" is used twice");
            }
            for (int pin : _all[i].pins) {
                if (std::find(_all[j].pins.begin(), _all[j].pins.end(), pin) != _all[j].pins.end()) {
                    refuse(_all[i], "pin " + std::to_string(pin) + " is also claimed by \"" + _all[j].name + "\"");
                }
            }
        }
    }

    size_t lights = 0;
    for (size_t i = 0; i < _all.size(); i++) {
        Appendage_t& a = _all[i];
        if (a.ok && a.kind == "flashlight") {
            if (lights >= kMaxLights) {
                refuse(a, "this build drives two flashlights at most");
            } else {
                a.slot = static_cast<int>(lights++);
                if (!light_setup(a, i)) {
                    a.slot = -1;
                    refuse(a, "its pin would not set up");
                }
            }
        }
        if (a.ok) {
            ESP_LOGI(kTag, "%s: %s, ready", a.name.c_str(), a.kind.c_str());
        } else {
            ESP_LOGW(kTag, "%s: refused: %s", a.name.c_str(), a.error.c_str());
        }
    }
}

cJSON* appendages::list()
{
    std::lock_guard<std::mutex> guard(_lock);
    cJSON* array = cJSON_CreateArray();
    for (const Appendage_t& a : _all) {
        cJSON* one = cJSON_CreateObject();
        cJSON_AddStringToObject(one, "name", a.name.c_str());
        cJSON_AddStringToObject(one, "kind", a.kind.c_str());
        cJSON* pins = cJSON_AddObjectToObject(one, "pins");
        if (a.kind == "flashlight" && !a.pins.empty()) {
            cJSON_AddNumberToObject(pins, "en", a.pins[0]);
        }
        if (!a.side.empty()) {
            cJSON_AddStringToObject(one, "side", a.side.c_str());
        }
        cJSON_AddBoolToObject(one, "ok", a.ok);
        if (a.ok) {
            add_state(one, a);
            if (a.kind == "flashlight") {
                cJSON* actions = cJSON_AddArrayToObject(one, "actions");
                for (const char* name : {"on", "off", "level", "chop"}) {
                    cJSON_AddItemToArray(actions, cJSON_CreateString(name));
                }
            }
        } else {
            cJSON_AddStringToObject(one, "error", a.error.c_str());
        }
        cJSON_AddItemToArray(array, one);
    }
    return array;
}

const char* appendages::act(const std::string& name, const cJSON* body, cJSON* answer, std::string& error)
{
    std::lock_guard<std::mutex> guard(_lock);
    for (Appendage_t& a : _all) {
        if (a.name != name) {
            continue;
        }
        if (!a.ok) {
            error = "\"" + name + "\" was refused at boot: " + a.error;
            return "409 Conflict";
        }
        const cJSON* action = cJSON_GetObjectItemCaseSensitive(body, "action");
        if (!cJSON_IsString(action)) {
            error = "expected {\"action\": \"...\"}";
            return "400 Bad Request";
        }
        const char* status = light_act(a, action->valuestring, body, error);
        if (status == nullptr) {
            cJSON_AddStringToObject(answer, "name", a.name.c_str());
            add_state(answer, a);
        }
        return status;
    }
    error = "no appendage is called \"" + name + "\"";
    return "404 Not Found";
}

void appendages::set_light_cap(int level)
{
    std::lock_guard<std::mutex> guard(_lock);
    level = std::max(0, std::min(level, 100));
    if (level == _light_cap) {
        return;
    }
    _light_cap = level;
    // A light already on comes down to the new ceiling now; it never climbs by itself when one lifts.
    for (Appendage_t& a : _all) {
        if (!a.ok || !a.on || a.raw || a.level <= level) {
            continue;
        }
        if (level <= 0) {
            esp_timer_stop(a.timer);
            light_low(a);
        } else if (light_drive(a, drive_for(level))) {
            a.level = level;
            a.drive = drive_for(level);
        }
    }
}
