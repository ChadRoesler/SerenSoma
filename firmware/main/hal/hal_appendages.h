/*
 * SPDX-License-Identifier: MIT
 */
/*
 * Appendages: the things plugged into the body that the base firmware doesn't know by itself
 * (docs/appendages.md). Each one is a line of soma.yaml's "appendages:" section, the kind first
 * and its wiring after:
 *
 *     appendages:
 *       light: flashlight en=9
 *
 * The registry reads the saved list at boot, gives each appendage its pins or refuses it, and
 * holds every claimed pin low before anything else runs. A refused appendage is not an error for
 * the robot: it boots, and the appendage shows ok: false with the reason.
 *
 * First kind, 19 Nov 2026: "flashlight", a stock M5 Unit FlashLight in torch mode, dimmed by
 * chopping its enable line.
 */
#pragma once
#include <string>

struct cJSON;

namespace appendages {

/** @brief Read the saved list and claim pins. Call once, before the control server takes requests. */
void start();

/** @brief Every configured appendage, as the array GET /api/appendages answers with. */
cJSON* list();

/**
 * @brief Run one action on one appendage: body is {"action": "...", ...}.
 * @return nullptr on success with answer filled, else the HTTP status, with error filled.
 */
const char* act(const std::string& name, const cJSON* body, cJSON* answer, std::string& error);

/** @brief The night ceiling on a light's level, 0 to 100. 100 is no ceiling. Not saved. */
void set_light_cap(int level);

}  // namespace appendages
