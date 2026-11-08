/*
 * SPDX-License-Identifier: MIT
 */
/*
 * Per-body settings, read from soma.yaml on the SD card.
 *
 * A release build has no secrets compiled in, so whatever makes one robot
 * *this* robot (its Wi-Fi, the voice server it checks in with, the token its
 * control server asks for, its tailnet key) arrives on the card instead:
 *
 *     wifi:
 *       ssid: MyNetwork
 *       password: hunter2
 *     voice: 192.168.0.200          # host, host:port, or a full http://host:port/ota/
 *     token: a-long-random-string   # what the control API asks for in X-StackChan-Token
 *     tailnet:
 *       key: tskey-auth-...
 *       name: soma
 *
 * Every key is optional. A key that is present replaces what the robot has
 * saved; an empty value ("" or nothing after the colon) forgets it, so the
 * build's own default comes back. A key that is absent leaves it alone.
 *
 * The file is read once, at boot, saved to NVS, and deleted, so a password or
 * token never outlives one boot on a card that moves between machines. It is
 * a small YAML subset, not YAML: "key: value" lines, one level of nesting by
 * indentation, # comments, optional quotes. That is all this file needs, and
 * a real parser would cost RAM the robot does not have.
 */
#pragma once
#include <string>

namespace soma_config {

/** @brief Read, save and delete soma.yaml. Call with the card mounted, after NVS is up. */
void apply_card_file();

/** @brief The control server's token: the saved one, else the build's. Empty means open on the LAN. */
const std::string& control_token();

/** @brief Where the token came from: "card", "build" or "none". Never the token itself. */
const char* control_token_source();

/** @brief Where the voice server address came from: "card" or "build". */
const char* voice_source();

/** @brief Tailnet auth key and device name: saved, else the build's. */
std::string tailnet_key();
std::string tailnet_name();

}  // namespace soma_config
