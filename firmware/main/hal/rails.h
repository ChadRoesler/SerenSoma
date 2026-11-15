/*
 * SPDX-License-Identifier: MIT
 */
#pragma once
#include <cstdint>

/**
 * @brief Which LED is which, by where it sits on the robot.
 *
 * The twelve LEDs are one chain: 0 to 5 run along one rail from the front of the head to the back,
 * and 6 to 11 come back along the other rail from the back to the front (the head board's schematic,
 * and lighting them one at a time on 12 Nov 2026). So the front pair is 0 and 11, and the pair
 * behind it is 1 and 10. Until 13 Nov 2026 the status lights used 0 and 6, and the camera's 1 and 7,
 * on the belief that both rails counted the same way: the front on one side, the back on the other.
 *
 * The front two of each rail are the robot's own and no face's colours touch them:
 *   kStatusLeds  the microphone and reply light
 *   kCameraLeds  the red light that is on whenever the camera is
 * A face's colours fade the other eight, kMoodLeds.
 */
namespace rails {

inline constexpr uint8_t kStatusLeds[] = {0, 11};
inline constexpr uint8_t kCameraLeds[] = {1, 10};
inline constexpr uint8_t kMoodLeds[]   = {2, 3, 4, 5, 6, 7, 8, 9};

}  // namespace rails
