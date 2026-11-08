/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "custom.h"
#include <assets/assets.h>
#include <assets.h>
#include <ArduinoJson.hpp>
#include <mooncake_log.h>
#include <cstdio>
#include <cstdlib>

using namespace uitk;
using namespace stackchan::avatar;

static const std::string_view _tag = "custom-face";

/* -------------------------------------------------------------------------- */
/*                                  SpriteSet                                  */
/* -------------------------------------------------------------------------- */

bool SpriteSet::load(const std::vector<std::string>& names)
{
    _frames.clear();
    _frames.reserve(names.size());

    for (const auto& name : names) {
        auto dsc = assets::get_image(name);
        if (dsc.data == nullptr) {
            // get_image already logged why. Skip the frame rather than aborting,
            // so one typo costs one frame instead of the whole face.
            mclog::tagWarn(_tag, "skipping missing frame: {}", name);
            continue;
        }
        _frames.push_back(dsc);
    }

    return !_frames.empty();
}

const lv_image_dsc_t* SpriteSet::frame(int index) const
{
    if (_frames.empty()) {
        return nullptr;
    }
    index = clamp(index, 0, static_cast<int>(_frames.size()) - 1);
    return &_frames[index];
}

const lv_image_dsc_t* SpriteSet::frameForValue(int value, bool invert) const
{
    if (_frames.empty()) {
        return nullptr;
    }

    value = clamp(value, 0, 100);
    if (invert) {
        value = 100 - value;
    }

    const int last  = static_cast<int>(_frames.size()) - 1;
    const int index = (value * last + 50) / 100;
    return frame(index);
}

/* -------------------------------------------------------------------------- */
/*                                  SpriteMap                                  */
/* -------------------------------------------------------------------------- */

bool SpriteMap::load(const std::map<std::string, std::string>& names)
{
    _sprites.clear();

    for (const auto& entry : names) {
        auto dsc = assets::get_image(entry.second);
        if (dsc.data == nullptr) {
            mclog::tagWarn(_tag, "skipping missing sprite '{}': {}", entry.first, entry.second);
            continue;
        }
        if (entry.first.size() >= sizeof(Entry_t::key)) {
            mclog::tagWarn(_tag, "pose name too long, skipping: {}", entry.first);
            continue;
        }

        Entry_t slot;
        std::snprintf(slot.key, sizeof(slot.key), "%s", entry.first.c_str());
        slot.sprite = dsc;
        _sprites.push_back(slot);
    }

    return !_sprites.empty();
}

const lv_image_dsc_t* SpriteMap::get(const std::string& key) const
{
    for (const auto& entry : _sprites) {
        if (key == entry.key) {
            return &entry.sprite;
        }
    }
    return nullptr;
}

/* -------------------------------------------------------------------------- */
/*                                FaceManifest                                 */
/* -------------------------------------------------------------------------- */

namespace {

Vector2i read_point(ArduinoJson::JsonVariant value, Vector2i fallback)
{
    if (value.is<ArduinoJson::JsonArray>()) {
        auto arr = value.as<ArduinoJson::JsonArray>();
        if (arr.size() >= 2) {
            return Vector2i{arr[0].as<int>(), arr[1].as<int>()};
        }
    }
    return fallback;
}

std::string read_string(ArduinoJson::JsonVariant value, const std::string& fallback = "")
{
    if (value.is<const char*>()) {
        return value.as<const char*>();
    }
    return fallback;
}

int read_int(ArduinoJson::JsonVariant value, int fallback)
{
    if (value.is<int>()) {
        return value.as<int>();
    }
    return fallback;
}

std::map<std::string, std::string> read_string_map(ArduinoJson::JsonVariant value)
{
    std::map<std::string, std::string> out;
    if (value.is<ArduinoJson::JsonObject>()) {
        ArduinoJson::JsonObject object = value.as<ArduinoJson::JsonObject>();
        for (auto kv : object) {
            if (kv.value().is<const char*>()) {
                out.emplace(kv.key().c_str(), kv.value().as<const char*>());
            }
        }
    }
    return out;
}

std::vector<std::string> read_string_list(ArduinoJson::JsonVariant value)
{
    std::vector<std::string> out;
    if (value.is<ArduinoJson::JsonArray>()) {
        ArduinoJson::JsonArray array = value.as<ArduinoJson::JsonArray>();
        for (auto item : array) {
            if (item.is<const char*>()) {
                out.emplace_back(item.as<const char*>());
            }
        }
    }
    return out;
}

lv_color_t read_color(ArduinoJson::JsonVariant value, lv_color_t fallback)
{
    if (!value.is<const char*>()) {
        return fallback;
    }
    std::string text = value.as<const char*>();
    if (!text.empty() && text.front() == '#') {
        text.erase(text.begin());
    }
    if (text.size() != 6) {
        return fallback;
    }
    return lv_color_hex(static_cast<uint32_t>(strtoul(text.c_str(), nullptr, 16)));
}

}  // namespace

const EmotionPose_t* FaceManifest_t::pose(const std::string& emotion) const
{
    auto it = emotions.find(emotion);
    if (it == emotions.end()) {
        return nullptr;
    }
    return &it->second;
}

FaceManifest_t FaceManifest_t::load()
{
    FaceManifest_t manifest;

    // Routed through get_raw so a face.json on the microSD card shadows the
    // flashed one -- otherwise an SD face would load its images but read the
    // built-in manifest.
    void* data_ptr   = nullptr;
    size_t data_size = 0;
    if (!assets::get_raw("face.json", data_ptr, data_size)) {
        mclog::tagInfo(_tag, "no face.json on SD or in the assets partition, custom skin unavailable");
        return manifest;
    }

    ArduinoJson::JsonDocument doc;
    auto error = ArduinoJson::deserializeJson(doc, static_cast<const char*>(data_ptr), data_size);
    if (error) {
        mclog::tagError(_tag, "face.json parse failed: {}", error.c_str());
        return manifest;
    }

    manifest.name       = read_string(doc["name"], "unnamed");
    manifest.version    = read_int(doc["version"], 2);
    manifest.background = read_color(doc["background"], lv_color_black());
    manifest.baseImage  = read_string(doc["base"]);
    manifest.overlayImage = read_string(doc["overlay"]);

    if (manifest.version < 2) {
        mclog::tagError(_tag, "face.json is version {}, this firmware needs 2. Rebuild with tools/face.py",
                        manifest.version);
        return manifest;
    }

    /* ----------------------------------- Eyes ---------------------------------- */
    if (doc["eyes"].is<ArduinoJson::JsonObject>()) {
        auto eyes = doc["eyes"];

        manifest.scleraLeft  = read_string(eyes["scleraLeft"]);
        manifest.scleraRight = read_string(eyes["scleraRight"]);
        manifest.irisLeft    = read_string(eyes["irisLeft"]);
        manifest.irisRight   = read_string(eyes["irisRight"]);

        manifest.lidsUpperLeft  = read_string_map(eyes["lidsUpperLeft"]);
        manifest.lidsUpperRight = read_string_map(eyes["lidsUpperRight"]);
        manifest.lidsLowerLeft  = read_string_map(eyes["lidsLowerLeft"]);
        manifest.lidsLowerRight = read_string_map(eyes["lidsLowerRight"]);

        manifest.leftEyePos  = read_point(eyes["leftPos"], manifest.leftEyePos);
        manifest.rightEyePos = read_point(eyes["rightPos"], manifest.rightEyePos);
        manifest.socketSize  = read_point(eyes["socketSize"], manifest.socketSize);
        manifest.irisTravel  = read_point(eyes["irisTravel"], manifest.irisTravel);
        manifest.irisOffsetLeft  = read_point(eyes["irisOffsetLeft"], manifest.irisOffsetLeft);
        manifest.irisOffsetRight = read_point(eyes["irisOffsetRight"], manifest.irisOffsetRight);
        manifest.irisRoom        = read_point(eyes["irisRoom"], manifest.irisRoom);
        manifest.glanceLagMs     = clamp(read_int(eyes["glanceLagMs"], manifest.glanceLagMs), 0, 300);

        manifest.irisParallax = clamp(read_int(eyes["irisParallax"], manifest.irisParallax), 0, 100);
        manifest.clipRadius   = read_int(eyes["clipRadius"], manifest.clipRadius);

        manifest.lidSwap         = read_string(eyes["lidMode"]) == "swap";
        manifest.irisMaskSclera  = read_string(eyes["irisMask"]) == "sclera";
        manifest.scleraMaskLeft  = read_string(eyes["scleraMaskLeft"]);
        manifest.scleraMaskRight = read_string(eyes["scleraMaskRight"]);
    }

    /* ---------------------------------- Mouth ---------------------------------- */
    if (doc["mouth"].is<ArduinoJson::JsonObject>()) {
        auto mouth = doc["mouth"];

        if (mouth["shapes"].is<ArduinoJson::JsonObject>()) {
            ArduinoJson::JsonObject shapes = mouth["shapes"].as<ArduinoJson::JsonObject>();
            for (auto kv : shapes) {
                auto frames = read_string_list(kv.value());
                if (!frames.empty()) {
                    manifest.mouthShapes.emplace(kv.key().c_str(), std::move(frames));
                }
            }
        }

        manifest.mouthPos    = read_point(mouth["pos"], manifest.mouthPos);
        manifest.mouthTravel = read_point(mouth["travel"], manifest.mouthTravel);
    }

    /* ---------------------------------- Brows ---------------------------------- */
    if (doc["brows"].is<ArduinoJson::JsonObject>()) {
        auto brows = doc["brows"];

        manifest.browLeftImage  = read_string(brows["left"]);
        manifest.browRightImage = read_string(brows["right"]);
        manifest.leftBrowPos    = read_point(brows["leftPos"], manifest.leftBrowPos);
        manifest.rightBrowPos   = read_point(brows["rightPos"], manifest.rightBrowPos);
        manifest.browsAboveEyes = brows["aboveEyes"].is<bool>() && brows["aboveEyes"].as<bool>();

        if (brows["shapes"].is<ArduinoJson::JsonObject>()) {
            ArduinoJson::JsonObject shapes = brows["shapes"].as<ArduinoJson::JsonObject>();
            for (auto kv : shapes) {
                auto pair  = kv.value();
                auto left  = read_string(pair["left"]);
                auto right = read_string(pair["right"]);
                if (!left.empty()) manifest.browShapesLeft.emplace(kv.key().c_str(), left);
                if (!right.empty()) manifest.browShapesRight.emplace(kv.key().c_str(), right);
            }
        }
    }

    /* -------------------------------- Decorators ------------------------------- */
    if (doc["decorators"].is<ArduinoJson::JsonObject>()) {
        ArduinoJson::JsonObject decorators = doc["decorators"].as<ArduinoJson::JsonObject>();
        for (auto kv : decorators) {
            if (!kv.value().is<ArduinoJson::JsonObject>()) {
                continue;
            }
            auto node = kv.value();

            DecoratorSpec_t spec;
            spec.image      = read_string(node["image"]);
            spec.anchor     = read_string(node["anchor"], "face");
            spec.offset     = read_point(node["offset"], Vector2i{0, 0});
            spec.animate    = read_string(node["animate"]);
            spec.lifetimeMs = static_cast<uint32_t>(read_int(node["lifetimeMs"], 0));

            if (!spec.image.empty()) {
                manifest.decorators.emplace(kv.key().c_str(), std::move(spec));
            }
        }
    }

    /* --------------------------------- Emotions -------------------------------- */
    if (doc["emotions"].is<ArduinoJson::JsonObject>()) {
        ArduinoJson::JsonObject emotions = doc["emotions"].as<ArduinoJson::JsonObject>();
        for (auto kv : emotions) {
            if (!kv.value().is<ArduinoJson::JsonObject>()) {
                continue;
            }
            auto node = kv.value();

            EmotionPose_t pose;
            pose.upperLid   = read_string(node["upper"]);
            pose.lowerLid   = read_string(node["lower"]);
            pose.mouthShape = read_string(node["mouth"]);
            pose.decorator  = read_string(node["decorator"]);
            pose.eyeOpen    = clamp(read_int(node["eyeOpen"], 100), 0, 100);

            auto brow        = read_point(node["brow"], Vector2i{0, 0});
            auto browLeft    = read_point(node["browLeft"], brow);
            auto browRight   = read_point(node["browRight"], brow);
            pose.browOffsetY = browLeft.x;
            pose.browRotation = browLeft.y;
            pose.browOffsetYRight  = browRight.x;
            pose.browRotationRight = browRight.y;
            pose.lookLeft    = read_point(node["lookLeft"], Vector2i{0, 0});
            pose.lookRight   = read_point(node["lookRight"], Vector2i{0, 0});

            pose.upperLidLeft   = read_string(node["upperLeft"], pose.upperLid);
            pose.upperLidRight  = read_string(node["upperRight"], pose.upperLid);
            pose.lowerLidLeft   = read_string(node["lowerLeft"], pose.lowerLid);
            pose.lowerLidRight  = read_string(node["lowerRight"], pose.lowerLid);
            auto shape          = read_string(node["browShape"]);
            pose.browShapeLeft  = read_string(node["browShapeLeft"], shape);
            pose.browShapeRight = read_string(node["browShapeRight"], shape);
            pose.decorators     = read_string_list(node["decorators"]);
            if (pose.decorators.empty() && !pose.decorator.empty()) {
                pose.decorators.push_back(pose.decorator);
            }
            pose.baseImage  = read_string(node["base"]);
            pose.overlaySet = node["overlay"].is<const char*>();
            pose.overlay    = read_string(node["overlay"]);

            const int mouthCycle = read_int(node["motion"]["mouth_cycle"], 0);
            pose.mouthCycleMs    = mouthCycle > 0 ? clamp(mouthCycle, 300, 20000) : 0;
            const int roll       = read_int(node["motion"]["roll"], 0);
            pose.rollMs          = roll > 0 ? clamp(roll, 300, 5000) : 0;

            auto twitch = node["motion"]["twitch"];
            if (twitch.is<ArduinoJson::JsonObject>()) {
                pose.twitch.on   = true;
                pose.twitch.eye  = read_string(twitch["eye"], "right");
                pose.twitch.lid  = read_string(twitch["lid"], "closed");
                auto every       = read_point(twitch["every"], Vector2i{2500, 6000});
                pose.twitch.everyMinMs = every.x;
                pose.twitch.everyMaxMs = every.y;
                pose.twitch.flutters   = clamp(read_int(twitch["flutters"], 3), 1, 10);
            }

            auto lights = node["lights"];
            if (lights.is<ArduinoJson::JsonArray>() && lights.size() == 3) {
                pose.lightsOn = true;
                for (int i = 0; i < 3; i++) {
                    pose.lights[i] = clamp(lights[i].as<int>(), 0, 168);
                }
                const int pulse    = read_int(node["lightsPulse"], 0);
                pose.lightsPulseMs = pulse > 0 ? clamp(pulse, 300, 10000) : 0;
            }

            manifest.emotions.emplace(kv.key().c_str(), std::move(pose));
        }
    }

    auto touch = doc["touch"];
    if (touch.is<ArduinoJson::JsonArray>()) {
        manifest.touchDeclared = true;
        for (auto node : touch.as<ArduinoJson::JsonArray>()) {
            auto rect = node["rect"];
            if (!rect.is<ArduinoJson::JsonArray>() || rect.size() != 4) {
                continue;
            }
            TouchSpot_t spot;
            spot.name = read_string(node["name"]);
            spot.pose = read_string(node["pose"]);
            spot.x    = rect[0].as<int>();
            spot.y    = rect[1].as<int>();
            spot.w    = rect[2].as<int>();
            spot.h    = rect[3].as<int>();
            spot.ms   = clamp(read_int(node["ms"], 2000), 200, 15000);
            spot.gesture = read_string(node["gesture"], "tap");
            manifest.touchSpots.push_back(std::move(spot));
        }
    }

    auto head = doc["head"];
    if (head.is<ArduinoJson::JsonObject>()) {
        manifest.headDeclared = true;
        for (auto kv : head.as<ArduinoJson::JsonObject>()) {
            HeadTouch_t touch;
            touch.pose = read_string(kv.value()["pose"]);
            touch.ms   = clamp(read_int(kv.value()["ms"], 2000), 200, 15000);
            manifest.headTouches.emplace(kv.key().c_str(), std::move(touch));
        }
    }

    // A face needs eyes. Everything else degrades gracefully.
    manifest.valid = !manifest.scleraRight.empty() || !manifest.lidsUpperRight.empty();
    if (!manifest.valid) {
        mclog::tagError(_tag, "face.json describes no eyes, refusing to build custom face");
        return manifest;
    }

    mclog::tagInfo(_tag, "loaded face '{}': {} emotions, {} mouth shapes, {} decorators", manifest.name,
                   manifest.emotions.size(), manifest.mouthShapes.size(), manifest.decorators.size());
    return manifest;
}
