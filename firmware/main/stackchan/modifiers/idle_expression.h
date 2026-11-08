/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once
#include "../modifiable.h"
#include "../utils/random.h"
#include "../avatar/skins/custom/custom.h"
#include <smooth_ui_toolkit.hpp>
#include <hal/hal.h>
#include <cstdint>

namespace stackchan {

/**
 * @brief
 *
 */
class IdleExpressionModifier : public Modifier {
public:
    IdleExpressionModifier(uint32_t interval_min = 2000, uint32_t interval_max = 6000)
        : _interval_min(interval_min), _interval_max(interval_max)
    {
        _next_tick = GetHAL().millis() + 500;
    }

    void _update(Modifiable& stackchan) override
    {
        if (!stackchan.hasAvatar() || stackchan.avatar().isModifyLocked()) {
            return;
        }

        uint32_t now = GetHAL().millis();
        // the other eye catches up
        if (_lag_pending && now >= _lag_at) {
            stackchan.avatar().rightEye().setPosition(_lag_position);
            _lag_pending = false;
        }
        if (now < _next_tick) {
            return;
        }

        // 执行随机微表情
        perform_idle_emotion(stackchan.avatar());

        // 计算下一次触发时间
        uint32_t delay = Random::getInstance().getInt(_interval_min, _interval_max);
        _next_tick     = now + delay;
    }

private:
    void perform_idle_emotion(avatar::Avatar& avatar)
    {
        int action = Random::getInstance().getInt(0, 100);

        if (action < 70) {
            // 【动作 1：眼神游离】双眼协同移动一个小范围
            int offsetX = Random::getInstance().getInt(-20, 20);
            int offsetY = Random::getInstance().getInt(-15, 15);
            // Wren's right eye (screen left) leads, the dominant one; the other follows a beat later,
            // because two eyes landing on the same frame looks mechanical
            avatar.leftEye().setPosition({offsetX, offsetY});
            follow(avatar, {offsetX, offsetY});

            // 嘴巴也配合动一下
            avatar.mouth().setPosition({0, Random::getInstance().getInt(0, 10)});
        } else if (action < 80) {
            // 【动作 3：嘴巴歪一下】旋转角度
            // Rotation: 0~3600
            int rotation = Random::getInstance().getInt(-30, 30);
            // 加上基准值
            avatar.mouth().setRotation(rotation < 0 ? 3600 + rotation : rotation);
        } else {
            // 【动作 4：回归中性】
            reset_to_neutral(avatar);
        }
    }

    void reset_to_neutral(avatar::Avatar& avatar)
    {
        // 位置回归中心
        avatar.leftEye().setPosition({0, 0});
        follow(avatar, {0, 0});
        avatar.mouth().setPosition({0, 0});

        // 缩放回归正常
        avatar.leftEye().setSize(0);
        avatar.rightEye().setSize(0);

        // 旋转和权重回归
        avatar.mouth().setRotation(0);
        avatar.mouth().setWeight(0);
    }

    // the other eye: at once if the face says 0, otherwise after its glance lag (40 ms unless it says)
    void follow(avatar::Avatar& avatar, const uitk::Vector2i& position)
    {
        auto* custom  = dynamic_cast<avatar::CustomAvatar*>(&avatar);
        const int lag = custom ? custom->glanceLagMs() : 40;
        if (lag <= 0) {
            avatar.rightEye().setPosition(position);
            _lag_pending = false;
            return;
        }
        _lag_position = position;
        _lag_at       = GetHAL().millis() + lag;
        _lag_pending  = true;
    }

    bool _lag_pending = false;
    uint32_t _lag_at  = 0;
    uitk::Vector2i _lag_position{0, 0};

    uint32_t _interval_min;
    uint32_t _interval_max;
    uint32_t _next_tick = 0;
};

}  // namespace stackchan
