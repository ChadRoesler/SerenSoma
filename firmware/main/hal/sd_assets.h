/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once
#include <cstddef>
#include <string>
#include <vector>

/**
 * @brief Face assets read from microSD at boot.
 *
 * Deliberately a small standalone header rather than methods on Hal, so
 * assets/assets.cpp can look assets up without including all of hal.h.
 *
 * See hal/hal_sdcard.cpp for why the card is only readable during early boot
 * (GPIO 35 is both the card's MISO and the LCD's Data/Command line).
 */
namespace sd_assets {

/**
 * @brief Mount the card, read /sdcard/face into PSRAM, unmount, free the bus.
 *
 * MUST be called before the display claims SPI3. Safe to call with no card
 * present -- it reports false and the firmware falls back to the assets
 * partition.
 *
 * @return true if at least one asset was loaded
 */
bool load();

/**
 * @brief Look up an asset read from the card.
 *
 * @param name file name as it appeared in /sdcard/face, e.g. "f_iris.bin"
 * @return false if the card supplied no such file
 */
bool get(const std::string& name, void*& ptr, size_t& size);

/**
 * @brief The names of the card's assets that start with prefix, as they were read (lowercase).
 * A face's sounds are "snd_<name>.ogg": names_with_prefix("snd_") lists them.
 */
std::vector<std::string> names_with_prefix(const std::string& prefix);

/**
 * @brief Whether a face was loaded from the card this boot.
 */
bool available();

/**
 * @brief Free the PSRAM holding the card's assets.
 *
 * Only safe once nothing references them, so in practice this is never called
 * during a normal session.
 */
void release();

}  // namespace sd_assets
