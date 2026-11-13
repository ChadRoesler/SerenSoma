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

/**
 * @brief The touch chip itself, for the bench (GET and POST /api/touch-probe): every channel it has,
 *        not just the three the head's gestures read, and the sensitivity it's running at.
 *
 * The Si12T has 12 channels. The head's three pads are on channels 1 to 3: back, middle and front.
 * The board's schematic (SCH_Touch) has channels 5 to 9 tied to ground and 10 to 12 unconnected,
 * so nothing but 1 to 3 should ever read. The probe still returns all 12, as the proof.
 */
namespace touch_bench {

struct Probe_t {
    uint8_t channels[12];    // 0 none, 1 low, 2 mid, 3 high; channel 1 first
    uint8_t sensitivity[6];  // registers 0x02..0x07 as the chip holds them, two channels a byte
    uint8_t ctrl1;
    uint8_t ctrl2;
    uint8_t hold[2];         // channel hold, 0x0C and 0x0D: a set bit is a channel switched off
    bool high;               // the sensitivity last asked for: type and level
    int level;
};

/** @brief Read the chip now. False if there's no sensor or it didn't answer. */
bool probe(Probe_t& out);

/** @brief Change the sensitivity until the next boot: type low or high, level 0 to 7. Never saved. */
bool set_sensitivity(bool high, int level);

}  // namespace touch_bench

/**
 * @brief Touches on the screen as the face saw them, for the bench (GET /api/screen-touch): where
 *        each began and ended and what it was read as. The finger that's down right now is read
 *        from the touch driver when asked; this keeps the ones that are over.
 */
namespace screen_touch {

struct Touch_t {
    uint32_t ms;
    int16_t x0, y0;       // where the finger came down
    int16_t x1, y1;       // where it lifted
    const char* gesture;  // "tap", "left", "right", "up", "down": a string literal, never freed
};

constexpr int kKept = 16;

/** @brief Keep one finished touch. Called from the screen's own task. */
void add(uint32_t ms, int x0, int y0, int x1, int y1, const char* gesture);

/** @brief The touches kept, oldest first. @return how many were written, at most kKept */
int recent(Touch_t* out);

}  // namespace screen_touch
