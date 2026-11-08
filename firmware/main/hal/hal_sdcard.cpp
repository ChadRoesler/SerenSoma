/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
/*
 * Boot-time microSD asset loading.
 *
 * On the CoreS3 the SD card and the LCD share SPI3, and GPIO 35 is wired to
 * BOTH the card's MISO and the panel's Data/Command line. There is no way to
 * read the card while the display is running without tearing down the panel's
 * SPI IO and remuxing that pin.
 *
 * So the card is a boot-time-only resource:
 *
 *   1. hold the LCD's chip select high so it ignores the bus
 *   2. claim SPI3 with MISO on GPIO 35
 *   3. mount, read every file under /sdcard/face into PSRAM, unmount
 *   4. free the bus, releasing GPIO 35
 *   5. the display then claims SPI3 for itself, with GPIO 35 as D/C
 *
 * Call loadFaceAssets() before xiaozhi_board_init(). After that point the
 * card is unreachable until the next reboot, which is fine: a face is read
 * once and lives in PSRAM for the life of the session.
 *
 * SRAM is not touched. Everything lands in PSRAM, which matters because the
 * running firmware idles at roughly 42 KB free internal heap.
 */
#include "sd_assets.h"
#include "face_push.h"
#include "soma_config.h"
#include <mooncake_log.h>
#include <driver/gpio.h>
#include <driver/spi_common.h>
#include <driver/sdspi_host.h>
#include <sdmmc_cmd.h>
#include <esp_vfs_fat.h>
#include <esp_heap_caps.h>
#include <dirent.h>
#include <cstdio>
#include <cctype>
#include <cstring>
#include <stackchan/utils/psram_alloc.h>
#include <ssid_manager.h>
#include <esp_log.h>
#include <cstdio>
#include <string>

static const std::string_view _tag = "HAL-SD";

namespace {

// CoreS3 wiring. Confirmed against docs.m5stack.com/en/core/CoreS3 and
// docs.m5stack.com/en/arduino/stackchan/sdcard, which agree.
constexpr gpio_num_t kPinSck  = GPIO_NUM_36;
constexpr gpio_num_t kPinMosi = GPIO_NUM_37;
constexpr gpio_num_t kPinMiso = GPIO_NUM_35;  // also the LCD D/C line, see above
constexpr gpio_num_t kPinSdCs = GPIO_NUM_4;
constexpr gpio_num_t kPinLcdCs = GPIO_NUM_3;

constexpr spi_host_device_t kSpiHost = SPI3_HOST;

constexpr const char* kMountPoint = "/sdcard";
constexpr const char* kFaceDir    = "/sdcard/face";
constexpr const char* kWifiFile   = "/sdcard/wifi.txt";
constexpr const char* kWifiFileDoubled = "/sdcard/wifi.txt.txt";
constexpr const char* kLogTag     = "HAL-SD";

// Refuse to swallow PSRAM if someone leaves a video on the card.
constexpr size_t kMaxTotalBytes = 2 * 1024 * 1024;
constexpr size_t kMaxFileBytes  = 1024 * 1024;

// Name inline rather than a std::string: the packer caps asset names at 32
// chars, and a per-entry string allocation would land in internal SRAM.
struct Asset_t {
    char name[33] = {0};
    void* data    = nullptr;
    size_t size   = 0;
};

// Keyed lowercase. FAT is case-insensitive, and FatFs returns uppercase names
// when long-filename support is off, so matching exactly is a trap.
std::string fold(std::string text)
{
    for (auto& c : text) {
        c = static_cast<char>(tolower(static_cast<unsigned char>(c)));
    }
    return text;
}

PsramVector<Asset_t> _assets;
bool _loaded = false;

void release_assets()
{
    for (auto& entry : _assets) {
        heap_caps_free(entry.data);
    }
    _assets.clear();
    _assets.shrink_to_fit();
}

Asset_t* find_asset(const std::string& name)
{
    const std::string key = fold(name);
    for (auto& entry : _assets) {
        if (key == entry.name) {
            return &entry;
        }
    }
    return nullptr;
}

/**
 * @brief Park the LCD's chip select high.
 *
 * At this point in boot the panel has not been configured, so GPIO 3 is
 * floating. Left floating it can latch SD traffic as display commands and
 * leave the controller in a confused state.
 */
void deselect_lcd()
{
    gpio_config_t cfg = {};
    cfg.pin_bit_mask  = 1ULL << kPinLcdCs;
    cfg.mode          = GPIO_MODE_OUTPUT;
    cfg.pull_up_en    = GPIO_PULLUP_ENABLE;
    cfg.pull_down_en  = GPIO_PULLDOWN_DISABLE;
    cfg.intr_type     = GPIO_INTR_DISABLE;
    gpio_config(&cfg);
    gpio_set_level(kPinLcdCs, 1);
}

void strip_line_end(char* line)
{
    line[std::strcspn(line, "\r\n")] = '\0';
}

/**
 * @brief Join a network named on the card: wifi.txt, SSID on the first line and
 * password on the second (blank for an open network).
 *
 * It replaces every saved network, and the file is deleted so a password never
 * outlives one boot on a card that moves between machines. This is what lets
 * Wi-Fi change with no phone app and no Bluetooth.
 *
 * Replaces, not adds: the Wi-Fi library scans only the channels of saved networks
 * when it knows any, so a new network on another channel is never seen, and among
 * those it finds it joins the strongest. A wrong password is recovered the same
 * way, with another wifi.txt.
 *
 * Logged through ESP_LOG, not mclog: only ESP_LOG reaches /api/log.
 */
void apply_wifi_file()
{
    // Windows hides known extensions, so "wifi.txt" typed in Explorer is easily wifi.txt.txt.
    const char* path = kWifiFile;
    FILE* f          = fopen(path, "r");
    if (f == nullptr) {
        path = kWifiFileDoubled;
        f    = fopen(path, "r");
    }
    if (f == nullptr) {
        ESP_LOGI(kLogTag, "no wifi.txt on the card");
        return;
    }
    char ssid[40]     = {0};
    char password[68] = {0};
    bool have_ssid    = fgets(ssid, sizeof(ssid), f) != nullptr;
    if (have_ssid && fgets(password, sizeof(password), f) == nullptr) {
        password[0] = '\0';
    }
    fclose(f);

    strip_line_end(ssid);
    strip_line_end(password);
    // Windows Notepad may start the file with a byte order mark.
    const char* name = std::strncmp(ssid, "\xEF\xBB\xBF", 3) == 0 ? ssid + 3 : ssid;

    if (*name == '\0') {
        ESP_LOGW(kLogTag, "wifi.txt has no SSID on its first line, ignoring it");
    } else {
        auto& manager = SsidManager::GetInstance();
        manager.Clear();
        manager.AddSsid(name, password);
        ESP_LOGI(kLogTag, "wifi.txt: Wi-Fi is now %s", name);
    }
    std::memset(password, 0, sizeof(password));

    if (std::remove(path) != 0) {
        ESP_LOGW(kLogTag, "could not delete wifi.txt: the password is still on the card");
    }
}

bool read_one_file(const std::string& path, const std::string& name, size_t& running_total)
{
    FILE* f = fopen(path.c_str(), "rb");
    if (f == nullptr) {
        mclog::tagWarn(_tag, "cannot open {}", path);
        return false;
    }

    fseek(f, 0, SEEK_END);
    const long length = ftell(f);
    fseek(f, 0, SEEK_SET);

    if (length <= 0) {
        fclose(f);
        return false;
    }
    if (static_cast<size_t>(length) > kMaxFileBytes) {
        mclog::tagWarn(_tag, "skipping {}: {} bytes exceeds the per-file cap", name, length);
        fclose(f);
        return false;
    }
    if (running_total + static_cast<size_t>(length) > kMaxTotalBytes) {
        mclog::tagWarn(_tag, "skipping {}: would exceed the {} KB budget", name, kMaxTotalBytes / 1024);
        fclose(f);
        return false;
    }

    void* buffer = heap_caps_malloc(static_cast<size_t>(length), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buffer == nullptr) {
        mclog::tagError(_tag, "out of PSRAM allocating {} bytes for {}", length, name);
        fclose(f);
        return false;
    }

    const size_t got = fread(buffer, 1, static_cast<size_t>(length), f);
    fclose(f);

    if (got != static_cast<size_t>(length)) {
        mclog::tagWarn(_tag, "short read on {}: {} of {} bytes", name, got, length);
        heap_caps_free(buffer);
        return false;
    }

    const std::string key = fold(name);
    if (key.size() > 32) {
        mclog::tagWarn(_tag, "asset name longer than 32 chars, skipping: {}", name);
        heap_caps_free(buffer);
        return false;
    }

    Asset_t entry;
    std::snprintf(entry.name, sizeof(entry.name), "%s", key.c_str());
    entry.data = buffer;
    entry.size = got;
    _assets.push_back(entry);
    running_total += got;
    return true;
}

}  // namespace

bool sd_assets::load()
{
    if (_loaded) {
        return !_assets.empty();
    }
    _loaded = true;

    deselect_lcd();

    spi_bus_config_t buscfg = {};
    buscfg.mosi_io_num      = kPinMosi;
    buscfg.miso_io_num      = kPinMiso;
    buscfg.sclk_io_num      = kPinSck;
    buscfg.quadwp_io_num    = -1;
    buscfg.quadhd_io_num    = -1;
    buscfg.max_transfer_sz  = 4096;

    esp_err_t err = spi_bus_initialize(kSpiHost, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        mclog::tagWarn(_tag, "spi_bus_initialize failed: {}", esp_err_to_name(err));
        return false;
    }

    sdmmc_host_t host    = SDSPI_HOST_DEFAULT();
    host.slot            = kSpiHost;
    host.max_freq_khz    = 20000;  // conservative; the bus is shared and short-lived

    sdspi_device_config_t slot_config = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot_config.gpio_cs               = kPinSdCs;
    slot_config.host_id               = kSpiHost;

    esp_vfs_fat_sdmmc_mount_config_t mount_config = {};
    mount_config.format_if_mount_failed           = false;  // never reformat a user's card
    mount_config.max_files                        = 4;
    mount_config.allocation_unit_size             = 16 * 1024;

    sdmmc_card_t* card = nullptr;
    err = esp_vfs_fat_sdspi_mount(kMountPoint, &host, &slot_config, &mount_config, &card);
    if (err != ESP_OK) {
        // No card, wrong format, or no reader. All non-fatal: the firmware
        // falls back to the assets partition.
        mclog::tagInfo(_tag, "no usable SD card ({}), using the assets partition", esp_err_to_name(err));
        face_push::note_no_card();
        spi_bus_free(kSpiHost);
        return false;
    }

    mclog::tagInfo(_tag, "SD mounted: {} ({} MB)", card->cid.name,
                   static_cast<uint32_t>(((uint64_t)card->csd.capacity * card->csd.sector_size) / (1024 * 1024)));

    apply_wifi_file();
    soma_config::apply_card_file();  // after wifi.txt, so soma.yaml wins if both are there
    face_push::apply_to_card();

    size_t total = 0;
    DIR* dir     = opendir(kFaceDir);
    if (dir == nullptr) {
        mclog::tagInfo(_tag, "no {} directory on the card, using the assets partition", kFaceDir);
    } else {
        struct dirent* entry = nullptr;
        while ((entry = readdir(dir)) != nullptr) {
            if (entry->d_name[0] == '.') {
                continue;
            }
            const std::string name = entry->d_name;
            const std::string path = std::string(kFaceDir) + "/" + name;
            read_one_file(path, name, total);
        }
        closedir(dir);
    }

    esp_vfs_fat_sdcard_unmount(kMountPoint, card);

    // Releases GPIO 35 so the display can claim it as its D/C line.
    spi_bus_free(kSpiHost);

    if (_assets.empty()) {
        mclog::tagInfo(_tag, "no face assets on the card, using the assets partition");
        return false;
    }

    mclog::tagInfo(_tag, "loaded {} face assets from SD ({} KB into PSRAM)", _assets.size(), total / 1024);

    // Names are the thing most likely to be wrong (8.3 mangling, case), so
    // show a few rather than making the next person guess.
    int shown = 0;
    for (const auto& entry : _assets) {
        mclog::tagInfo(_tag, "  asset: {}", entry.name);
        if (++shown >= 4) {
            break;
        }
    }
    if (find_asset("face.json") == nullptr) {
        mclog::tagError(_tag, "no face.json among the SD assets -- check FatFs long filename support");
    }
    return true;
}

bool sd_assets::get(const std::string& name, void*& ptr, size_t& size)
{
    Asset_t* entry = find_asset(name);
    if (entry == nullptr) {
        return false;
    }
    ptr  = entry->data;
    size = entry->size;
    return true;
}

bool sd_assets::available()
{
    return !_assets.empty();
}

void sd_assets::release()
{
    release_assets();
    _loaded = false;
}
