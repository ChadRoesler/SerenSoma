/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "custom.h"
#include <assets/assets.h>
#include <mooncake_log.h>

using namespace uitk;
using namespace uitk::lvgl_cpp;
using namespace stackchan::avatar;

static const std::string_view _tag = "custom-face";

// LVGL treats 256 as 1:1 and skips the transform, so the resting case is free.
static const int _scale_neutral = 256;
static const int _scale_min     = 208;
static const int _scale_max     = 304;

EyeRig::EyeRig(lv_obj_t* parent, const FaceManifest_t& manifest, bool isLeftEye)
{
    _is_left_eye = isLeftEye;
    _anchor      = isLeftEye ? manifest.leftEyePos : manifest.rightEyePos;
    _iris_travel = manifest.irisTravel;
    _iris_offset = isLeftEye ? manifest.irisOffsetLeft : manifest.irisOffsetRight;
    _iris_room   = manifest.irisRoom;
    _parallax    = manifest.irisParallax;
    _lid_swap    = manifest.lidSwap;

    const auto& sclera_name = isLeftEye ? manifest.scleraLeft : manifest.scleraRight;
    const auto& iris_name   = isLeftEye ? manifest.irisLeft : manifest.irisRight;
    const auto& upper_names = isLeftEye ? manifest.lidsUpperLeft : manifest.lidsUpperRight;
    const auto& lower_names = isLeftEye ? manifest.lidsLowerLeft : manifest.lidsLowerRight;

    // The socket clips everything below it. That is what keeps the iris inside
    // the eye and lets the upper lid slide out of view to open.
    _socket = std::make_unique<Container>(parent);
    _socket->setAlign(LV_ALIGN_CENTER);
    _socket->setSize(manifest.socketSize.x, manifest.socketSize.y);
    _socket->setPos(_anchor.x, _anchor.y);
    _socket->setBorderWidth(0);
    _socket->setBgOpa(0);
    _socket->setPaddingAll(0);
    _socket->setRadius(manifest.clipRadius);
    _socket->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
    _socket->setTransformPivot(manifest.socketSize.x / 2, manifest.socketSize.y / 2);

    if (manifest.clipRadius > 0) {
        // Not wrapped by smooth_ui_toolkit, so reach for the raw object.
        lv_obj_set_style_clip_corner(_socket->get(), true, LV_PART_MAIN);
    }

    /* --------------------------------- Sclera --------------------------------- */
    if (!sclera_name.empty()) {
        _sclera_dsc = assets::get_image(sclera_name);
        if (_sclera_dsc.data != nullptr) {
            _sclera = std::make_unique<Image>(_socket->get());
            _sclera->setAlign(LV_ALIGN_CENTER);
            _sclera->setSrc(&_sclera_dsc);
            _sclera->setPos(0, 0);
        } else {
            mclog::tagWarn(_tag, "sclera missing: {}", sclera_name);
        }
    }

    /* ------------------------------- Iris window ------------------------------- */
    // irisMask sclera: a window the size of the eye white, masked by an A8 copy of
    // it, holds the iris; the iris only ever shows where the white is.
    lv_obj_t* iris_parent = _socket->get();
    const auto& mask_name = isLeftEye ? manifest.scleraMaskLeft : manifest.scleraMaskRight;
    if (manifest.irisMaskSclera && _sclera && !mask_name.empty()) {
        _mask_dsc = assets::get_image(mask_name);
        if (_mask_dsc.data != nullptr) {
            _iris_window = std::make_unique<Container>(_socket->get());
            _iris_window->setAlign(LV_ALIGN_CENTER);
            _iris_window->setSize(_sclera_dsc.header.w, _sclera_dsc.header.h);
            _iris_window->setPos(0, 0);
            _iris_window->setBorderWidth(0);
            _iris_window->setBgOpa(0);
            _iris_window->setPaddingAll(0);
            _iris_window->setRadius(0);
            _iris_window->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_style_bitmap_mask_src(_iris_window->get(), &_mask_dsc, LV_PART_MAIN);
            iris_parent = _iris_window->get();
        } else {
            mclog::tagWarn(_tag, "sclera mask missing: {}", mask_name);
        }
    }

    /* ---------------------------------- Iris ---------------------------------- */
    if (!iris_name.empty()) {
        _iris_dsc = assets::get_image(iris_name);
        if (_iris_dsc.data != nullptr) {
            _iris = std::make_unique<Image>(iris_parent);
            _iris->setAlign(LV_ALIGN_CENTER);
            _iris->setSrc(&_iris_dsc);
            _iris->setPivot(_iris->getWidth() / 2, _iris->getHeight() / 2);
            _iris->setPos(0, 0);
        } else {
            mclog::tagWarn(_tag, "iris missing: {}", iris_name);
        }
    }

    /* --------------------------------- Lower lid -------------------------------- */
    if (_lids_lower.load(lower_names)) {
        _lid_lower = std::make_unique<Image>(_socket->get());
        _lid_lower->setAlign(LV_ALIGN_BOTTOM_MID);
        _lid_lower->setPos(0, 0);
    }

    /* --------------------------------- Upper lid -------------------------------- */
    if (_lids_upper.load(upper_names)) {
        _lid_upper = std::make_unique<Image>(_socket->get());
        _lid_upper->setAlign(LV_ALIGN_TOP_MID);
        _lid_upper->setPos(0, 0);
    }

    // swap lids keep their closed drawings at hand for every blink
    _upper_closed = _lids_upper.get("closed");
    _lower_closed = _lids_lower.get("closed");

    setWeight(100);
    setPosition(_position);
}

EyeRig::~EyeRig()
{
    _lid_upper.reset();
    _lid_lower.reset();
    _iris.reset();
    _iris_window.reset();
    _sclera.reset();
    _socket.reset();
}

void EyeRig::applyLids(const std::string& upper, const std::string& lower)
{
    if (_lid_swap) {
        // remember the pose's drawings; applyLidPosition() picks pose or closed
        if (auto* s = _lids_upper.get(upper)) _upper_pose = s;
        if (auto* s = _lids_lower.get(lower)) _lower_pose = s;
        applyLidPosition();
        return;
    }

    if (_lid_upper && !upper.empty()) {
        if (auto* sprite = _lids_upper.get(upper)) {
            _lid_upper->setSrc(sprite);
            _lid_upper_height = _lid_upper->getHeight();
        }
    }

    if (_lid_lower && !lower.empty()) {
        if (auto* sprite = _lids_lower.get(lower)) {
            _lid_lower->setSrc(sprite);
        }
    }

    // A taller or shorter lid changes how far it has to travel.
    applyLidPosition();
}

void EyeRig::applyLidPosition()
{
    if (_lid_swap) {
        // nothing slides: below half open (a blink is 25, sleepy sits low) the
        // closed drawings show, otherwise the pose's, each exactly where drawn
        const bool shut = _weight < 50;
        if (_lid_upper) {
            const lv_image_dsc_t* s = (shut && _upper_closed) ? _upper_closed : _upper_pose;
            if (s && s != _upper_shown) {
                _lid_upper->setSrc(s);
                _upper_shown = s;
            }
            _lid_upper->setPos(0, 0);
        }
        if (_lid_lower) {
            const lv_image_dsc_t* s = (shut && _lower_closed) ? _lower_closed : _lower_pose;
            if (s && s != _lower_shown) {
                _lid_lower->setSrc(s);
                _lower_shown = s;
            }
        }
        return;
    }
    if (!_lid_upper) {
        return;
    }

    if (_lid_upper_height <= 0) {
        _lid_upper_height = _lid_upper->getHeight();
    }

    // weight 100 == open == lid parked above the socket and clipped away.
    // weight 0   == shut == lid sitting at the top of the socket, covering it.
    const int offset_y = -map_range(_weight, 0, 100, 0, _lid_upper_height);
    _lid_upper->setPos(0, offset_y);
}

void EyeRig::setPosition(const Vector2i& position)
{
    Element::setPosition(position);

    if (!_socket) {
        return;
    }

    // The iris takes the full gaze offset; the socket follows a fraction of it.
    // Small deltas (breathing) read as a gentle whole-eye drift, large ones
    // (a glance) read as the iris travelling inside a mostly still eye.
    const int iris_x = map_range(clamp(_position.x + _look.x, -100, 100), -100, 100, -_iris_travel.x, _iris_travel.x);
    const int iris_y = map_range(clamp(_position.y + _look.y, -100, 100), -100, 100, -_iris_travel.y, _iris_travel.y);
    _iris_now        = Vector2i{iris_x, iris_y};

    if (_iris) {
        _iris->setPos(iris_x + _iris_offset.x, iris_y + _iris_offset.y);
    }

    const int follow_x = _iris_room.x >= 0 ? clamp(iris_x, -_iris_room.x, _iris_room.x) : iris_x;
    const int follow_y = _iris_room.y >= 0 ? clamp(iris_y, -_iris_room.y, _iris_room.y) : iris_y;
    _socket->setPos(_anchor.x + (follow_x * _parallax) / 100, _anchor.y + (follow_y * _parallax) / 100);
}

void EyeRig::setLook(const Vector2i& look)
{
    _look = look;
    setPosition(_position);
}

void EyeRig::setWeight(int weight)
{
    Feature::setWeight(weight);
    applyLidPosition();
}

void EyeRig::setRotation(int rotation)
{
    Element::setRotation(rotation);

    if (_socket) {
        _socket->setRotation(_rotation);
    }
}

void EyeRig::setVisible(bool visible)
{
    Element::setVisible(visible);

    if (_socket) {
        _socket->setHidden(!visible);
    }
}

void EyeRig::setSize(int size)
{
    const int previous = _size;
    Feature::setSize(size);

    // Scaling the iris alone reads as pupil dilation, which is cheaper and
    // more natural than scaling the whole eye.
    if (!_iris || previous == _size) {
        return;
    }

    const int scale = (_size == 0) ? _scale_neutral : map_range(_size, -100, 100, _scale_min, _scale_max);
    _iris->setScale(static_cast<uint32_t>(scale));
}
