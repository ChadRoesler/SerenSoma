/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "custom.h"
#include "../../../utils/random.h"
#include <cmath>
#include <cstring>
#include <hal/hal.h>
#include <assets/assets.h>
#include <mooncake_log.h>
#include <esp_heap_caps.h>
#include <sdkconfig.h>

using namespace uitk;
using namespace uitk::lvgl_cpp;
using namespace stackchan::avatar;

static const std::string_view _tag = "custom-face";

namespace {

// LVGL wants 0~3600. The modifiers happily hand out negative angles.
int normalize_rotation(int rotation)
{
    rotation %= 3600;
    return rotation < 0 ? 3600 + rotation : rotation;
}

// Names the six-value enum resolves to when something calls setEmotion()
// directly instead of going through the protocol string.
const char* enum_emotion_name(const Emotion& emotion)
{
    switch (emotion) {
        case Emotion::Happy:
            return "happy";
        case Emotion::Angry:
            return "angry";
        case Emotion::Sad:
            return "sad";
        case Emotion::Doubt:
            return "confused";
        case Emotion::Sleepy:
            return "sleepy";
        case Emotion::Neutral:
        default:
            return "neutral";
    }
}

}  // namespace

/* -------------------------------------------------------------------------- */
/*                                  SpriteBrow                                 */
/* -------------------------------------------------------------------------- */

SpriteBrow::SpriteBrow(lv_obj_t* parent, const FaceManifest_t& manifest, bool isLeftBrow)
{
    _is_left_brow = isLeftBrow;
    _anchor       = isLeftBrow ? manifest.leftBrowPos : manifest.rightBrowPos;

    _shapes.load(isLeftBrow ? manifest.browShapesLeft : manifest.browShapesRight);

    const auto& name = isLeftBrow ? manifest.browLeftImage : manifest.browRightImage;
    const lv_image_dsc_t* src = nullptr;
    if (!name.empty()) {
        _dsc = assets::get_image(name);
        if (_dsc.data != nullptr) {
            src = &_dsc;
        } else {
            mclog::tagWarn(_tag, "brow image missing: {}", name);
        }
    }
    if (src == nullptr) {
        // only shapes drawn: start on the neutral one, or whichever came first
        src = _shapes.get("neutral") ? _shapes.get("neutral") : _shapes.first();
    }
    if (src == nullptr) {
        return;
    }

    _image = std::make_unique<Image>(parent);
    _image->setAlign(LV_ALIGN_CENTER);
    _image->setSrc(src);
    _image->setPivot(_image->getWidth() / 2, _image->getHeight() / 2);
    _image->setPos(_anchor.x, _anchor.y);
}

SpriteBrow::~SpriteBrow()
{
    _image.reset();
}

void SpriteBrow::applyPose(int offsetY, int rotation, const std::string& shape)
{
    if (!_image) {
        return;
    }

    if (!shape.empty()) {
        auto* s = _shapes.get(shape);
        if (s && lv_image_get_src(_image->get()) != s) {
            _image->setSrc(s);
            _image->setPivot(s->header.w / 2, s->header.h / 2);
        }
    }

    // Mirror the tilt on the right brow so one authored pose reads symmetrically.
    const int applied = _is_left_brow ? rotation : -rotation;
    _now_y            = offsetY;
    _now_rotation     = rotation;

    _image->setPos(_anchor.x, _anchor.y + offsetY);
    _image->setRotation(normalize_rotation(applied));
}

void SpriteBrow::setVisible(bool visible)
{
    if (_image) {
        _image->setHidden(!visible);
    }
}

/* -------------------------------------------------------------------------- */
/*                                 CustomAvatar                                */
/* -------------------------------------------------------------------------- */

bool CustomAvatar::init(lv_obj_t* parent, const lv_font_t* font)
{
    // The manifest is hundreds of small strings and map nodes, every one under the
    // ALWAYSINTERNAL limit, and internal heap is what TLS and AES run out of. It is
    // only read on an emotion change, so it can live in PSRAM. Runs before the
    // network starts, so nothing else is allocating through this window.
    mclog::tagInfo(_tag, "before manifest: internal heap free {} B", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    heap_caps_malloc_extmem_enable(16);
    _manifest = FaceManifest_t::load();
    heap_caps_malloc_extmem_enable(CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL);
    mclog::tagInfo(_tag, "after manifest: internal heap free {} B", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    if (!_manifest.valid) {
        _valid = false;
        return false;
    }

    _pannel = std::make_unique<Container>(parent);
    _pannel->align(LV_ALIGN_CENTER, 0, 0);
    _pannel->setSize(320, 240);
    _pannel->setRadius(0);
    _pannel->setBorderWidth(0);
    _pannel->setBgColor(_manifest.background);
    _pannel->removeFlag(LV_OBJ_FLAG_SCROLLABLE);

    // Base layer first, so everything composites on top of it.
    if (!_manifest.baseImage.empty()) {
        _base_dsc = assets::get_image(_manifest.baseImage);
        if (_base_dsc.data != nullptr) {
            _base = std::make_unique<Image>(_pannel->get());
            _base->setAlign(LV_ALIGN_CENTER);
            _base->setSrc(&_base_dsc);
            _base->setPos(0, 0);
        } else {
            mclog::tagWarn(_tag, "base image missing: {}", _manifest.baseImage);
        }
    }

    // Brows sit below the eyes in z-order so a heavy lid can overlap them --
    // unless the face draws them above (aboveEyes): then no lid ever covers them.
    if (!_manifest.browsAboveEyes) {
        _left_brow  = std::make_unique<SpriteBrow>(_pannel->get(), _manifest, true);
        _right_brow = std::make_unique<SpriteBrow>(_pannel->get(), _manifest, false);
    }

    auto left_eye  = std::make_unique<EyeRig>(_pannel->get(), _manifest, true);
    auto right_eye = std::make_unique<EyeRig>(_pannel->get(), _manifest, false);
    auto mouth     = std::make_unique<MouthRig>(_pannel->get(), _manifest);

    if (_manifest.browsAboveEyes) {
        _left_brow  = std::make_unique<SpriteBrow>(_pannel->get(), _manifest, true);
        _right_brow = std::make_unique<SpriteBrow>(_pannel->get(), _manifest, false);
    }

    // Hair over the face: above the eyes, brows and mouth, under the speech bubble
    // and every decorator (those are created later, so they land on top).
    bool any_overlay = !_manifest.overlayImage.empty();
    for (const auto& kv : _manifest.emotions) {
        any_overlay = any_overlay || (kv.second.overlaySet && !kv.second.overlay.empty());
    }
    if (any_overlay) {
        _overlay = std::make_unique<Image>(_pannel->get());
        _overlay->setAlign(LV_ALIGN_CENTER);
        _overlay->setPos(0, 0);
        if (auto* s = cachedImage(_manifest.overlayImage)) {
            _overlay->setSrc(s);
        } else {
            _overlay->setHidden(true);
        }
    }

    if (!left_eye->isValid() && !right_eye->isValid()) {
        mclog::tagError(_tag, "no eye layers rendered, falling back to the default face");
        _pannel.reset();
        _valid = false;
        return false;
    }

    _key_elements.leftEye  = std::move(left_eye);
    _key_elements.rightEye = std::move(right_eye);
    _key_elements.mouth    = std::move(mouth);
    _key_elements.speechBubble =
        std::make_unique<DefaultSpeechBubble>(_pannel->get(), lv_color_white(), _manifest.background, font);

    _valid = true;

    // The rigs have copied every sprite they need, so the name tables are dead
    // weight. Only emotions and decorators are consulted at runtime.
    _manifest.lidsUpperLeft.clear();
    _manifest.lidsUpperRight.clear();
    _manifest.lidsLowerLeft.clear();
    _manifest.lidsLowerRight.clear();
    _manifest.mouthShapes.clear();
    _manifest.browShapesLeft.clear();
    _manifest.browShapesRight.clear();

    // Land on a defined resting pose rather than whatever loaded first.
    setEmotionByName("neutral");

    mclog::tagInfo(_tag, "custom face '{}' ready, internal heap free {} B", _manifest.name,
                   heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    return true;
}

Container* CustomAvatar::getPanel() const
{
    if (_pannel) {
        return _pannel.get();
    }
    return NULL;
}

void CustomAvatar::applyDecorators(const std::vector<std::string>& names)
{
    // Drop whatever the previous emotion put on screen.
    for (int id : _emotion_decorator_ids) {
        removeDecorator(id);
    }
    _emotion_decorator_ids.clear();

    for (const auto& name : names) {
        if (name.empty() || name == "none") {
            continue;
        }
        auto it = _manifest.decorators.find(name);
        if (it == _manifest.decorators.end()) {
            mclog::tagWarn(_tag, "emotion refers to undefined decorator: {}", name);
            continue;
        }
        const auto& spec = it->second;
        _emotion_decorator_ids.push_back(
            addDecorator(std::make_unique<SpriteDecorator>(_pannel->get(), spec, anchorFor(spec.anchor))));
    }
}

const lv_image_dsc_t* CustomAvatar::cachedImage(const std::string& name)
{
    if (name.empty()) {
        return nullptr;
    }
    auto it = _images.find(name);
    if (it == _images.end()) {
        auto dsc = assets::get_image(name);
        if (dsc.data == nullptr) {
            mclog::tagWarn(_tag, "image missing: {}", name);
            return nullptr;
        }
        it = _images.emplace(name, dsc).first;
    }
    return &it->second;
}

void CustomAvatar::applyBackdrop(const EmotionPose_t& pose)
{
    // lv_image_set_src invalidates even for the same source, and these two cover the
    // whole screen: only touch them when the picture really changes.
    const auto swap_src = [](Image& image, const void* src) {
        if (lv_image_get_src(image.get()) != src) {
            image.setSrc(src);
        }
    };

    // the emotion's own base (the clown) or the face's
    if (_base) {
        const auto& want = pose.baseImage.empty() ? _manifest.baseImage : pose.baseImage;
        if (want == _manifest.baseImage) {
            swap_src(*_base, &_base_dsc);
        } else if (auto* s = cachedImage(want)) {
            swap_src(*_base, s);
        }
    }
    // the emotion's own hair over, none, or the face's
    if (_overlay) {
        const auto& want = pose.overlaySet ? pose.overlay : _manifest.overlayImage;
        auto* s = cachedImage(want);
        if (s) {
            swap_src(*_overlay, s);
        }
        _overlay->setHidden(s == nullptr);
    }
}

Vector2i CustomAvatar::anchorFor(const std::string& anchor) const
{
    // Anchoring to a part means decorators follow the face when it drifts on
    // breathing, instead of floating in absolute space.
    if (anchor == "left_eye" && _key_elements.leftEye) {
        return static_cast<EyeRig*>(_key_elements.leftEye.get())->anchorPoint();
    }
    if (anchor == "right_eye" && _key_elements.rightEye) {
        return static_cast<EyeRig*>(_key_elements.rightEye.get())->anchorPoint();
    }
    if (anchor == "mouth" && _key_elements.mouth) {
        return static_cast<MouthRig*>(_key_elements.mouth.get())->anchorPoint();
    }
    return Vector2i{0, 0};
}

int CustomAvatar::addNamedDecorator(const std::string& name, uint32_t lifetimeMs)
{
    if (!_valid) {
        return -1;
    }

    auto it = _manifest.decorators.find(name);
    if (it == _manifest.decorators.end()) {
        return -1;  // caller falls back to the built-in
    }

    DecoratorSpec_t spec = it->second;
    if (lifetimeMs > 0) {
        spec.lifetimeMs = lifetimeMs;
    }

    return addDecorator(std::make_unique<SpriteDecorator>(_pannel->get(), spec, anchorFor(spec.anchor)));
}

bool CustomAvatar::setEmotionByName(const std::string& emotion)
{
    // held: it's handled, by not happening. True, so the caller doesn't fall back to the six-pose enum.
    if (_held) {
        return true;
    }
    return applyPose(emotion);
}

void CustomAvatar::setHeld(bool held)
{
    _held = held;
    if (held && _react_timer != nullptr) {
        // a react that was showing would otherwise come back for its pose after the hold is let go
        lv_timer_delete(_react_timer);
        _react_timer = nullptr;
    }
}

bool CustomAvatar::applyPose(const std::string& emotion)
{
    if (!_valid) {
        return false;
    }

    const auto* pose = _manifest.pose(emotion);
    if (pose == nullptr) {
        return false;
    }

    _emotion         = Emotion::Neutral;
    _current_emotion = emotion;

    // the lids below are about to be set anyway, so a twitch mid-flick needs no undoing
    _pose              = pose;
    _twitch            = pose->twitch;
    _twitch_next       = 0;
    _twitch_flips_left = 0;
    _twitch_shown      = false;

    if (_key_elements.leftEye) {
        auto* eye = static_cast<EyeRig*>(_key_elements.leftEye.get());
        eye->applyLids(pose->upperLidLeft.empty() ? pose->upperLid : pose->upperLidLeft,
                       pose->lowerLidLeft.empty() ? pose->lowerLid : pose->lowerLidLeft);
        eye->setWeight(pose->eyeOpen);
    }
    if (_key_elements.rightEye) {
        auto* eye = static_cast<EyeRig*>(_key_elements.rightEye.get());
        eye->applyLids(pose->upperLidRight.empty() ? pose->upperLid : pose->upperLidRight,
                       pose->lowerLidRight.empty() ? pose->lowerLid : pose->lowerLidRight);
        eye->setWeight(pose->eyeOpen);
    }
    if (_key_elements.mouth) {
        static_cast<MouthRig*>(_key_elements.mouth.get())->applyShape(pose->mouthShape);
    }

    if (_left_brow) {
        _left_brow->applyPose(pose->browOffsetY, pose->browRotation, pose->browShapeLeft);
    }
    if (_right_brow) {
        _right_brow->applyPose(pose->browOffsetYRight, pose->browRotationRight, pose->browShapeRight);
    }

    applyBackdrop(*pose);
    applyDecorators(pose->decorators);
    if (_mouth_cycle_ms > 0 && pose->mouthCycleMs == 0 && _key_elements.mouth) {
        _key_elements.mouth->setWeight(0);  // back to closed, not wherever it had got to
    }
    _mouth_cycle_ms     = pose->mouthCycleMs;
    _mouth_cycle_start  = GetHAL().millis();
    _mouth_cycle_weight = -1;

    _base_look_left  = pose->lookLeft;
    _base_look_right = pose->lookRight;
    _roll_ms         = pose->rollMs;
    _roll_start      = GetHAL().millis();
    applyLooks({0, 0});

    if (pose->lightsOn) {
        const uint32_t was = _light_start;
        showLights(pose->lights[0], pose->lights[1], pose->lights[2], pose->lightsPulseMs);
        // a pulse counts from the pose starting, like the mouth does, so the two that are set to
        // the same time stay in step even when the pose comes back with the same color
        if (pose->lightsPulseMs > 0) {
            if (_light_start == was) {
                // same color as before: no fade to play, only the breath to restart
                for (int i = 0; i < 3; i++) {
                    _light_from[i] = _light_to[i];
                }
            }
            _light_start  = _mouth_cycle_start;
            _light_fading = true;
        }
    } else if (_light_lit) {
        showLights(0, 0, 0);
    }
    return true;
}

void CustomAvatar::applyLooks(const Vector2i& add)
{
    if (_key_elements.leftEye) {
        static_cast<EyeRig*>(_key_elements.leftEye.get())->setLook({_base_look_left.x + add.x, _base_look_left.y + add.y});
    }
    if (_key_elements.rightEye) {
        static_cast<EyeRig*>(_key_elements.rightEye.get())->setLook({_base_look_right.x + add.x, _base_look_right.y + add.y});
    }
}

void CustomAvatar::tickRoll(uint32_t nowMs)
{
    if (_roll_ms == 0) {
        return;
    }
    const uint32_t t = nowMs - _roll_start;
    if (t >= _roll_ms) {
        _roll_ms = 0;
        applyLooks({0, 0});
        return;
    }
    // over the top, from one side to the other: in from the middle, round, and back
    const float s = static_cast<float>(t) / static_cast<float>(_roll_ms);
    const float r = 90.0f * std::min(1.0f, std::min(4.0f * s, 4.0f * (1.0f - s)));
    const float a = (160.0f - 140.0f * s) * static_cast<float>(M_PI) / 180.0f;
    applyLooks({static_cast<int>(r * std::cos(a)), static_cast<int>(-r * std::sin(a))});
}

void CustomAvatar::tryRig(const Vector2i& lookLeft, const Vector2i& lookRight, const Vector2i& browLeft, const Vector2i& browRight,
                          uint32_t mouthCycleMs, uint32_t rollMs)
{
    _base_look_left  = lookLeft;
    _base_look_right = lookRight;
    _roll_ms         = rollMs;
    _roll_start      = GetHAL().millis();
    applyLooks({0, 0});
    if (_left_brow) {
        _left_brow->applyPose(browLeft.x, browLeft.y);
    }
    if (_right_brow) {
        _right_brow->applyPose(browRight.x, browRight.y);
    }
    if (_mouth_cycle_ms > 0 && mouthCycleMs == 0 && _key_elements.mouth) {
        _key_elements.mouth->setWeight(0);
    }
    _mouth_cycle_ms     = mouthCycleMs;
    _mouth_cycle_start  = GetHAL().millis();
    _mouth_cycle_weight = -1;
}

void CustomAvatar::rigState(char* out, size_t size)
{
    Vector2i left{0, 0}, right{0, 0};
    if (_key_elements.leftEye) {
        left = static_cast<EyeRig*>(_key_elements.leftEye.get())->irisNow();
    }
    if (_key_elements.rightEye) {
        right = static_cast<EyeRig*>(_key_elements.rightEye.get())->irisNow();
    }
    snprintf(out, size, "%d,%d %d,%d %d,%d %d,%d %d", left.x, left.y, right.x, right.y, _left_brow ? _left_brow->nowY() : 0,
             _left_brow ? _left_brow->nowRotation() : 0, _right_brow ? _right_brow->nowY() : 0,
             _right_brow ? _right_brow->nowRotation() : 0, _mouth_cycle_weight);
}

void CustomAvatar::tickMouth(uint32_t nowMs)
{
    if (_mouth_cycle_ms == 0 || !_key_elements.mouth) {
        return;
    }
    if (_talking) {
        _mouth_cycle_weight = -1;  // ask again the moment the speech is done
        return;
    }
    const float phase = static_cast<float>((nowMs - _mouth_cycle_start) % _mouth_cycle_ms) / static_cast<float>(_mouth_cycle_ms);
    // in tens: most of these land on the same drawing, so don't ask for it again
    const int weight = static_cast<int>(5.0f - 5.0f * std::cos(2.0f * static_cast<float>(M_PI) * phase) + 0.5f) * 10;
    if (weight != _mouth_cycle_weight) {
        _mouth_cycle_weight = weight;
        _key_elements.mouth->setWeight(weight);
    }
}

void CustomAvatar::showLights(uint8_t r, uint8_t g, uint8_t b, uint32_t pulseMs)
{
    _light_lit = r || g || b;
    if (_light_to[0] == r && _light_to[1] == g && _light_to[2] == b && _light_pulse_ms == pulseMs) {
        return;
    }
    _light_pulse_ms = pulseMs;
    for (int i = 0; i < 3; i++) {
        _light_from[i] = _light_now[i];
    }
    _light_to[0]  = r;
    _light_to[1]  = g;
    _light_to[2]  = b;
    _light_start  = GetHAL().millis();
    _light_fading = true;
}

void CustomAvatar::tickLights(uint32_t nowMs)
{
    constexpr uint32_t kFadeMs = 300;
    if (!_light_fading || nowMs - _light_last_write < 20) {
        return;
    }
    _light_last_write     = nowMs;
    const uint32_t elapsed = nowMs - _light_start;
    const uint32_t t       = std::min(elapsed, kFadeMs);
    float level            = 1.0f;
    if (_light_pulse_ms > 0) {
        // starts at full, down to 15% and back, so a pose arrives at the color it asked for
        const float phase = static_cast<float>(elapsed % _light_pulse_ms) / static_cast<float>(_light_pulse_ms);
        level             = 0.15f + 0.85f * (0.5f + 0.5f * std::cos(2.0f * static_cast<float>(M_PI) * phase));
    }
    for (int i = 0; i < 3; i++) {
        const int base = _light_from[i] + (static_cast<int>(_light_to[i]) - _light_from[i]) * static_cast<int>(t) / static_cast<int>(kFadeMs);
        _light_now[i]  = static_cast<uint8_t>(base * level);
    }
    _light_fading = t < kFadeMs || _light_pulse_ms > 0;
    // The LEDs take 5 bits of red and blue, 6 of green. Nothing to send if that hasn't moved,
    // which is most ticks of a slow pulse. The last step of a fade lands exactly either way.
    const bool sameOnLeds = ((_light_now[0] ^ _light_written[0]) & 0xF8) == 0 && ((_light_now[1] ^ _light_written[1]) & 0xFC) == 0 &&
                            ((_light_now[2] ^ _light_written[2]) & 0xF8) == 0;
    if (sameOnLeds && _light_fading) {
        return;
    }
    for (uint8_t led : {2, 3, 4, 5, 8, 9, 10, 11}) {
        GetHAL().setRgbColor(led, _light_now[0], _light_now[1], _light_now[2]);
    }
    GetHAL().refreshRgb();
    for (int i = 0; i < 3; i++) {
        _light_written[i] = _light_now[i];
    }
}

int CustomAvatar::spotAt(int x, int y, const char* gesture) const
{
    const bool tap = std::strcmp(gesture, "tap") == 0;
    for (size_t i = 0; i < _manifest.touchSpots.size(); i++) {
        const auto& s = _manifest.touchSpots[i];
        const bool answers = s.gesture == gesture || (s.gesture == "drag" && !tap);
        if (answers && x >= s.x && x < s.x + s.w && y >= s.y && y < s.y + s.h) {
            return static_cast<int>(i);
        }
    }
    return -1;
}

void CustomAvatar::react(const std::string& emotion, uint32_t ms)
{
    if (_held || _manifest.pose(emotion) == nullptr) {
        return;
    }
    // boop twice and the second one goes back to the same place as the first
    if (_react_timer == nullptr) {
        _react_back = _current_emotion;
    }
    setEmotionByName(emotion);
    _react_name = emotion;
    if (_react_timer != nullptr) {
        lv_timer_set_period(_react_timer, ms);
        lv_timer_reset(_react_timer);
        return;
    }
    _react_timer = lv_timer_create(
        [](lv_timer_t* timer) {
            auto* self         = static_cast<CustomAvatar*>(lv_timer_get_user_data(timer));
            self->_react_timer = nullptr;  // a repeat count of 1 deletes the timer after this
            // if something else set a pose meanwhile, that wins
            if (self->_current_emotion == self->_react_name) {
                self->setEmotionByName(self->_react_back.empty() ? "neutral" : self->_react_back);
            }
        },
        ms, this);
    lv_timer_set_repeat_count(_react_timer, 1);
}

void CustomAvatar::showTwitchLid(bool shown)
{
    if (_pose == nullptr) {
        return;
    }
    const bool both = _twitch.eye == "both";
    if (_key_elements.leftEye && (both || _twitch.eye == "left")) {
        static_cast<EyeRig*>(_key_elements.leftEye.get())
            ->applyLids(shown ? _twitch.lid : (_pose->upperLidLeft.empty() ? _pose->upperLid : _pose->upperLidLeft),
                        _pose->lowerLidLeft.empty() ? _pose->lowerLid : _pose->lowerLidLeft);
    }
    if (_key_elements.rightEye && (both || _twitch.eye == "right")) {
        static_cast<EyeRig*>(_key_elements.rightEye.get())
            ->applyLids(shown ? _twitch.lid : (_pose->upperLidRight.empty() ? _pose->upperLid : _pose->upperLidRight),
                        _pose->lowerLidRight.empty() ? _pose->lowerLid : _pose->lowerLidRight);
    }
}

void CustomAvatar::setTwitch(const PoseTwitch_t& twitch)
{
    if (_twitch_shown) {
        showTwitchLid(false);
    }
    _twitch            = twitch;
    _twitch_next       = 0;
    _twitch_flips_left = 0;
    _twitch_shown      = false;
}

void CustomAvatar::tickMotion(uint32_t nowMs)
{
    tickLights(nowMs);
    tickMouth(nowMs);
    tickRoll(nowMs);
    constexpr uint32_t kFlickMs = 70;
    if (!_twitch.on || static_cast<int32_t>(nowMs - _twitch_next) < 0) {
        return;
    }
    if (_twitch_flips_left <= 0) {
        // there and back for each flick, so it always lands on the pose's own lid
        _twitch_flips_left = _twitch.flutters * 2;
        _twitch_count++;
    }
    _twitch_shown = !_twitch_shown;
    showTwitchLid(_twitch_shown);
    _twitch_flips_left--;
    _twitch_next = nowMs + (_twitch_flips_left > 0
                                ? kFlickMs
                                : Random::getInstance().getInt(_twitch.everyMinMs, std::max(_twitch.everyMinMs, _twitch.everyMaxMs)));
}

void CustomAvatar::setEmotion(const Emotion& emotion)
{
    if (_held) {
        return;  // a stroke's happy face, and what it puts back after
    }
    if (setEmotionByName(enum_emotion_name(emotion))) {
        _emotion = emotion;
        return;
    }

    // Manifest does not describe it: let the base class fan the enum out so
    // decorators and any non-rig elements still react.
    Avatar::setEmotion(emotion);
}
