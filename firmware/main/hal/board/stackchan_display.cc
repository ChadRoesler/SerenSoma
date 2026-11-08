/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "stackchan_display.h"
#include <esp_log.h>
#include <esp_err.h>
#include <esp_lvgl_port.h>
#include <esp_psram.h>
#include <vector>
#include <cstring>
#include <src/misc/cache/lv_cache.h>
#include <settings.h>
#include <lvgl.h>
#include <lvgl_theme.h>
#include <stackchan/stackchan.h>
#include <assets/lang_config.h>
#include <hal/hal.h>
#include <hal/touch_log.h>

using namespace stackchan;
using namespace stackchan::avatar;

#define TAG "StackChanAvatarDisplay"

LV_FONT_DECLARE(BUILTIN_TEXT_FONT);
LV_FONT_DECLARE(BUILTIN_ICON_FONT);
LV_FONT_DECLARE(font_material_symbols_30_4);

// Have to register themes, so the asset apply can update the text font
void StackChanAvatarDisplay::InitializeLcdThemes()
{
    auto text_font       = std::make_shared<LvglBuiltInFont>(&BUILTIN_TEXT_FONT);
    auto icon_font       = std::make_shared<LvglBuiltInFont>(&BUILTIN_ICON_FONT);
    auto large_icon_font = std::make_shared<LvglBuiltInFont>(&font_material_symbols_30_4);

    // light theme
    auto light_theme = new LvglTheme("light");
    light_theme->set_background_color(lv_color_hex(0xFFFFFF));        // rgb(255, 255, 255)
    light_theme->set_text_color(lv_color_hex(0x000000));              // rgb(0, 0, 0)
    light_theme->set_chat_background_color(lv_color_hex(0xE0E0E0));   // rgb(224, 224, 224)
    light_theme->set_user_bubble_color(lv_color_hex(0x00FF00));       // rgb(0, 128, 0)
    light_theme->set_assistant_bubble_color(lv_color_hex(0xDDDDDD));  // rgb(221, 221, 221)
    light_theme->set_system_bubble_color(lv_color_hex(0xFFFFFF));     // rgb(255, 255, 255)
    light_theme->set_system_text_color(lv_color_hex(0x000000));       // rgb(0, 0, 0)
    light_theme->set_border_color(lv_color_hex(0x000000));            // rgb(0, 0, 0)
    light_theme->set_low_battery_color(lv_color_hex(0x000000));       // rgb(0, 0, 0)
    light_theme->set_text_font(text_font);
    light_theme->set_icon_font(icon_font);
    light_theme->set_large_icon_font(large_icon_font);

    // dark theme
    auto dark_theme = new LvglTheme("dark");
    dark_theme->set_background_color(lv_color_hex(0x000000));        // rgb(0, 0, 0)
    dark_theme->set_text_color(lv_color_hex(0xFFFFFF));              // rgb(255, 255, 255)
    dark_theme->set_chat_background_color(lv_color_hex(0x1F1F1F));   // rgb(31, 31, 31)
    dark_theme->set_user_bubble_color(lv_color_hex(0x00FF00));       // rgb(0, 128, 0)
    dark_theme->set_assistant_bubble_color(lv_color_hex(0x222222));  // rgb(34, 34, 34)
    dark_theme->set_system_bubble_color(lv_color_hex(0x000000));     // rgb(0, 0, 0)
    dark_theme->set_system_text_color(lv_color_hex(0xFFFFFF));       // rgb(255, 255, 255)
    dark_theme->set_border_color(lv_color_hex(0xFFFFFF));            // rgb(255, 255, 255)
    dark_theme->set_low_battery_color(lv_color_hex(0xFF0000));       // rgb(255, 0, 0)
    dark_theme->set_text_font(text_font);
    dark_theme->set_icon_font(icon_font);
    dark_theme->set_large_icon_font(large_icon_font);

    auto& theme_manager = LvglThemeManager::GetInstance();
    theme_manager.RegisterTheme("light", light_theme);
    theme_manager.RegisterTheme("dark", dark_theme);
}

StackChanAvatarDisplay::StackChanAvatarDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                                               int width, int height, int offset_x, int offset_y, bool mirror_x,
                                               bool mirror_y, bool swap_xy)
    : LvglDisplay(), panel_io_(panel_io), panel_(panel)
{
    width_  = width;
    height_ = height;

    // Initialize LCD themes
    InitializeLcdThemes();

    // Load theme from settings
    Settings settings("display", false);
    std::string theme_name = settings.GetString("theme", "light");
    current_theme_         = LvglThemeManager::GetInstance().GetTheme(theme_name);

    // Draw white screen
    std::vector<uint16_t> buffer(width_, 0xFFFF);
    for (int y = 0; y < height_; y++) {
        esp_lcd_panel_draw_bitmap(panel_, 0, y, width_, y + 1, buffer.data());
    }

    // Set the display to on
    ESP_LOGI(TAG, "Turning display on");
    {
        esp_err_t __err = esp_lcd_panel_disp_on_off(panel_, true);
        if (__err == ESP_ERR_NOT_SUPPORTED) {
            ESP_LOGW(TAG, "Panel does not support disp_on_off; assuming ON");
        } else {
            ESP_ERROR_CHECK(__err);
        }
    }

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

#if CONFIG_SPIRAM
    // lv image cache, currently only PNG is supported
    size_t psram_size_mb = esp_psram_get_size() / 1024 / 1024;
    if (psram_size_mb >= 8) {
        lv_image_cache_resize(2 * 1024 * 1024, true);
        ESP_LOGI(TAG, "Use 2MB of PSRAM for image cache");
    } else if (psram_size_mb >= 2) {
        lv_image_cache_resize(512 * 1024, true);
        ESP_LOGI(TAG, "Use 512KB of PSRAM for image cache");
    }
#endif

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    // port_cfg.task_priority   = 20;
    port_cfg.task_priority = 3;
#if CONFIG_SOC_CPU_CORES_NUM > 1
    port_cfg.task_affinity = 1;
#endif
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    const lvgl_port_display_cfg_t display_cfg = {
        .io_handle      = panel_io_,
        .panel_handle   = panel_,
        .control_handle = nullptr,
        .buffer_size    = static_cast<uint32_t>(width_ * 20),
        .double_buffer  = false,
        .trans_size     = 0,
        .hres           = static_cast<uint32_t>(width_),
        .vres           = static_cast<uint32_t>(height_),
        .monochrome     = false,
        .rotation =
            {
                .swap_xy  = swap_xy,
                .mirror_x = mirror_x,
                .mirror_y = mirror_y,
            },
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags =
            {
                .buff_dma     = 1,
                .buff_spiram  = 0,
                .sw_rotate    = 0,
                .swap_bytes   = 1,
                .full_refresh = 0,
                .direct_mode  = 0,
            },
    };

    display_ = lvgl_port_add_disp(&display_cfg);
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "Failed to add display");
        return;
    }

    if (offset_x != 0 || offset_y != 0) {
        lv_display_set_offset(display_, offset_x, offset_y);
    }

    // Create a timer to hide the preview image
    esp_timer_create_args_t preview_timer_args = {
        .callback =
            [](void* arg) {
                StackChanAvatarDisplay* display = static_cast<StackChanAvatarDisplay*>(arg);
                display->SetPreviewImage(nullptr);
            },
        .arg                   = this,
        .dispatch_method       = ESP_TIMER_TASK,
        .name                  = "preview_timer",
        .skip_unhandled_events = false,
    };
    esp_timer_create(&preview_timer_args, &preview_timer_);

    // Create boot logo label if not warm boot
    if (GetHAL().getWarmRebootTarget() < 0) {
        ESP_LOGI(TAG, "Create boot logo label");
        Lock();
        {
            uitk::lvgl_cpp::ScreenActive screen;
            screen.setBgColor(lv_color_hex(0x000000));
        }
        GetHAL().bootLogo = std::make_unique<BootLogo>();
        Unlock();
    }

    // Robot will be created later in SetupXiaoZhiUI()
}

StackChanAvatarDisplay::~StackChanAvatarDisplay()
{
    ESP_LOGI(TAG, "Destroying StackChanAvatarDisplay");

    if (preview_timer_ != nullptr) {
        esp_timer_stop(preview_timer_);
        esp_timer_delete(preview_timer_);
    }

    if (preview_image_ != nullptr) {
        lv_obj_del(preview_image_);
    }

    auto& stackchan = GetStackChan();
    if (stackchan.hasAvatar()) {
        stackchan.resetAvatar();
    }
}

bool StackChanAvatarDisplay::Lock(int timeout_ms)
{
    return lvgl_port_lock(timeout_ms);
}

void StackChanAvatarDisplay::Unlock()
{
    lvgl_port_unlock();
}

lv_disp_t* StackChanAvatarDisplay::GetLvglDisplay()
{
    return display_;
}

#include <hal/board/hal_bridge.h>

void StackChanAvatarDisplay::SetupUI()
{
    // Prevent duplicate calls - if already called, return early
    if (setup_ui_called_) {
        ESP_LOGW(TAG, "SetupUI() called multiple times, skipping duplicate call");
        return;
    }

    Display::SetupUI();  // Mark SetupUI as called

    auto& stackchan = GetStackChan();

    if (stackchan.hasAvatar()) {
        ESP_LOGW(TAG, "Avatar already created");
        return;
    }

    DisplayLockGuard lock(this);

    ESP_LOGI(TAG, "Creating Stack-chan Avatar...");

    // Tapping the face toggles the chat state, whichever skin is in use.
    auto attach_tap_to_talk = [](uitk::lvgl_cpp::Container* panel) {
        if (panel == nullptr) {
            ESP_LOGW(TAG, "Avatar panel is null, tap-to-talk disabled");
            return;
        }
        // Where the finger came down: a touch belongs to the spot it began in.
        static lv_point_t pressed_at{};
        lv_obj_add_event_cb(
            panel->get(),
            [](lv_event_t*) {
                if (lv_indev_t* indev = lv_indev_active()) {
                    lv_indev_get_point(indev, &pressed_at);
                }
            },
            LV_EVENT_PRESSED, nullptr);

        panel->onClick().connect([]() {
            // Spots on the face that react rather than talk, which the face declares (face.yaml: touch).
            // /api/log has every touch, for placing them.
            lv_point_t lifted{};
            if (lv_indev_t* indev = lv_indev_active()) {
                lv_indev_get_point(indev, &lifted);
            }
            const lv_point_t at = pressed_at;
            const int dx        = lifted.x - at.x;
            const int dy        = lifted.y - at.y;
            constexpr int kDragPixels = 18;   // less than this is a finger settling, not a drag
            const char* gesture = "tap";
            if (std::abs(dx) >= kDragPixels || std::abs(dy) >= kDragPixels) {
                gesture = std::abs(dx) >= std::abs(dy) ? (dx < 0 ? "left" : "right") : (dy < 0 ? "up" : "down");
            }
            const bool tap = std::strcmp(gesture, "tap") == 0;
            ESP_LOGI(TAG, "%s at %d,%d", gesture, (int)at.x, (int)at.y);
            if (auto* custom = dynamic_cast<CustomAvatar*>(&GetStackChan().avatar())) {
                const uint8_t none[3] = {0, 0, 0};
                const int spot        = custom->spotAt(at.x, at.y, gesture);
                if (spot >= 0) {
                    custom->react(custom->touchSpots()[spot].pose, custom->touchSpots()[spot].ms);
                    touch_log::add(GetHAL().millis(), none, touch_log::kSpotFirst + spot);
                    return;
                }
                // a face from before touch spots: the two the first sprite face had, until it declares its own
                if (custom->touchDeclared() || !tap) {
                } else if (at.x >= 135 && at.x <= 205 && at.y >= 105 && at.y <= 155) {
                    custom->react("surprised", 1500);
                    touch_log::add(GetHAL().millis(), none, touch_log::kBoopNose);
                    return;
                } else if (at.x >= 105 && at.x <= 215 && at.y >= 30 && at.y <= 72) {
                    custom->react("loving", 3000);
                    touch_log::add(GetHAL().millis(), none, touch_log::kKissForehead);
                    return;
                }
            }

            // a drag that isn't for a spot is nothing: only a tap talks
            if (!tap) {
                return;
            }

            static uint32_t last_toggle_tick = 0;
            const uint32_t now               = GetHAL().millis();
            if (last_toggle_tick != 0 && now - last_toggle_tick < 2000) {
                return;
            }

            if (hal_bridge::is_xiaozhi_ready()) {
                last_toggle_tick = now;
                hal_bridge::toggle_xiaozhi_chat_state();
            }
        });
    };

    // Prefer a sprite face from the assets partition (face.json + images).
    // Falls back to the built-in vector face when none is flashed or it fails
    // to load, so a bad or absent face can never stop the device from booting.
    // See firmware/docs/custom-faces.md
    std::unique_ptr<Avatar> avatar;

    auto custom_avatar = std::make_unique<CustomAvatar>();
    if (custom_avatar->init(lv_screen_active())) {
        ESP_LOGI(TAG, "Using custom face: %s", custom_avatar->faceName().c_str());
        attach_tap_to_talk(custom_avatar->getPanel());
        avatar = std::move(custom_avatar);
    } else {
        ESP_LOGI(TAG, "No usable custom face, using default avatar");
        custom_avatar.reset();

        auto default_avatar = std::make_unique<DefaultAvatar>();
        default_avatar->init(lv_screen_active());
        attach_tap_to_talk(default_avatar->getPanel());
        avatar = std::move(default_avatar);
    }

    stackchan.attachAvatar(std::move(avatar));
    stackchan.addModifier(std::make_unique<BreathModifier>());
    blink_modifier_id_ = stackchan.addModifier(std::make_unique<BlinkModifier>());
    stackchan.addModifier(std::make_unique<PoseMotionModifier>());
    stackchan.addModifier(std::make_unique<HeadPetModifier>());
    stackchan.addModifier(std::make_unique<ImuEventModifier>());

    preview_image_ = lv_image_create(lv_screen_active());
    lv_obj_set_size(preview_image_, 320, 240);
    lv_obj_align(preview_image_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);

    // GetHAL().startStackChanAutoUpdate(24);

    auto config        = hal_bridge::get_xiaozhi_config();
    idle_motion_level_ = config.idleRandomMovementLevel;

    ESP_LOGI(TAG, "Avatar created and started");
}

void StackChanAvatarDisplay::LvglLock()
{
    if (!Lock(30000)) {
        ESP_LOGE("Display", "Failed to lock display");
    }
}

void StackChanAvatarDisplay::LvglUnlock()
{
    Unlock();
}

// The status light, shown on both sides: LEDs 0-5 are one strip and 6-11 the other.
// Held, so a mood or a light show can't hide whether the mic is open. Black lets go.
static void set_status_light(uint8_t r, uint8_t g, uint8_t b)
{
    for (uint8_t led : {0, 6}) {
        if (r || g || b) {
            GetHAL().holdRgb(led, r, g, b);
        } else {
            GetHAL().releaseRgb(led);
        }
    }
    GetHAL().refreshRgb();
}

void StackChanAvatarDisplay::CreateIdleMotionModifier()
{
    auto& stackchan = GetStackChan();

    switch (idle_motion_level_) {
        case 0:
            idle_motion_modifier_id_ = -1;
            return;
        case 1:
            idle_motion_modifier_id_ = stackchan.addModifier(std::make_unique<IdleMotionModifier>(8000, 12000));
            return;
        case 3:
            idle_motion_modifier_id_ = stackchan.addModifier(std::make_unique<IdleMotionModifier>(2000, 4000));
            return;
        case 2:
        default:
            idle_motion_modifier_id_ = stackchan.addModifier(std::make_unique<IdleMotionModifier>());
            return;
    }
}

namespace {

// Xiaozhi's full emotion vocabulary, from
// xiaozhi-esp32/main/display/lvgl_display/emoji_collection.cc
// mapped onto the six poses the built-in vector face can express.
// A custom sprite face handles these strings directly and never reaches here.
Emotion map_emotion_string(const char* emotion)
{
    struct Entry {
        const char* name;
        Emotion pose;
    };

    static const Entry table[] = {
        {"neutral", Emotion::Neutral},      {"happy", Emotion::Happy},
        {"laughing", Emotion::Happy},       {"funny", Emotion::Happy},
        {"sad", Emotion::Sad},              {"angry", Emotion::Angry},
        {"crying", Emotion::Sad},           {"loving", Emotion::Happy},
        {"embarrassed", Emotion::Happy},    {"surprised", Emotion::Doubt},
        {"shocked", Emotion::Doubt},        {"thinking", Emotion::Doubt},
        {"winking", Emotion::Happy},        {"cool", Emotion::Neutral},
        {"relaxed", Emotion::Neutral},      {"delicious", Emotion::Happy},
        {"kissy", Emotion::Happy},          {"confident", Emotion::Neutral},
        {"sleepy", Emotion::Sleepy},        {"silly", Emotion::Happy},
        {"confused", Emotion::Doubt},
    };

    for (const auto& entry : table) {
        if (strcmp(emotion, entry.name) == 0) {
            return entry.pose;
        }
    }

    ESP_LOGW(TAG, "Unknown emotion: %s, using NEUTRAL", emotion);
    return Emotion::Neutral;
}

}  // namespace

void StackChanAvatarDisplay::SetEmotion(const char* emotion)
{
    auto& stackchan = GetStackChan();

    if (!stackchan.hasAvatar() || !emotion) {
        return;
    }

    DisplayLockGuard lock(this);

    auto& avatar = stackchan.avatar();

    if (strcmp(emotion, "neutral") == 0) {
        if (auto* custom = dynamic_cast<CustomAvatar*>(&avatar); custom && !custom->resting().empty()) {
            emotion = custom->resting().c_str();
        }
    }

    // A sprite face describes each emotion in its manifest, so give it the raw
    // protocol string and let it pick lids, mouth shape, brows and decorator.
    // Anything it does not describe falls through to the six-pose mapping.
    bool handled = false;
    if (auto* custom = dynamic_cast<CustomAvatar*>(&avatar)) {
        handled = custom->setEmotionByName(emotion);
    }

    if (!handled) {
        avatar.setEmotion(map_emotion_string(emotion));
    }

    // Sleep is more than a pose: it parks the servos and stops idle movement.
    if (strcmp(emotion, "sleepy") == 0) {
        avatar.setSpeech("Zzz…");
        is_sleeping_ = true;

        // Stop idle motion
        ESP_LOGW(TAG, "Stop idle motion");
        if (idle_motion_modifier_id_ >= 0) {
            stackchan.removeModifier(idle_motion_modifier_id_);
            idle_motion_modifier_id_ = -1;
            stackchan.removeModifier(idle_expression_modifier_id_);
            idle_expression_modifier_id_ = -1;
        }

        // Return to default pose
        auto& motion = GetStackChan().motion();
        motion.pitchServo().moveWithSpeed(0, 80);
    }

    // Resync blink modifier base eye weights
    auto blink_modifier = static_cast<BlinkModifier*>(stackchan.getModifier(blink_modifier_id_));
    if (blink_modifier) {
        blink_modifier->resyncEyeWeights();
    }
}

void StackChanAvatarDisplay::SetChatMessage(const char* role, const char* content)
{
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "SetChatMessage('%s', '%s') called before SetupUI() - message will be lost!", role, content);
    }

    auto& stackchan = GetStackChan();
    if (!stackchan.hasAvatar()) {
        return;
    }

    // ESP_LOGE(TAG, "SetChatMessage: role=%s, content=%s", role ? role : "null", content ? content : "null");

    DisplayLockGuard lock(this);

    if (strcmp(role, "system") == 0) {
        stackchan.avatar().setSpeech(content);
    } else if (strcmp(role, "assistant") == 0) {
        stackchan.avatar().setSpeech(content);
    }
}

void StackChanAvatarDisplay::ClearChatMessages()
{
    auto& stackchan = GetStackChan();
    if (!stackchan.hasAvatar()) {
        return;
    }

    DisplayLockGuard lock(this);

    stackchan.avatar().clearSpeech();

    ESP_LOGI(TAG, "Chat messages cleared");
}

void StackChanAvatarDisplay::SetPreviewImage(std::unique_ptr<LvglImage> image)
{
    DisplayLockGuard lock(this);
    if (preview_image_ == nullptr) {
        return;
    }

    if (image == nullptr) {
        esp_timer_stop(preview_timer_);
        lv_obj_add_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);
        preview_image_cached_.reset();
        return;
    }

    preview_image_cached_ = std::move(image);
    auto img_dsc          = preview_image_cached_->image_dsc();
    // Set image source and show preview image
    lv_image_set_src(preview_image_, img_dsc);
    if (img_dsc->header.w > 0 && img_dsc->header.h > 0) {
        // Scale to fit width
        lv_image_set_scale(preview_image_, 256 * width_ / img_dsc->header.w);
    }

    lv_obj_remove_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(preview_image_);
    esp_timer_stop(preview_timer_);
    ESP_ERROR_CHECK(esp_timer_start_once(preview_timer_, 6000 * 1000));
}

void StackChanAvatarDisplay::UpdateStatusBar(bool update_all)
{
}

void StackChanAvatarDisplay::SetTheme(Theme* theme)
{
    ESP_LOGI(TAG, "SetTheme: %s", theme->name().c_str());

    auto& stackchan = GetStackChan();
    if (!stackchan.hasAvatar()) {
        ESP_LOGE(TAG, "Avatar is invalid");
        return;
    }

    DisplayLockGuard lock(this);

    auto lvgl_theme = static_cast<LvglTheme*>(theme);
    auto text_font  = lvgl_theme->text_font()->font();

    stackchan.avatar().setSpeechTextFont((void*)text_font);
}

#include <hal/board/hal_bridge.h>
static bool _is_xiaozhi_ready = false;
static bool _is_xiaozhi_idle  = false;
bool hal_bridge::is_xiaozhi_ready()
{
    return _is_xiaozhi_ready;
}
bool hal_bridge::is_xiaozhi_idle()
{
    return _is_xiaozhi_idle;
}

void StackChanAvatarDisplay::SetStatus(const char* status)
{
    // ESP_LOGE(TAG, "SetStatus: %s", status);

    auto& stackchan = GetStackChan();
    if (!stackchan.hasAvatar()) {
        ESP_LOGE(TAG, "Avatar is invalid");
        return;
    }

    auto& avatar = stackchan.avatar();
    auto& motion = stackchan.motion();

    DisplayLockGuard lock(this);

    bool is_idle      = false;
    bool is_listening = false;

    // The mic art shows for as long as the mic is open, over whatever face is up. A face that
    // hasn't drawn "microphone" just doesn't show it; the green light still does.
    static int mic_decorator_id = -1;
    const bool listening        = strcmp(status, Lang::Strings::LISTENING) == 0;
    if (listening && mic_decorator_id < 0) {
        mic_decorator_id = avatar.addNamedDecorator("microphone", 0);
    } else if (!listening && mic_decorator_id >= 0) {
        avatar.removeDecorator(mic_decorator_id);
        mic_decorator_id = -1;
    }

    if (auto* custom = dynamic_cast<avatar::CustomAvatar*>(&avatar)) {
        custom->setTalking(strcmp(status, Lang::Strings::SPEAKING) == 0);
    }

    if (strcmp(status, Lang::Strings::LISTENING) == 0) {
        if (speaking_modifier_id_ >= 0) {
            // Start speaking
            stackchan.removeModifier(speaking_modifier_id_);
            avatar.mouth().setWeight(0);
            speaking_modifier_id_ = -1;
        }

        set_status_light(0, 50, 0);

    } else if (strcmp(status, Lang::Strings::STANDBY) == 0) {
        _is_xiaozhi_ready = true;

        if (speaking_modifier_id_ >= 0) {
            // Stop speaking
            stackchan.removeModifier(speaking_modifier_id_);
            avatar.mouth().setWeight(0);
            speaking_modifier_id_ = -1;
        }

        is_idle = true;

        set_status_light(0, 0, 0);

    } else if (strcmp(status, Lang::Strings::SPEAKING) == 0) {
        if (speaking_modifier_id_ < 0) {
            speaking_modifier_id_ = stackchan.addModifier(std::make_unique<SpeakingModifier>(0, 180, false));
        }

        set_status_light(0, 0, 50);
    } else {
        avatar.setSpeech(status);
    }

    if (is_idle) {
        // Start idle motion
        ESP_LOGW(TAG, "Start idle motion");
        if (idle_motion_modifier_id_ < 0) {
            if (idle_motion_level_ > 0) {
                CreateIdleMotionModifier();
            }
            idle_expression_modifier_id_ = stackchan.addModifier(std::make_unique<IdleExpressionModifier>());
        }

        _is_xiaozhi_idle = true;
    } else {
        // Stop idle motion
        ESP_LOGW(TAG, "Stop idle motion");
        if (idle_motion_modifier_id_ >= 0) {
            stackchan.removeModifier(idle_motion_modifier_id_);
            idle_motion_modifier_id_ = -1;
            stackchan.removeModifier(idle_expression_modifier_id_);
            idle_expression_modifier_id_ = -1;
        }

        // if (!is_listening) {
        //     // Return to default pose
        //     motion.pitchServo().moveWithSpeed(200, 350);
        //     motion.yawServo().moveWithSpeed(0, 350);
        // }

        _is_xiaozhi_idle = false;
    }

    // Clear sleep state
    if (is_sleeping_) {
        avatar.setSpeech("");
    }
}

void StackChanAvatarDisplay::ShowNotification(const char* notification, int duration_ms)
{
}
