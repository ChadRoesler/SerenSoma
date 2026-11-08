/*
 * SPDX-License-Identifier: MIT
 */
/*
 * soma.yaml: see soma_config.h for the file and why it is shaped like this.
 *
 * Logged through ESP_LOG, not mclog: only ESP_LOG reaches /api/log. Key names
 * and the voice server's address are logged; passwords, tokens and keys never
 * are, not even their first few characters.
 */
#include "soma_config.h"
#include <settings.h>
#include <ssid_manager.h>
#include <esp_log.h>
#include <sdkconfig.h>
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>

namespace {

constexpr const char* kTag = "SOMA-CFG";
constexpr const char* kNs  = "soma";

// Windows hides known extensions, so a file typed as soma.yaml is easily soma.yaml.txt.
constexpr const char* kFiles[] = {
    "/sdcard/soma.yaml",
    "/sdcard/soma.yml",
    "/sdcard/soma.yaml.txt",
    "/sdcard/soma.yml.txt",
};

constexpr size_t kMaxLine = 192;

// What each value can hold. The token's cap matches the header buffer in
// hal_control_server.cpp's token_ok.
constexpr size_t kMaxSsid     = 32;
constexpr size_t kMaxPassword = 64;
constexpr size_t kMaxToken    = 95;
constexpr size_t kMaxUrl      = 128;
constexpr size_t kMaxTsKey    = 128;
constexpr size_t kMaxTsName   = 63;

#ifdef CONFIG_STACKCHAN_CONTROL_TOKEN
constexpr const char* kBuildToken = CONFIG_STACKCHAN_CONTROL_TOKEN;
#else
constexpr const char* kBuildToken = "";
#endif

#ifdef CONFIG_STACKCHAN_TAILNET_AUTH_KEY
constexpr const char* kBuildTsKey = CONFIG_STACKCHAN_TAILNET_AUTH_KEY;
#else
constexpr const char* kBuildTsKey = "";
#endif

#ifdef CONFIG_STACKCHAN_TAILNET_DEVICE_NAME
constexpr const char* kBuildTsName = CONFIG_STACKCHAN_TAILNET_DEVICE_NAME;
#else
constexpr const char* kBuildTsName = "soma";
#endif

#ifdef CONFIG_STACKCHAN_TAILNET
constexpr bool kHasTailnet = true;
#else
constexpr bool kHasTailnet = false;
#endif

struct Value_t {
    bool present = false;
    std::string text;
};

struct Card_t {
    Value_t ssid, password, voice, token, ts_key, ts_name;
};

void wipe(std::string& s)
{
    std::fill(s.begin(), s.end(), '\0');
    s.clear();
}

std::string trim(const std::string& s)
{
    size_t a = 0;
    size_t b = s.size();
    while (a < b && std::isspace(static_cast<unsigned char>(s[a]))) {
        ++a;
    }
    while (b > a && std::isspace(static_cast<unsigned char>(s[b - 1]))) {
        --b;
    }
    return s.substr(a, b - a);
}

std::string lower(std::string s)
{
    for (auto& c : s) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return s;
}

/**
 * @brief A value as written after the colon: quoted ("..." or '...') is taken
 * whole, otherwise a " #" starts a comment. So a password with " #" in it
 * needs quotes, and a password with a quote in it is easier in wifi.txt.
 */
std::string value_of(const std::string& raw)
{
    std::string v = trim(raw);
    if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'')) {
        const size_t end = v.find(v.front(), 1);
        if (end != std::string::npos) {
            return v.substr(1, end - 1);
        }
    }
    if (!v.empty() && v.front() == '#') {
        return "";
    }
    const size_t hash = v.find(" #");
    if (hash != std::string::npos) {
        v = trim(v.substr(0, hash));
    }
    return v;
}

Value_t* slot(Card_t& card, const std::string& section, const std::string& key)
{
    if (section.empty()) {
        if (key == "voice") return &card.voice;
        if (key == "token") return &card.token;
    } else if (section == "wifi") {
        if (key == "ssid") return &card.ssid;
        if (key == "password") return &card.password;
    } else if (section == "tailnet") {
        if (key == "key") return &card.ts_key;
        if (key == "name") return &card.ts_name;
    }
    return nullptr;
}

bool is_section(const std::string& key)
{
    return key == "wifi" || key == "tailnet";
}

/** @brief Read the file into card. Returns false if it could not be opened. */
bool parse(FILE* f, Card_t& card)
{
    char line[kMaxLine];
    std::string section;
    int number = 0;
    while (fgets(line, sizeof(line), f) != nullptr) {
        ++number;
        const size_t len = std::strlen(line);
        if (len > 0 && line[len - 1] != '\n' && !feof(f)) {
            ESP_LOGW(kTag, "line %d is too long, skipping it", number);
            int c;
            while ((c = fgetc(f)) != EOF && c != '\n') {
            }
            continue;
        }
        std::string text(line);
        std::memset(line, 0, sizeof(line));
        // Windows Notepad may start the file with a byte order mark.
        if (number == 1 && text.rfind("\xEF\xBB\xBF", 0) == 0) {
            text.erase(0, 3);
        }
        const size_t indent = text.find_first_not_of(" \t");
        const std::string body = trim(text);
        wipe(text);
        if (body.empty() || body[0] == '#') {
            continue;
        }
        const size_t colon = body.find(':');
        if (colon == std::string::npos || body[0] == '-') {
            ESP_LOGW(kTag, "line %d is not \"key: value\", ignoring it", number);
            continue;
        }
        const std::string key = lower(trim(body.substr(0, colon)));
        std::string value     = value_of(body.substr(colon + 1));

        if (indent == 0) {
            if (is_section(key) && value.empty()) {
                section = key;
                continue;
            }
            section.clear();
        } else if (section.empty()) {
            ESP_LOGW(kTag, "line %d is indented but not under wifi: or tailnet:, ignoring it", number);
            wipe(value);
            continue;
        }

        Value_t* target = slot(card, section, key);
        if (target == nullptr) {
            ESP_LOGW(kTag, "line %d: unknown key %s%s%s, ignoring it", number, section.c_str(),
                     section.empty() ? "" : ".", key.c_str());
            wipe(value);
            continue;
        }
        target->present = true;
        wipe(target->text);
        target->text = std::move(value);
    }
    return true;
}

/** @brief host, host:port, http://host:port or a whole URL, to the OTA check-in URL. */
std::string ota_url(std::string v)
{
    if (v.find("://") == std::string::npos) {
        v = "http://" + v;
    }
    const size_t host_at = v.find("://") + 3;
    const size_t slash   = v.find('/', host_at);
    std::string host     = v.substr(host_at, slash == std::string::npos ? std::string::npos : slash - host_at);
    std::string path     = slash == std::string::npos ? "" : v.substr(slash);
    if (host.find(':') == std::string::npos) {
        host += ":7480";
    }
    if (path.empty() || path == "/") {
        path = "/ota/";
    }
    return v.substr(0, host_at) + host + path;
}

bool fits(const Value_t& v, size_t cap, const char* name)
{
    if (v.text.size() <= cap) {
        return true;
    }
    ESP_LOGW(kTag, "%s is longer than %u characters, ignoring it", name, static_cast<unsigned>(cap));
    return false;
}

/** @brief Save or forget one secret in NVS "soma". Logs that it changed, never what to. */
void save_secret(Value_t& v, const char* nvs_key, const char* name)
{
    Settings settings(kNs, true);
    if (v.text.empty()) {
        settings.EraseKey(nvs_key);
        ESP_LOGI(kTag, "%s forgotten, the build's own default is back", name);
    } else {
        settings.SetString(nvs_key, v.text);
        ESP_LOGI(kTag, "%s saved (%u characters)", name, static_cast<unsigned>(v.text.size()));
    }
}

void apply(Card_t& card)
{
    if (card.ssid.present) {
        if (card.ssid.text.empty()) {
            ESP_LOGW(kTag, "wifi.ssid is empty, keeping the saved networks");
        } else if (fits(card.ssid, kMaxSsid, "wifi.ssid") && fits(card.password, kMaxPassword, "wifi.password")) {
            // Replaces, not adds: see apply_wifi_file in hal_sdcard.cpp.
            auto& manager = SsidManager::GetInstance();
            manager.Clear();
            manager.AddSsid(card.ssid.text, card.password.text);
            ESP_LOGI(kTag, "Wi-Fi is now %s", card.ssid.text.c_str());
        }
    } else if (card.password.present) {
        ESP_LOGW(kTag, "wifi.password without wifi.ssid, ignoring it");
    }

    if (card.voice.present && fits(card.voice, kMaxUrl, "voice")) {
        // Xiaozhi's own override: Ota::GetCheckVersionUrl reads it before CONFIG_OTA_URL.
        Settings settings("wifi", true);
        if (card.voice.text.empty()) {
            settings.EraseKey("ota_url");
            ESP_LOGI(kTag, "voice server forgotten, the build's own is back");
        } else {
            const std::string url = ota_url(card.voice.text);
            settings.SetString("ota_url", url);
            ESP_LOGI(kTag, "voice server is now %s", url.c_str());
        }
    }

    if (card.token.present && fits(card.token, kMaxToken, "token")) {
        save_secret(card.token, "token", "control token");
    }

    if (card.ts_key.present && fits(card.ts_key, kMaxTsKey, "tailnet.key")) {
        save_secret(card.ts_key, "ts_key", "tailnet key");
    }
    if (card.ts_name.present && fits(card.ts_name, kMaxTsName, "tailnet.name")) {
        Settings settings(kNs, true);
        if (card.ts_name.text.empty()) {
            settings.EraseKey("ts_name");
        } else {
            settings.SetString("ts_name", card.ts_name.text);
        }
        ESP_LOGI(kTag, "tailnet name is now %s", card.ts_name.text.empty() ? kBuildTsName : card.ts_name.text.c_str());
    }
    if (!kHasTailnet && (card.ts_key.present || card.ts_name.present)) {
        ESP_LOGW(kTag, "tailnet settings saved, but this build has no tailnet; they wait for one that does");
    }
}

std::string saved(const char* nvs_key)
{
    Settings settings(kNs, false);
    return settings.GetString(nvs_key);
}

}  // namespace

void soma_config::apply_card_file()
{
    const char* path = nullptr;
    FILE* f          = nullptr;
    for (const char* candidate : kFiles) {
        f = fopen(candidate, "r");
        if (f != nullptr) {
            path = candidate;
            break;
        }
    }
    if (f == nullptr) {
        ESP_LOGI(kTag, "no soma.yaml on the card");
        return;
    }

    ESP_LOGI(kTag, "reading %s", path);
    Card_t card;
    parse(f, card);
    fclose(f);
    apply(card);

    for (Value_t* v : {&card.ssid, &card.password, &card.voice, &card.token, &card.ts_key, &card.ts_name}) {
        wipe(v->text);
    }

    // Deleted even if some lines were ignored: the log says which, the file is
    // still on the machine it was written on, and secrets left on a card are worse.
    if (std::remove(path) != 0) {
        ESP_LOGW(kTag, "could not delete %s: its secrets are still on the card", path);
    }
}

const std::string& soma_config::control_token()
{
    // Read once: the card is only read at boot, before the control server starts.
    static const std::string token = [] {
        std::string t = saved("token");
        return t.empty() ? std::string(kBuildToken) : t;
    }();
    return token;
}

const char* soma_config::control_token_source()
{
    if (!saved("token").empty()) {
        return "card";
    }
    return std::strlen(kBuildToken) > 0 ? "build" : "none";
}

const char* soma_config::voice_source()
{
    Settings settings("wifi", false);
    return settings.GetString("ota_url").empty() ? "build" : "card";
}

std::string soma_config::tailnet_key()
{
    std::string k = saved("ts_key");
    return k.empty() ? std::string(kBuildTsKey) : k;
}

std::string soma_config::tailnet_name()
{
    std::string n = saved("ts_name");
    return n.empty() ? std::string(kBuildTsName) : n;
}
