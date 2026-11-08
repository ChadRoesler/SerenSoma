/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once
#include <cstddef>
#include <cstdint>

/**
 * @brief The head sensor's raw readings while it's being touched, for GET /api/touch-raw.
 *
 * Kept in PSRAM. Idle readings aren't kept, so it holds about a minute of actual touching.
 */
namespace touch_log {

// Taps on the screen share the feed: the head gestures use 0 to 4.
constexpr int kBoopNose     = 10;
constexpr int kKissForehead = 11;
constexpr int kSpotFirst    = 100;  // a face's touch spots: this plus the spot's place in its list

/** @brief One sensor read: the strength in each of the 3 zones, and the gesture it completed. */
void add(uint32_t ms, const uint8_t zones[3], int gesture);

/**
 * @brief Readings newer than since_ms as lines of "ms zone0 zone1 zone2 gesture".
 * @return bytes written
 */
size_t read(uint32_t since_ms, char* out, size_t capacity);

}  // namespace touch_log
