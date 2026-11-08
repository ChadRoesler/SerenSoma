/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
/*
 * LAN control API.
 *
 * A small REST surface over the same robot primitives hal_mcp.cpp exposes to
 * the assistant backend, so tools on your own network can drive the device
 * without going through the cloud. Modelled on
 * xiaozhi-esp32/main/boards/otto-robot/websocket_control_server.cc, but REST
 * rather than websocket because it is far easier to call from a script.
 *
 *   GET  /api/status            battery, wifi, heap, head angles, face name
 *   POST /api/head    {"yaw":0,"pitch":20,"speed":150}
 *   POST /api/led     {"r":0,"g":120,"b":80}
 *   POST /api/leds    {"frame":[[r,g,b],...]} from LED 0 up, or {"index":3,"r":0,"g":50,"b":0} for one
 *   POST /api/settings {} reads them; any of "dimmer", "volume", "brightness" (0~100) saves it on the robot;
 *                     "night": {"dimmer","volume","brightness"} caps them until "night": null. Never saved.
 *   POST /api/emotion {"name":"sleepy","rest":true} also makes it the pose to return to in place of neutral,
 *                     until the next /api/emotion without "rest"
 *   POST /api/emotion {"name":"happy"} and optionally, to try things out without sending a face:
 *                     "lights":[r,g,b], "lightsPulse":ms, "mouthCycle":ms, "roll":ms,
 *                     "look":{"left":[x,y],"right":[x,y]}, "brow":{"left":[y,tilt],"right":[y,tilt]}
 *   POST /api/say     {"text":"hello"}
 *   GET  /api/photo   a JPEG; the camera is on only for this, red on both sides while it is.
 *                     ?probe=1 runs the same thing and answers with sizes, no picture leaves.
 *   GET  /api/touch-raw?since=<ms>  head sensor readings, lines of "ms zone0 zone1 zone2 gesture"
 *   POST /api/face    a face bundle, X-Face-Crc32: <hex>; it installs on the next boot (face_push.h)
 *   POST /api/face-restore  on the next boot, put the previous face back
 *   POST /api/reboot
 *   POST /api/wake    open the voice channel, the same as a tap, if it's idle
 *   GET  /api/touch-spots  the current face's touch spot names, in order, and what its head touches show
 *   POST /api/react   {"name":"laughing","ms":2000}: show a pose for a moment, then go back
 *   POST /api/twitch  {"eye":"right","lid":"closed","every":[2500,6000],"flutters":3}, or {"off":true}
 *                     tries a twitch on the current pose, until the next emotion
 *
 * Anything touching the avatar or motion runs under LvglLockGuard: the
 * StackChan update task owns those on core 1 and will corrupt without it.
 *
 * SECURITY: this is an unauthenticated control surface on your LAN unless
 * CONFIG_STACKCHAN_CONTROL_TOKEN is set, in which case every request must
 * carry it in an X-StackChan-Token header.
 */
#include <atomic>
#include <cstdio>
#include <sdkconfig.h>
#include <freertos/FreeRTOS.h>
#include <freertos/idf_additions.h>
#include <freertos/semphr.h>
#include "board/hal_bridge.h"
#include "board/stackchan_camera.h"
#include <jpg/image_to_jpeg.h>
#ifdef CONFIG_STACKCHAN_CAMERA
#endif
#include "ram_map.h"
#include "face_push.h"
#include "touch_log.h"
#include "hal.h"
#include <mooncake_log.h>
#include <sdkconfig.h>

static const std::string_view _tag = "HAL-Control";

#ifdef CONFIG_STACKCHAN_CONTROL_SERVER

#include <esp_http_server.h>
#include <esp_err.h>
#include <esp_event.h>
#include <esp_netif.h>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <cJSON.h>
#include <board.h>
#include <display/display.h>
#include <stackchan/stackchan.h>
#include <apps/common/common.h>
#include <cstring>
#include <string>

using namespace stackchan;

namespace {

httpd_handle_t _server = nullptr;

bool token_ok(httpd_req_t* req)
{
    const char* expected = CONFIG_STACKCHAN_CONTROL_TOKEN;
    if (expected == nullptr || std::strlen(expected) == 0) {
        return true;  // no token configured, open on the LAN
    }

    char got[96] = {0};
    if (httpd_req_get_hdr_value_str(req, "X-StackChan-Token", got, sizeof(got)) != ESP_OK) {
        return false;
    }
    return std::strcmp(got, expected) == 0;
}

esp_err_t send_json(httpd_req_t* req, cJSON* root, const char* status = "200 OK")
{
    char* text = cJSON_PrintUnformatted(root);
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, text ? text : "{}");
    cJSON_free(text);
    cJSON_Delete(root);
    return ESP_OK;
}

// Plain text from a PSRAM buffer: cJSON's small nodes would come out of the very
// internal heap this reports on, and at 5 KB free it would fail to report at all.
esp_err_t send_text_and_free(httpd_req_t* req, char* text, size_t length)
{
    httpd_resp_set_type(req, "text/plain");
    esp_err_t err = text ? httpd_resp_send(req, text, length) : httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no memory");
    heap_caps_free(text);
    return err;
}

// Head touch, for /api/touch. Atomics: the head touch task writes, the server task reads.
std::atomic<uint32_t> _touch_pats{0};
std::atomic<uint32_t> _touch_presses{0};
std::atomic<uint32_t> _touch_last_ms{0};

esp_err_t send_error(httpd_req_t* req, const char* status, const char* message)
{
    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", false);
    cJSON_AddStringToObject(root, "error", message);
    return send_json(req, root, status);
}

/** @brief Read a JSON body. Caller owns the returned cJSON. */
cJSON* read_body(httpd_req_t* req)
{
    if (req->content_len <= 0 || req->content_len > 1024) {
        return nullptr;
    }

    std::string buffer(req->content_len, '\0');
    int received = httpd_req_recv(req, &buffer[0], req->content_len);
    if (received <= 0) {
        return nullptr;
    }
    buffer.resize(received);
    return cJSON_Parse(buffer.c_str());
}

int json_int(cJSON* root, const char* key, int fallback)
{
    cJSON* item = cJSON_GetObjectItem(root, key);
    return cJSON_IsNumber(item) ? item->valueint : fallback;
}

/* ---------------------------------- Routes --------------------------------- */

const char* reset_reason_name()
{
    switch (esp_reset_reason()) {
        case ESP_RST_POWERON:  return "power on";
        case ESP_RST_EXT:      return "external pin";
        case ESP_RST_SW:       return "software";
        case ESP_RST_PANIC:    return "panic";
        case ESP_RST_INT_WDT:  return "interrupt watchdog";
        case ESP_RST_TASK_WDT: return "task watchdog";
        case ESP_RST_WDT:      return "other watchdog";
        case ESP_RST_DEEPSLEEP: return "deep sleep";
        case ESP_RST_BROWNOUT: return "brownout";
        case ESP_RST_SDIO:     return "sdio";
        case ESP_RST_USB:      return "usb";
        case ESP_RST_JTAG:     return "jtag";
        default:               return "unknown";
    }
}

// Night caps are never saved: the voice server sends them again every minute, so a reboot at night
// stays dim, and the day setting is back the moment they're cleared.
struct NightCap_t {
    bool on        = false;
    int dimmer     = 100;
    int volume     = 100;
    int brightness = 100;
};
NightCap_t _night;
int _applied_volume     = -1;
int _applied_brightness = -1;

int capped(int mine, int cap)
{
    return _night.on ? std::min(mine, cap) : mine;
}

void apply_settings()
{
    GetHAL().setRgbDimmer(capped(hal_bridge::soma_setting("dimmer", 100), _night.dimmer));
    // only when it changes: setting the speaker writes flash
    const int volume = capped(hal_bridge::board_get_speaker_volume(), _night.volume);
    if (volume != _applied_volume) {
        hal_bridge::board_set_speaker_volume(volume, false);
        _applied_volume = volume;
    }
    const int brightness = capped(hal_bridge::board_saved_backlight_brightness(), _night.brightness);
    if (brightness != _applied_brightness) {
        hal_bridge::board_set_backlight_brightness(brightness, false);
        _applied_brightness = brightness;
    }
}

void add_settings(cJSON* root)
{
    cJSON* s = cJSON_AddObjectToObject(root, "settings");
    cJSON_AddNumberToObject(s, "dimmer", hal_bridge::soma_setting("dimmer", 100));
    cJSON_AddNumberToObject(s, "volume", hal_bridge::board_get_speaker_volume());
    cJSON_AddNumberToObject(s, "brightness", hal_bridge::board_saved_backlight_brightness());
    if (_night.on) {
        cJSON* n = cJSON_AddObjectToObject(s, "night");
        cJSON_AddNumberToObject(n, "dimmer", _night.dimmer);
        cJSON_AddNumberToObject(n, "volume", _night.volume);
        cJSON_AddNumberToObject(n, "brightness", _night.brightness);
    } else {
        cJSON_AddNullToObject(s, "night");
    }
    cJSON* a = cJSON_AddObjectToObject(s, "applied");
    cJSON_AddNumberToObject(a, "dimmer", GetHAL().rgbDimmer());
    cJSON_AddNumberToObject(a, "volume", hal_bridge::board_live_speaker_volume());
    cJSON_AddNumberToObject(a, "brightness", hal_bridge::board_get_backlight_brightness());
}

esp_err_t handle_settings(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    cJSON* body = req->content_len > 0 ? read_body(req) : nullptr;
    if (body != nullptr) {
        if (cJSON_IsNumber(cJSON_GetObjectItem(body, "dimmer"))) {
            hal_bridge::soma_set_setting("dimmer", uitk::clamp(json_int(body, "dimmer", 100), 0, 100));
        }
        if (cJSON_IsNumber(cJSON_GetObjectItem(body, "volume"))) {
            hal_bridge::board_set_speaker_volume(uitk::clamp(json_int(body, "volume", 70), 0, 100), true);
            _applied_volume = -1;
        }
        if (cJSON_IsNumber(cJSON_GetObjectItem(body, "brightness"))) {
            // 5, not 0: a dark screen in the day looks like a dead robot
            hal_bridge::board_set_backlight_brightness(uitk::clamp(json_int(body, "brightness", 75), 5, 100), true);
            _applied_brightness = -1;
        }
        cJSON* night = cJSON_GetObjectItem(body, "night");
        if (cJSON_IsObject(night)) {
            _night.on         = true;
            _night.dimmer     = uitk::clamp(json_int(night, "dimmer", 100), 0, 100);
            _night.volume     = uitk::clamp(json_int(night, "volume", 100), 0, 100);
            _night.brightness = uitk::clamp(json_int(night, "brightness", 100), 0, 100);
        } else if (cJSON_IsNull(night) || cJSON_IsFalse(night)) {
            _night.on = false;
        }
        cJSON_Delete(body);
    }
    apply_settings();
    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    add_settings(root);
    return send_json(req, root);
}

esp_err_t handle_status(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }

    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    cJSON_AddStringToObject(root, "firmware", FIRMWARE_VERSION);
    cJSON_AddNumberToObject(root, "uptimeMs", static_cast<double>(esp_timer_get_time() / 1000));
    cJSON_AddStringToObject(root, "resetReason", reset_reason_name());
    {
        // what each LED was last told to show, rrggbb from LED 0: lets a test read the rails
        char leds[12 * 6 + 1] = {0};
        for (int i = 0; i < 12; i++) {
            uint8_t r, g, b;
            GetHAL().shownRgb(i, r, g, b);
            snprintf(leds + i * 6, 7, "%02x%02x%02x", r, g, b);
        }
        cJSON_AddStringToObject(root, "leds", leds);
    }
    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar())) {
                char rig[72] = {0};
                custom->rigState(rig, sizeof(rig));
                cJSON_AddStringToObject(root, "rig", rig);
                cJSON_AddStringToObject(root, "resting", custom->resting().c_str());
            }
        }
    }
    if (*face_push::last_result() != '\0') {
        cJSON_AddStringToObject(root, "facePush", face_push::last_result());
    }
    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar())) {
                cJSON_AddNumberToObject(root, "twitches", custom->twitchCount());
            }
        }
    }
    add_settings(root);
    cJSON_AddNumberToObject(root, "batteryLevel", GetHAL().getBatteryLevel());
    cJSON_AddBoolToObject(root, "charging", GetHAL().isBatteryCharging());
    cJSON_AddNumberToObject(root, "freeInternalHeap",
                            static_cast<double>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)));
    cJSON_AddNumberToObject(root, "freePsram",
                            static_cast<double>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));

    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            cJSON_AddNumberToObject(root, "yaw", stackchan.motion().yawServo().getCurrentAngle() / 10);
            cJSON_AddNumberToObject(root, "pitch", stackchan.motion().pitchServo().getCurrentAngle() / 10);

            auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar());
            cJSON_AddStringToObject(root, "face", custom ? custom->faceName().c_str() : "default");
        }
    }

    return send_json(req, root);
}

esp_err_t handle_head(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }

    cJSON* body = read_body(req);
    if (body == nullptr) {
        return send_error(req, "400 Bad Request", "expected a JSON body");
    }

    // Same ranges hal_mcp.cpp advertises to the assistant.
    const int yaw   = json_int(body, "yaw", -9999);
    const int pitch = json_int(body, "pitch", -9999);
    const int speed = uitk::clamp(json_int(body, "speed", 150), 100, 1000);
    cJSON_Delete(body);

    {
        LvglLockGuard lock;
        auto& motion = GetStackChan().motion();
        if (pitch != -9999) {
            motion.pitchServo().moveWithSpeed(uitk::clamp(pitch, 0, 90) * 10, speed);
        }
        if (yaw != -9999) {
            motion.yawServo().moveWithSpeed(uitk::clamp(yaw, -128, 128) * 10, speed);
        }
    }

    mclog::tagInfo(_tag, "head: yaw={} pitch={} speed={}", yaw, pitch, speed);

    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

// One rail is LEDs 0-5, the other 6-11. Values stop at 168, the same limit /api/led has.
esp_err_t handle_leds(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    cJSON* body = read_body(req);
    if (body == nullptr) {
        return send_error(req, "400 Bad Request", "expected a JSON body");
    }
    constexpr int kLeds = 12;
    cJSON* frame = cJSON_GetObjectItem(body, "frame");
    if (cJSON_IsArray(frame)) {
        const int count = std::min(cJSON_GetArraySize(frame), kLeds);
        for (int i = 0; i < count; i++) {
            cJSON* c = cJSON_GetArrayItem(frame, i);
            if (cJSON_IsArray(c) && cJSON_GetArraySize(c) == 3) {
                GetHAL().setRgbColor(i, uitk::clamp(cJSON_GetArrayItem(c, 0)->valueint, 0, 168),
                                     uitk::clamp(cJSON_GetArrayItem(c, 1)->valueint, 0, 168),
                                     uitk::clamp(cJSON_GetArrayItem(c, 2)->valueint, 0, 168));
            }
        }
    } else {
        const int index = json_int(body, "index", -1);
        if (index < 0 || index >= kLeds) {
            cJSON_Delete(body);
            return send_error(req, "400 Bad Request", "expected a frame, or an index from 0 to 11");
        }
        GetHAL().setRgbColor(index, uitk::clamp(json_int(body, "r", 0), 0, 168), uitk::clamp(json_int(body, "g", 0), 0, 168),
                             uitk::clamp(json_int(body, "b", 0), 0, 168));
    }
    cJSON_Delete(body);
    GetHAL().refreshRgb();
    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

esp_err_t handle_led(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }

    cJSON* body = read_body(req);
    if (body == nullptr) {
        return send_error(req, "400 Bad Request", "expected a JSON body");
    }

    const int r = uitk::clamp(json_int(body, "r", 0), 0, 168);
    const int g = uitk::clamp(json_int(body, "g", 0), 0, 168);
    const int b = uitk::clamp(json_int(body, "b", 0), 0, 168);
    cJSON_Delete(body);

    {
        LvglLockGuard lock;
        GetStackChan().leftNeonLight().setColor(r, g, b);
        GetStackChan().rightNeonLight().setColor(r, g, b);
    }

    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

esp_err_t handle_emotion(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }

    cJSON* body = read_body(req);
    if (body == nullptr) {
        return send_error(req, "400 Bad Request", "expected a JSON body");
    }

    cJSON* name = cJSON_GetObjectItem(body, "name");
    if (!cJSON_IsString(name)) {
        cJSON_Delete(body);
        return send_error(req, "400 Bad Request", "expected {\"name\": \"...\"}");
    }

    const std::string emotion = name->valuestring;
    cJSON* lights             = cJSON_GetObjectItem(body, "lights");
    const bool hasLights      = cJSON_IsArray(lights) && cJSON_GetArraySize(lights) == 3;
    int rgb[3]                = {0, 0, 0};
    for (int i = 0; hasLights && i < 3; i++) {
        rgb[i] = uitk::clamp(cJSON_GetArrayItem(lights, i)->valueint, 0, 168);
    }
    const int asked = json_int(body, "lightsPulse", 0);
    const int pulse = asked > 0 ? uitk::clamp(asked, 300, 10000) : 0;

    auto pairOf = [](cJSON* node, const char* side, int lo, int hi, int& a, int& b) {
        cJSON* pair = cJSON_GetObjectItem(node, side);
        if (cJSON_IsArray(pair) && cJSON_GetArraySize(pair) == 2) {
            a = uitk::clamp(cJSON_GetArrayItem(pair, 0)->valueint, lo, hi);
            b = uitk::clamp(cJSON_GetArrayItem(pair, 1)->valueint, lo, hi);
        }
    };
    cJSON* look         = cJSON_GetObjectItem(body, "look");
    cJSON* brow         = cJSON_GetObjectItem(body, "brow");
    const bool hasRig   = cJSON_IsObject(look) || cJSON_IsObject(brow);
    int lookL[2] = {0, 0}, lookR[2] = {0, 0}, browL[2] = {0, 0}, browR[2] = {0, 0};
    if (cJSON_IsObject(look)) {
        pairOf(look, "left", -100, 100, lookL[0], lookL[1]);
        pairOf(look, "right", -100, 100, lookR[0], lookR[1]);
    }
    if (cJSON_IsObject(brow)) {
        pairOf(brow, "left", -3600, 3600, browL[0], browL[1]);
        pairOf(brow, "right", -3600, 3600, browR[0], browR[1]);
    }
    const bool rest      = cJSON_IsTrue(cJSON_GetObjectItem(body, "rest"));
    const int askedCycle = json_int(body, "mouthCycle", 0);
    const int mouthCycle = askedCycle > 0 ? uitk::clamp(askedCycle, 300, 20000) : 0;
    const int askedRoll  = json_int(body, "roll", 0);
    const int roll       = askedRoll > 0 ? uitk::clamp(askedRoll, 300, 5000) : 0;
    cJSON_Delete(body);

    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar())) {
                custom->setResting(rest ? emotion : "");
            }
        }
    }

    // Routed through the display so the sleepy side effects (servo park, idle
    // teardown) still happen, rather than poking the avatar directly.
    auto* display = Board::GetInstance().GetDisplay();
    if (display != nullptr) {
        display->SetEmotion(emotion.c_str());
    }
    if (hasLights || hasRig) {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar())) {
                if (hasLights) {
                    custom->showLights(rgb[0], rgb[1], rgb[2], pulse);
                }
                if (hasRig) {
                    custom->tryRig({lookL[0], lookL[1]}, {lookR[0], lookR[1]}, {browL[0], browL[1]}, {browR[0], browR[1]}, mouthCycle, roll);
                }
            }
        }
    }

    mclog::tagInfo(_tag, "emotion: {}", emotion);

    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    cJSON_AddStringToObject(root, "emotion", emotion.c_str());
    return send_json(req, root);
}

esp_err_t handle_twitch(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }

    cJSON* body = read_body(req);
    if (body == nullptr) {
        return send_error(req, "400 Bad Request", "expected a JSON body");
    }

    avatar::PoseTwitch_t twitch;
    twitch.on = !cJSON_IsTrue(cJSON_GetObjectItem(body, "off"));
    cJSON* eye = cJSON_GetObjectItem(body, "eye");
    cJSON* lid = cJSON_GetObjectItem(body, "lid");
    if (cJSON_IsString(eye)) {
        twitch.eye = eye->valuestring;
    }
    if (cJSON_IsString(lid)) {
        twitch.lid = lid->valuestring;
    }
    cJSON* every = cJSON_GetObjectItem(body, "every");
    if (cJSON_IsArray(every) && cJSON_GetArraySize(every) == 2) {
        twitch.everyMinMs = uitk::clamp(cJSON_GetArrayItem(every, 0)->valueint, 100, 120000);
        twitch.everyMaxMs = uitk::clamp(cJSON_GetArrayItem(every, 1)->valueint, twitch.everyMinMs, 120000);
    }
    twitch.flutters = uitk::clamp(json_int(body, "flutters", twitch.flutters), 1, 10);
    cJSON_Delete(body);

    uint32_t count = 0;
    bool done      = false;
    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar())) {
                custom->setTwitch(twitch);
                count = custom->twitchCount();
                done  = true;
            }
        }
    }
    if (!done) {
        return send_error(req, "409 Conflict", "the current face has no lids to twitch");
    }

    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    cJSON_AddBoolToObject(root, "on", twitch.on);
    cJSON_AddNumberToObject(root, "twitches", count);
    return send_json(req, root);
}

esp_err_t handle_react(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    cJSON* body = read_body(req);
    if (body == nullptr) {
        return send_error(req, "400 Bad Request", "expected a JSON body");
    }
    cJSON* name = cJSON_GetObjectItem(body, "name");
    if (!cJSON_IsString(name)) {
        cJSON_Delete(body);
        return send_error(req, "400 Bad Request", "expected {\"name\": \"...\"}");
    }
    const std::string pose = name->valuestring;
    const int ms           = uitk::clamp(json_int(body, "ms", 2000), 200, 15000);
    cJSON_Delete(body);
    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar())) {
                custom->react(pose, ms);
            }
        }
    }
    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

esp_err_t handle_touch_spots(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    cJSON* root  = cJSON_CreateObject();
    cJSON* names = cJSON_AddArrayToObject(root, "spots");
    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar())) {
                for (const auto& spot : custom->touchSpots()) {
                    cJSON_AddItemToArray(names, cJSON_CreateString(spot.name.c_str()));
                }
                if (custom->headDeclared()) {
                    cJSON* head = cJSON_AddObjectToObject(root, "head");
                    for (const auto& kv : custom->headTouches()) {
                        cJSON* one = cJSON_AddObjectToObject(head, kv.first.c_str());
                        cJSON_AddStringToObject(one, "pose", kv.second.pose.c_str());
                        cJSON_AddNumberToObject(one, "ms", kv.second.ms);
                    }
                }
            }
        }
    }
    return send_json(req, root);
}

esp_err_t handle_touch_raw(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    uint32_t since = 0;
    char query[32] = {0}, value[16] = {0};
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
        httpd_query_key_value(query, "since", value, sizeof(value)) == ESP_OK) {
        since = std::strtoul(value, nullptr, 10);
    }
    constexpr size_t kCapacity = 48 * 1024;
    char* text                 = static_cast<char*>(heap_caps_malloc(kCapacity, MALLOC_CAP_SPIRAM));
    if (text == nullptr) {
        return send_error(req, "500 Internal Server Error", "out of memory");
    }
    const size_t used = touch_log::read(since, text, kCapacity);
    httpd_resp_set_type(req, "text/plain");
    const esp_err_t err = httpd_resp_send(req, text, used);
    heap_caps_free(text);
    return err;
}

struct PhotoJob_t {
    SemaphoreHandle_t done = nullptr;
    uint8_t* jpeg          = nullptr;
    size_t size            = 0;
    int width              = 0;
    int height             = 0;
    size_t heap_while_on   = 0;
    const char* error      = nullptr;
};

// The camera art, up for as long as the sensor is on. The red light lives with the sensor itself.
int show_camera_art()
{
    LvglLockGuard lock;
    auto& stackchan = GetStackChan();
    return stackchan.hasAvatar() ? stackchan.avatar().addNamedDecorator("camera", 0) : -1;
}

void hide_camera_art(int id)
{
    if (id < 0) {
        return;
    }
    LvglLockGuard lock;
    auto& stackchan = GetStackChan();
    if (stackchan.hasAvatar()) {
        stackchan.avatar().removeDecorator(id);
    }
}

// Runs on a PSRAM stack: the JPEG encoder wants more stack than internal RAM should give.
void photo_task(void* arg)
{
    auto* job = static_cast<PhotoJob_t*>(arg);
    const int art = show_camera_art();

    uint8_t* frame = nullptr;
    size_t length  = 0;
    int format     = 0;
    auto* camera   = hal_bridge::camera_open();
    if (camera == nullptr) {
        job->error = "the camera didn't start";
    } else {
        job->heap_while_on = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
        auto lock          = camera->TryLockFrame();
        bool captured      = false;
        // a sensor just woken needs a moment to find its exposure
        constexpr int kSettleFrames = 12;
        for (int i = 0; i < kSettleFrames; i++) {
            captured = camera->StreamCaptures();
        }
        if (!captured || camera->GetFrameData() == nullptr) {
            job->error = "the capture failed";
        } else {
            length      = camera->GetFrameSize();
            format      = camera->GetFrameFormat();
            job->width  = camera->GetFrameWidth();
            job->height = camera->GetFrameHeight();
            frame       = static_cast<uint8_t*>(heap_caps_malloc(length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
            if (frame == nullptr) {
                job->error = "out of memory";
            } else {
                std::memcpy(frame, camera->GetFrameData(), length);
            }
        }
        lock.unlock();
    }
    hal_bridge::camera_close();  // off before encoding: the picture's taken, and the RAM comes back
    hide_camera_art(art);

    if (frame != nullptr) {
        if (!image_to_jpeg(frame, length, job->width, job->height, (v4l2_pix_fmt_t)format, 60, &job->jpeg, &job->size) ||
            job->jpeg == nullptr) {
            job->error = "the jpeg failed";
        }
        heap_caps_free(frame);
    }

    xSemaphoreGive(job->done);
    vTaskSuspend(nullptr);  // the handler deletes this task, which frees its stack
}

esp_err_t handle_photo(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    // the mic channel holds the RAM the camera needs
    if (!hal_bridge::is_xiaozhi_idle()) {
        return send_error(req, "409 Conflict", "busy talking: the mic channel has to close first");
    }
    constexpr size_t kNeedFree = 42 * 1024;
    if (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) < kNeedFree) {
        return send_error(req, "503 Service Unavailable", "not enough memory for the camera right now");
    }

    char query[32] = {0};
    bool probe     = false;
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK) {
        probe = std::strstr(query, "probe=1") != nullptr;
    }

    PhotoJob_t job;
    job.done = xSemaphoreCreateBinary();
    if (job.done == nullptr) {
        return send_error(req, "500 Internal Server Error", "out of memory");
    }
    TaskHandle_t task = nullptr;
    if (xTaskCreatePinnedToCoreWithCaps(photo_task, "photo", 16 * 1024, &job, 3, &task, 1, MALLOC_CAP_SPIRAM) != pdPASS) {
        vSemaphoreDelete(job.done);
        return send_error(req, "500 Internal Server Error", "could not start the photo task");
    }
    if (xSemaphoreTake(job.done, pdMS_TO_TICKS(30000)) != pdTRUE) {
        // the task still holds job, which lives on this stack: this is not recoverable in place
        ESP_LOGE("HAL-Control", "photo task hung, rebooting");
        esp_restart();
    }
    vTaskDeleteWithCaps(task);
    vSemaphoreDelete(job.done);

    if (job.error != nullptr) {
        free(job.jpeg);
        return send_error(req, "500 Internal Server Error", job.error);
    }

    if (probe) {
        cJSON* root = cJSON_CreateObject();
        cJSON_AddBoolToObject(root, "ok", true);
        cJSON_AddNumberToObject(root, "width", job.width);
        cJSON_AddNumberToObject(root, "height", job.height);
        cJSON_AddNumberToObject(root, "jpegBytes", job.size);
        cJSON_AddNumberToObject(root, "heapWhileOn", job.heap_while_on);
        cJSON_AddNumberToObject(root, "heapAfter", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        free(job.jpeg);
        return send_json(req, root);
    }

    httpd_resp_set_type(req, "image/jpeg");
    esp_err_t err = httpd_resp_send(req, reinterpret_cast<const char*>(job.jpeg), job.size);
    free(job.jpeg);
    return err;
}

esp_err_t handle_face(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }

    char crc_text[12] = {0};
    if (req->content_len <= 0 || httpd_req_get_hdr_value_str(req, "X-Face-Crc32", crc_text, sizeof(crc_text)) != ESP_OK) {
        return send_error(req, "400 Bad Request", "expected a bundle and an X-Face-Crc32 header");
    }
    const uint32_t crc = std::strtoul(crc_text, nullptr, 16);

    if (!face_push::begin(req->content_len)) {
        return send_error(req, "400 Bad Request", "no room for a bundle that size");
    }

    constexpr size_t kBuffer = 4096;
    char* buffer             = static_cast<char*>(heap_caps_malloc(kBuffer, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (buffer == nullptr) {
        return send_error(req, "500 Internal Server Error", "out of memory");
    }

    size_t remaining = req->content_len;
    int timeouts     = 0;
    bool ok          = true;
    while (ok && remaining > 0) {
        const int got = httpd_req_recv(req, buffer, std::min(remaining, kBuffer));
        if (got == HTTPD_SOCK_ERR_TIMEOUT) {
            ok = ++timeouts <= 5;
            continue;
        }
        ok = got > 0 && face_push::write(buffer, got);
        if (ok) {
            timeouts = 0;
            remaining -= got;
        }
    }
    heap_caps_free(buffer);

    if (!ok || !face_push::commit(crc)) {
        return send_error(req, "400 Bad Request", "the upload was cut short or failed its checksum");
    }
    mclog::tagInfo(_tag, "face bundle waiting: {} bytes", req->content_len);

    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    cJSON_AddNumberToObject(root, "bytes", req->content_len);
    return send_json(req, root);
}

esp_err_t handle_face_restore(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    if (!face_push::request_restore()) {
        return send_error(req, "500 Internal Server Error", "could not mark it");
    }
    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

esp_err_t handle_wake(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    if (!hal_bridge::is_xiaozhi_ready() || !hal_bridge::is_xiaozhi_idle()) {
        return send_error(req, "409 Conflict", "the channel isn't idle");
    }
    hal_bridge::toggle_xiaozhi_chat_state();
    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

esp_err_t handle_reboot(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }
    // a moment later, so the answer gets out first
    static esp_timer_handle_t timer = nullptr;
    if (timer == nullptr) {
        esp_timer_create_args_t args = {};
        args.callback                = [](void*) { esp_restart(); };
        args.name                    = "reboot";
        if (esp_timer_create(&args, &timer) != ESP_OK) {
            return send_error(req, "500 Internal Server Error", "could not schedule it");
        }
    }
    esp_timer_start_once(timer, 500 * 1000);
    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

esp_err_t handle_say(httpd_req_t* req)
{
    if (!token_ok(req)) {
        return send_error(req, "401 Unauthorized", "bad or missing token");
    }

    cJSON* body = read_body(req);
    if (body == nullptr) {
        return send_error(req, "400 Bad Request", "expected a JSON body");
    }

    cJSON* text = cJSON_GetObjectItem(body, "text");
    if (!cJSON_IsString(text)) {
        cJSON_Delete(body);
        return send_error(req, "400 Bad Request", "expected {\"text\": \"...\"}");
    }

    const std::string message = text->valuestring;
    cJSON_Delete(body);

    {
        LvglLockGuard lock;
        auto& stackchan = GetStackChan();
        if (stackchan.hasAvatar()) {
            stackchan.avatar().setSpeech(message);
        }
    }

    cJSON* root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    return send_json(req, root);
}

}  // namespace

namespace {

void start_httpd();

/** @brief WiFi can come up via mooncake's setup app or via xiaozhi's
 *  WifiBoard. Binding to the IP event covers both. */
void on_got_ip(void* arg, esp_event_base_t base, int32_t id, void* data)
{
    (void)arg;
    (void)base;
    (void)id;
    auto* event = static_cast<ip_event_got_ip_t*>(data);
    mclog::tagInfo(_tag, "got ip " IPSTR ", starting control server", IP2STR(&event->ip_info.ip));
    start_httpd();
}

void start_httpd()
{
    if (_server != nullptr) {
        return;
    }

    httpd_config_t config   = HTTPD_DEFAULT_CONFIG();
    config.server_port      = CONFIG_STACKCHAN_CONTROL_PORT;
    config.ctrl_port        = CONFIG_STACKCHAN_CONTROL_PORT + 1;
    config.max_open_sockets = 4;
    config.lru_purge_enable = true;
    config.max_uri_handlers = 20;  // the default of 8 is full, and past it a route fails to register
#ifdef CONFIG_STACKCHAN_CAMERA
    config.stack_size = 8192;  // the JPEG encoder runs on this task
#else
    config.stack_size = 5120;
#endif

    if (httpd_start(&_server, &config) != ESP_OK) {
        mclog::tagError(_tag, "failed to start control server");
        _server = nullptr;
        return;
    }

    static const auto handle_ram = [](httpd_req_t* req) -> esp_err_t {
        if (!token_ok(req)) {
            return send_error(req, "401 Unauthorized", "bad or missing token");
        }
        size_t length = 0;
        char* text    = ram_map::report(length);
        return send_text_and_free(req, text, length);
    };
    static const auto handle_log = [](httpd_req_t* req) -> esp_err_t {
        if (!token_ok(req)) {
            return send_error(req, "401 Unauthorized", "bad or missing token");
        }
        size_t length = 0;
        char* text    = ram_map::recent_log(length);
        return send_text_and_free(req, text, length);
    };

    static const auto handle_touch = [](httpd_req_t* req) -> esp_err_t {
        if (!token_ok(req)) {
            return send_error(req, "401 Unauthorized", "bad or missing token");
        }
        char text[96];
        const uint32_t last = _touch_last_ms.load();
        const uint32_t ago  = last ? GetHAL().millis() - last : 0;
        int n = std::snprintf(text, sizeof(text), "{\"ok\":true,\"pats\":%u,\"presses\":%u,\"ms_ago\":%u}",
                              (unsigned)_touch_pats.load(), (unsigned)_touch_presses.load(), (unsigned)ago);
        httpd_resp_set_type(req, "application/json");
        return httpd_resp_send(req, text, n);
    };

    static const httpd_uri_t routes[] = {
        {"/api/touch", HTTP_GET, handle_touch, nullptr},
        {"/api/photo", HTTP_GET, handle_photo, nullptr},
        {"/api/touch-raw", HTTP_GET, handle_touch_raw, nullptr},
        {"/api/react", HTTP_POST, handle_react, nullptr},
        {"/api/touch-spots", HTTP_GET, handle_touch_spots, nullptr},
        {"/api/status", HTTP_GET, handle_status, nullptr},
        {"/api/ram", HTTP_GET, handle_ram, nullptr},
        {"/api/log", HTTP_GET, handle_log, nullptr},
        {"/api/head", HTTP_POST, handle_head, nullptr},
        {"/api/led", HTTP_POST, handle_led, nullptr},
        {"/api/leds", HTTP_POST, handle_leds, nullptr},
        {"/api/emotion", HTTP_POST, handle_emotion, nullptr},
        {"/api/say", HTTP_POST, handle_say, nullptr},
        {"/api/twitch", HTTP_POST, handle_twitch, nullptr},
        {"/api/face", HTTP_POST, handle_face, nullptr},
        {"/api/face-restore", HTTP_POST, handle_face_restore, nullptr},
        {"/api/reboot", HTTP_POST, handle_reboot, nullptr},
        {"/api/wake", HTTP_POST, handle_wake, nullptr},
        {"/api/settings", HTTP_POST, handle_settings, nullptr},
    };
    for (const auto& route : routes) {
        httpd_register_uri_handler(_server, &route);
    }

    const char* token = CONFIG_STACKCHAN_CONTROL_TOKEN;
    mclog::tagInfo(_tag, "control server on port {} ({})", CONFIG_STACKCHAN_CONTROL_PORT,
                   (token && std::strlen(token)) ? "token required" : "NO AUTH - LAN open");
}

}  // namespace

void Hal::startControlServer()
{
    static bool registered = false;
    if (registered) {
        return;
    }
    registered = true;

    // the saved light level, from the first frame
    setRgbDimmer(hal_bridge::soma_setting("dimmer", 100));

    onHeadPetGesture.connect([this](HeadPetGesture gesture) {
        if (gesture == HeadPetGesture::SwipeForward || gesture == HeadPetGesture::SwipeBackward) {
            _touch_pats++;
            _touch_last_ms = millis();
        } else if (gesture == HeadPetGesture::Press) {
            _touch_presses++;
            _touch_last_ms = millis();
        }
    });

    // The default loop may not exist this early. Creating it is idempotent --
    // ESP_ERR_INVALID_STATE just means somebody got there first.
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        mclog::tagError(_tag, "esp_event_loop_create_default failed: {}", esp_err_to_name(err));
        return;
    }

    err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &on_got_ip, nullptr, nullptr);
    if (err != ESP_OK) {
        mclog::tagError(_tag, "could not bind IP_EVENT_STA_GOT_IP: {}", esp_err_to_name(err));
        return;
    }
    mclog::tagInfo(_tag, "waiting for an IP to start the control server");

    // Already online (a warm reboot, or this ran late) -- do not wait.
    esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t info = {};
    if (netif != nullptr && esp_netif_get_ip_info(netif, &info) == ESP_OK && info.ip.addr != 0) {
        start_httpd();
    }
}

void Hal::stopControlServer()
{
    if (_server != nullptr) {
        httpd_stop(_server);
        _server = nullptr;
        mclog::tagInfo(_tag, "control server stopped");
    }
}

#else /* CONFIG_STACKCHAN_CONTROL_SERVER */

void Hal::startControlServer()
{
}

void Hal::stopControlServer()
{
}

#endif
