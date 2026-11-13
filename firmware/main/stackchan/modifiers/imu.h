/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once
#include "../modifiable.h"
#include "../avatar/skins/custom/custom.h"
#include <hal/hal.h>
#include <cstdint>

namespace stackchan {

/**
 * @brief
 *
 */
class ImuEventModifier : public Modifier {
public:
    ImuEventModifier(uint32_t reactionDurationMs = 4000) : _reaction_duration_ms(reactionDurationMs)
    {
        _signal_connection = GetHAL().onImuMotionEvent.connect([this](ImuMotionEvent event) {
            if (event == ImuMotionEvent::Shake) {
                _event_shake = true;
            }
        });
    }

    ~ImuEventModifier()
    {
        GetHAL().onImuMotionEvent.disconnect(_signal_connection);
    }

    void _update(Modifiable& stackchan) override
    {
        uint32_t now = GetHAL().millis();

        // 收到晃动事件
        if (_event_shake) {
            _event_shake = false;
            handle_shake_start(stackchan, now);
        }

        // 如果处于晃动反应状态
        if (_is_reacting) {
            // a face showing its own dizzy pose moves its own mouth
            if (!_posed && now >= _next_toggle_tick) {
                _next_toggle_tick = now + 600;

                _toggle_phase      = !_toggle_phase;
                int mouth_rotation = _toggle_phase ? -25 : 25;

                auto& avatar = stackchan.avatar();
                avatar.mouth().setRotation(mouth_rotation);
                avatar.mouth().setWeight(65);
            }

            //  Lock motion modify and move home
            auto& motion = stackchan.motion();
            if (!motion.isModifyLocked()) {
                motion.setModifyLock(true);
                motion.goHome(300);
            }

            // 检查是否结束反应
            if (now >= _restore_at) {
                restore_state(stackchan);
            }
        }
    }

private:
    /**
     * A sprite face that draws a dizzy pose shows that, whole: its eyes, its decorators, its roll and
     * its lights. Which pose is the face's to say (face.yaml, head: shake: {pose: ...}); with nothing
     * said it's "dizzy". Until 12 Nov 2026 a shake hid the eyes and drew only the dizzy decorator, so
     * a face's dizzy eyes were never seen. A face with no such pose gets what it always got.
     */
    bool show_dizzy_pose(Modifiable& stackchan)
    {
        auto* custom = dynamic_cast<avatar::CustomAvatar*>(&stackchan.avatar());
        if (custom == nullptr) {
            return false;
        }
        std::string pose = "dizzy";
        const auto said  = custom->headTouches().find("shake");
        if (said != custom->headTouches().end()) {
            pose = said->second.pose;
        }
        if (!custom->hasPose(pose)) {
            return false;
        }
        // a little longer than the reaction, so the pose is still up when the head is let move again
        custom->react(pose, _reaction_duration_ms + 200);
        return true;
    }

    void handle_shake_start(Modifiable& stackchan, uint32_t now)
    {
        if (_is_reacting && _posed) {
            show_dizzy_pose(stackchan);  // still being shaken: the pose stays up that much longer
        }
        if (!_is_reacting && show_dizzy_pose(stackchan)) {
            _is_reacting = true;
            _posed       = true;
        }
        if (!_is_reacting) {
            // 首次触发时，记录状态以便恢复
            _is_reacting = true;

            auto& avatar = stackchan.avatar();

            avatar.setModifyLock(true);
            avatar.leftEye().setVisible(false);
            avatar.rightEye().setVisible(false);

            stackchan.avatar().removeDecorator(_dizzy_decorator_id);
            stackchan.avatar().removeDecorator(_shy_decorator_id);
            auto& av            = stackchan.avatar();
            _dizzy_decorator_id = av.addNamedDecorator("dizzy");
            if (_dizzy_decorator_id < 0) {
                _dizzy_decorator_id =
                    av.addDecorator(std::make_unique<avatar::DizzyDecorator>(lv_screen_active(), 0, 300));
            }

            _shy_decorator_id = av.addNamedDecorator("blush");
            if (_shy_decorator_id < 0) {
                _shy_decorator_id = av.addNamedDecorator("shy");
            }
            if (_shy_decorator_id < 0) {
                _shy_decorator_id = av.addDecorator(std::make_unique<avatar::ShyDecorator>(lv_screen_active(), 0));
            }
        }

        // 刷新恢复时间和切换时间
        _restore_at = now + _reaction_duration_ms;
        if (_next_toggle_tick <= now) {
            _next_toggle_tick = now;  // 立即触发第一次嘴巴动作
        }
    }

    void restore_state(Modifiable& stackchan)
    {
        if (!_is_reacting) {
            return;
        }

        if (!_posed) {
            auto& avatar = stackchan.avatar();
            avatar.setModifyLock(false);
            avatar.leftEye().setVisible(true);
            avatar.rightEye().setVisible(true);
            avatar.mouth().setWeight(0);
            avatar.mouth().setRotation(0);

            stackchan.avatar().removeDecorator(_dizzy_decorator_id);
            stackchan.avatar().removeDecorator(_shy_decorator_id);
            _dizzy_decorator_id = -1;
            _shy_decorator_id   = -1;
        }
        _posed = false;

        auto& motion = stackchan.motion();
        motion.setModifyLock(false);

        _is_reacting = false;
    }

    // 信号相关
    int _signal_connection;
    volatile bool _event_shake = false;

    // 状态控制
    bool _is_reacting          = false;
    bool _posed                = false;  // this shake is showing the face's own dizzy pose
    bool _toggle_phase         = false;
    uint32_t _restore_at       = 0;
    uint32_t _next_toggle_tick = 0;
    uint32_t _reaction_duration_ms;

    int _dizzy_decorator_id = -1;
    int _shy_decorator_id   = -1;
};

}  // namespace stackchan
