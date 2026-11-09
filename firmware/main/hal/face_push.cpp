/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "face_push.h"
#include <dirent.h>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_partition.h>
#include <esp_ota_ops.h>
#include <esp_rom_crc.h>
#include <sys/stat.h>
#include <unistd.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>

namespace {

// ESP_LOG, not mclog: only ESP_LOG reaches /api/log
constexpr const char* kTag = "FACE-PUSH";

constexpr const char* kFace = "/sdcard/face";
constexpr const char* kNew  = "/sdcard/face_new";
constexpr const char* kOld  = "/sdcard/face_old";
constexpr const char* kSwap = "/sdcard/face_swp";

constexpr size_t kSector    = 4096;
constexpr size_t kBody      = kSector;  // the first sector is only the mark, so it erases alone
constexpr size_t kMaxBytes  = 2500 * 1024;
constexpr size_t kChunk     = 16 * 1024;
constexpr uint32_t kKindFace    = 1;
constexpr uint32_t kKindRestore = 2;

struct Mark_t {
    char magic[4];
    uint32_t kind;
    uint32_t length;
    uint32_t crc;
};
constexpr char kMagic[4] = {'S', 'C', 'F', 'P'};

constexpr size_t kEraseAhead = 64 * 1024;   // a flash block: erased in one go, a fraction of a second

size_t _length  = 0;
size_t _written = 0;
size_t _erased  = 0;   // bytes of the slot erased so far, from 0
uint32_t _crc   = 0;
char _result[96] = {0};

/*
 * Where a bundle waits for the next boot: the app slot that ISN'T running. Until 8 Nov 2026 this
 * was always ota_1, the spare until the robot took firmware over the air: an OTA boots the app
 * from ota_1, after which every face push erased the running app and the flash driver aborted
 * (reset reason: panic, mid-upload). The same slot the next OTA would write, which is fine: the
 * bundle is read at the next boot and the slot is free again, and an OTA image written over a
 * waiting bundle just means that bundle never had its mark.
 *
 * Only while the running app is good: a pending-verify image (fresh from an OTA, before its first
 * check-in) can still roll back to the other slot, and a bundle there would leave nothing to boot.
 */
const esp_partition_t* slot()
{
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGW(kTag, "the running app hasn't checked in yet, so the other slot may be needed to roll back to");
        return nullptr;
    }
    return esp_ota_get_next_update_partition(nullptr);
}

void set_result(const char* text)
{
    std::snprintf(_result, sizeof(_result), "%s", text);
    ESP_LOGI(kTag, "%s", text);
}

bool write_mark(const esp_partition_t* p, uint32_t kind, uint32_t length, uint32_t crc)
{
    Mark_t mark{};
    std::memcpy(mark.magic, kMagic, sizeof(kMagic));
    mark.kind   = kind;
    mark.length = length;
    mark.crc    = crc;
    return esp_partition_write(p, 0, &mark, sizeof(mark)) == ESP_OK;
}

void clear_mark(const esp_partition_t* p)
{
    esp_partition_erase_range(p, 0, kSector);
}

bool read_mark(const esp_partition_t* p, Mark_t& mark)
{
    if (p == nullptr || esp_partition_read(p, 0, &mark, sizeof(mark)) != ESP_OK) {
        return false;
    }
    return std::memcmp(mark.magic, kMagic, sizeof(kMagic)) == 0;
}

bool exists(const char* path)
{
    struct stat st{};
    return stat(path, &st) == 0;
}

void remove_tree(const char* path)
{
    DIR* dir = opendir(path);
    if (dir == nullptr) {
        return;
    }
    while (struct dirent* entry = readdir(dir)) {
        if (std::strcmp(entry->d_name, ".") != 0 && std::strcmp(entry->d_name, "..") != 0) {
            std::remove((std::string(path) + "/" + entry->d_name).c_str());
        }
    }
    closedir(dir);
    rmdir(path);
}

bool valid_name(const char* name, size_t length)
{
    if (length == 0 || length > 32 || name[0] == '.') {
        return false;
    }
    for (size_t i = 0; i < length; i++) {
        const char c = name[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-')) {
            return false;
        }
    }
    return true;
}

bool body_crc_ok(const esp_partition_t* p, const Mark_t& mark, uint8_t* buffer)
{
    uint32_t crc = 0;
    for (size_t done = 0; done < mark.length;) {
        const size_t n = std::min(kChunk, static_cast<size_t>(mark.length) - done);
        if (esp_partition_read(p, kBody + done, buffer, n) != ESP_OK) {
            return false;
        }
        crc = esp_rom_crc32_le(crc, buffer, n);
        done += n;
    }
    return crc == mark.crc;
}

/** @brief Every file in the bundle into face_new. Returns how many, or -1. */
int unpack(const esp_partition_t* p, const Mark_t& mark, uint8_t* buffer)
{
    remove_tree(kNew);
    if (mkdir(kNew, 0775) != 0) {
        ESP_LOGE(kTag, "cannot make %s", kNew);
        return -1;
    }

    int files        = 0;
    bool has_manifest = false;
    size_t at        = 0;
    while (at < mark.length) {
        uint8_t name_length = 0;
        char name[33]       = {0};
        uint32_t size       = 0;
        if (at + 1 > mark.length || esp_partition_read(p, kBody + at, &name_length, 1) != ESP_OK) {
            return -1;
        }
        at += 1;
        if (name_length > 32 || at + name_length + 4 > mark.length ||
            esp_partition_read(p, kBody + at, name, name_length) != ESP_OK || !valid_name(name, name_length)) {
            ESP_LOGE(kTag, "bad name in the bundle");
            return -1;
        }
        at += name_length;
        if (esp_partition_read(p, kBody + at, &size, 4) != ESP_OK || at + 4 + size > mark.length) {
            ESP_LOGE(kTag, "bad size for %s", name);
            return -1;
        }
        at += 4;

        const std::string path = std::string(kNew) + "/" + name;
        FILE* f                = fopen(path.c_str(), "wb");
        if (f == nullptr) {
            ESP_LOGE(kTag, "cannot write %s", path.c_str());
            return -1;
        }
        for (size_t done = 0; done < size;) {
            const size_t n = std::min(kChunk, static_cast<size_t>(size) - done);
            if (esp_partition_read(p, kBody + at + done, buffer, n) != ESP_OK || fwrite(buffer, 1, n, f) != n) {
                ESP_LOGE(kTag, "short write on %s", name);
                fclose(f);
                return -1;
            }
            done += n;
        }
        fclose(f);
        at += size;
        files++;
        has_manifest = has_manifest || std::strcmp(name, "face.json") == 0;
    }
    if (!has_manifest) {
        ESP_LOGE(kTag, "the bundle has no face.json");
        return -1;
    }
    return files;
}

}  // namespace

bool face_push::begin(size_t length)
{
    const esp_partition_t* p = slot();
    if (p == nullptr || length == 0 || length > kMaxBytes || kBody + length > p->size) {
        return false;
    }
    // Only the mark's sector now; the body is erased a block ahead of the writes (write()). Erasing
    // all of it first took long enough, with the upload unread, that the sender's TCP backed off
    // and the robot's receive timed out (8 Nov 2026: "the upload was cut short").
    if (esp_partition_erase_range(p, 0, kBody) != ESP_OK) {
        return false;
    }
    _length  = length;
    _written = 0;
    _erased  = kBody;
    _crc     = 0;
    return true;
}

bool face_push::write(const void* data, size_t length)
{
    const esp_partition_t* p = slot();
    if (p == nullptr || _written + length > _length) {
        return false;
    }
    const size_t end = kBody + _written + length;
    if (end > _erased) {
        const size_t total = (kBody + _length + kSector - 1) / kSector * kSector;
        const size_t upto  = std::min(total, (end + kEraseAhead - 1) / kEraseAhead * kEraseAhead);
        if (esp_partition_erase_range(p, _erased, upto - _erased) != ESP_OK) {
            return false;
        }
        _erased = upto;
    }
    if (esp_partition_write(p, kBody + _written, data, length) != ESP_OK) {
        return false;
    }
    _crc = esp_rom_crc32_le(_crc, static_cast<const uint8_t*>(data), length);
    _written += length;
    return true;
}

bool face_push::commit(uint32_t crc)
{
    const esp_partition_t* p = slot();
    if (p == nullptr || _written != _length || _crc != crc) {
        ESP_LOGE(kTag, "upload rejected: %u of %u bytes, crc %08lx, expected %08lx", (unsigned)_written, (unsigned)_length,
                 (unsigned long)_crc, (unsigned long)crc);
        return false;
    }
    return write_mark(p, kKindFace, _length, crc);
}

bool face_push::request_restore()
{
    const esp_partition_t* p = slot();
    if (p == nullptr) {
        return false;
    }
    clear_mark(p);
    return write_mark(p, kKindRestore, 0, 0);
}

void face_push::apply_to_card()
{
    // power died between the two renames of an earlier swap
    if (!exists(kFace) && exists(kOld)) {
        ESP_LOGW(kTag, "no face on the card: putting face_old back");
        rename(kOld, kFace);
    }

    const esp_partition_t* p = slot();
    Mark_t mark{};
    if (!read_mark(p, mark)) {
        return;
    }

    if (mark.kind == kKindRestore) {
        if (!exists(kOld)) {
            set_result("no previous face on the card to put back");
        } else {
            remove_tree(kSwap);
            const bool ok = !exists(kFace) || rename(kFace, kSwap) == 0;
            if (ok && rename(kOld, kFace) == 0) {
                if (exists(kSwap)) {
                    rename(kSwap, kOld);
                }
                set_result("previous face restored");
            } else {
                set_result("could not swap the faces back");
            }
        }
        clear_mark(p);
        return;
    }

    uint8_t* buffer = static_cast<uint8_t*>(heap_caps_malloc(kChunk, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (buffer == nullptr) {
        set_result("no memory to install the waiting face");
        clear_mark(p);
        return;
    }

    if (mark.kind != kKindFace || mark.length > kMaxBytes || !body_crc_ok(p, mark, buffer)) {
        set_result("the waiting face failed its checksum: nothing changed");
    } else {
        const int files = unpack(p, mark, buffer);
        if (files < 0) {
            remove_tree(kNew);
            set_result("could not write the new face to the card: nothing changed");
        } else {
            remove_tree(kOld);
            if (exists(kFace) && rename(kFace, kOld) != 0) {
                remove_tree(kNew);
                set_result("could not move the old face aside: nothing changed");
            } else if (rename(kNew, kFace) != 0) {
                set_result("could not swap in the new face");   // the check at the top puts the old one back
            } else {
                char text[64];
                std::snprintf(text, sizeof(text), "installed a new face: %d files", files);
                set_result(text);
            }
        }
    }
    heap_caps_free(buffer);
    clear_mark(p);
}

void face_push::note_no_card()
{
    Mark_t mark{};
    if (read_mark(slot(), mark)) {
        set_result("a face is waiting, but there is no SD card");
    }
}

const char* face_push::last_result()
{
    return _result;
}
