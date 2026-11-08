/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "hal.h"
#include "soma_config.h"
#include <mooncake_log.h>
#include <sdkconfig.h>

static const std::string_view _tag = "HAL-Tailnet";

#ifdef CONFIG_STACKCHAN_TAILNET

#include <microlink.h>
#include <esp_heap_caps.h>
#include <esp_err.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <cstring>
#include <ctime>

// Kconfig bools are "defined or absent", not 1/0.
#ifdef CONFIG_STACKCHAN_TAILNET_ENABLE_DERP
#define STACKCHAN_TAILNET_DERP true
#else
#define STACKCHAN_TAILNET_DERP false
#endif

namespace {

microlink_t* _ml = nullptr;
TailnetStatus_t _status;

const char* state_name(microlink_state_t state)
{
    switch (state) {
        case ML_STATE_IDLE:
            return "idle";
        case ML_STATE_WIFI_WAIT:
            return "waiting for wifi";
        case ML_STATE_CONNECTING:
            return "connecting";
        case ML_STATE_REGISTERING:
            return "registering";
        case ML_STATE_CONNECTED:
            return "connected";
        case ML_STATE_RECONNECTING:
            return "reconnecting";
        case ML_STATE_ERROR:
            return "error";
        default:
            return "unknown";
    }
}

size_t free_internal()
{
    return heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
}

// microlink's four task stacks alone take 42 KB of internal RAM, the largest being
// 14 KB in one piece, and TLS and AES still need room after that. Starting it short
// of this takes the voice down with it (seen on this board: AES failing at 4 KB
// free), so the tailnet is the one that gives way.
constexpr size_t kMinInternalFree  = 80 * 1024;
constexpr size_t kMinLargestBlock  = 16 * 1024;
constexpr time_t kClockIsSet       = 1700000000;  // any time after Nov 2023

bool enough_heap()
{
    const size_t free_now = free_internal();
    const size_t largest  = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    if (free_now >= kMinInternalFree && largest >= kMinLargestBlock) {
        return true;
    }
    mclog::tagWarn(_tag, "not starting tailnet: internal heap free {} B (need {}), largest block {} B (need {})",
                   free_now, kMinInternalFree, largest, kMinLargestBlock);
    return false;
}

void tailnet_start_task(void*)
{
    // The handshake is timestamp-sensitive, so give SNTP up to a minute.
    for (int i = 0; i < 120 && time(nullptr) < kClockIsSet; i++) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    GetHAL().startTailnet();
    vTaskDelete(nullptr);
}

void on_state_changed(microlink_t* ml, microlink_state_t state, void* user_data)
{
    _status.state = static_cast<int>(state);

    if (state == ML_STATE_CONNECTED) {
        char ip[20] = {0};
        microlink_ip_to_str(microlink_get_vpn_ip(ml), ip);
        _status.connected = true;
        _status.address   = ip;
        mclog::tagInfo(_tag, "tailnet up: {} ({} peers), internal heap free {} B", ip,
                       microlink_get_peer_count(ml), free_internal());
    } else {
        _status.connected = false;
        mclog::tagInfo(_tag, "tailnet state: {}", state_name(state));
    }
}

}  // namespace

void Hal::startTailnet(std::function<void(std::string_view)> onLog)
{
    if (_ml != nullptr) {
        mclog::tagInfo(_tag, "tailnet already started");
        return;
    }

    // microlink keeps the pointers, so these outlive the call. soma.yaml's, else the build's.
    static std::string auth_key;
    static std::string device_name;
    auth_key    = soma_config::tailnet_key();
    device_name = soma_config::tailnet_name();
    if (auth_key.empty()) {
        // Not a failure: the feature is compiled in but unconfigured.
        mclog::tagWarn(_tag, "no auth key set, skipping tailnet "
                             "(put tailnet: key: in soma.yaml on the SD card)");
        if (onLog) {
            onLog("Tailnet: no auth key, skipped");
        }
        return;
    }

    if (!enough_heap()) {
        if (onLog) {
            onLog("Tailnet: not enough RAM, skipped");
        }
        return;
    }

    // Record the budget before and after so the cost on this board is visible
    // in the log rather than guessed at.
    const size_t heap_before = free_internal();
    mclog::tagInfo(_tag, "starting tailnet, internal heap free {} B", heap_before);
    if (onLog) {
        onLog("Joining tailnet...");
    }

    microlink_config_t config = {};
    config.auth_key           = auth_key.c_str();
    config.device_name        = device_name.c_str();
    config.enable_derp        = STACKCHAN_TAILNET_DERP;
    config.enable_stun        = true;
    config.enable_disco       = true;
    config.max_peers          = CONFIG_STACKCHAN_TAILNET_MAX_PEERS;
    // The radio shares a die with a busy audio pipeline; keep transmit power
    // modest, matching microlink's own examples.
    config.wifi_tx_power_dbm = 13;

    _ml = microlink_init(&config);
    if (_ml == nullptr) {
        mclog::tagError(_tag, "microlink_init failed (internal heap free {} B)", free_internal());
        if (onLog) {
            onLog("Tailnet: init failed");
        }
        return;
    }

    microlink_set_state_callback(_ml, on_state_changed, nullptr);

    esp_err_t err = microlink_start(_ml);
    if (err != ESP_OK) {
        mclog::tagError(_tag, "microlink_start failed: {}", esp_err_to_name(err));
        microlink_destroy(_ml);
        _ml = nullptr;
        if (onLog) {
            onLog("Tailnet: start failed");
        }
        return;
    }

    const size_t heap_after = free_internal();
    mclog::tagInfo(_tag, "tailnet started, internal heap free {} B (cost {} B)", heap_after,
                   static_cast<int>(heap_before) - static_cast<int>(heap_after));

#ifdef CONFIG_STACKCHAN_TAILNET_BLOCK_BOOT
    // Opt-in only. The assistant must still work with no tailnet, so the
    // default path returns immediately and lets the state callback report.
    constexpr int timeout_ms = 30000;
    int waited               = 0;
    while (!microlink_is_connected(_ml) && waited < timeout_ms) {
        vTaskDelay(pdMS_TO_TICKS(250));
        waited += 250;
    }
    if (!microlink_is_connected(_ml)) {
        mclog::tagWarn(_tag, "tailnet did not come up within {} ms, continuing anyway", timeout_ms);
        if (onLog) {
            onLog("Tailnet: timed out, continuing");
        }
    }
#endif
}

void Hal::startTailnetWhenReady()
{
    if (_ml != nullptr || !enough_heap()) {
        return;
    }
    // NVS and key generation: an internal stack, and a generous one, but only briefly.
    if (xTaskCreate(tailnet_start_task, "tailnet_start", 8192, nullptr, 2, nullptr) != pdPASS) {
        mclog::tagError(_tag, "could not create the tailnet start task");
    }
}

void Hal::stopTailnet()
{
    if (_ml == nullptr) {
        return;
    }

    mclog::tagInfo(_tag, "stopping tailnet");
    microlink_stop(_ml);
    microlink_destroy(_ml);
    _ml = nullptr;

    _status = TailnetStatus_t{};
}

TailnetStatus_t Hal::getTailnetStatus()
{
    if (_ml != nullptr) {
        _status.peerCount = microlink_get_peer_count(_ml);
    }
    return _status;
}

#else  /* CONFIG_STACKCHAN_TAILNET */

// Compiled out entirely. These no-ops keep callers free of #ifdefs.

void Hal::startTailnet(std::function<void(std::string_view)> onLog)
{
    (void)onLog;
}

void Hal::startTailnetWhenReady()
{
}

void Hal::stopTailnet()
{
}

TailnetStatus_t Hal::getTailnetStatus()
{
    return TailnetStatus_t{};
}

#endif /* CONFIG_STACKCHAN_TAILNET */
