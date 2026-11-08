# SerenSoma

**The body.** A Stack-chan desktop robot with Seren inside: the firmware that
runs it, and the voice server that is its nervous system. Soma is the part
that has to keep working when nothing is watching it. Watching it, and making
its faces, is [SerenPsycheion](https://github.com/ChadRoesler/SerenPsycheion)'s
job; what passes between them is spelled once in
[SerenSinew](https://github.com/ChadRoesler/SerenSinew)'s `seren_sinew.soma`.

It's built for whoever puts a mind behind it: your own local model, or one you
call out to. Whichever it is, you should know exactly what this body senses,
what it keeps, and where it sends things. That comes first.

## What it senses, what it keeps, and where it goes

| | When it's on | What's kept | Where it goes |
|---|---|---|---|
| **Microphone** | Only after a tap on the screen, or when the mind calls you. There's no wake word: nothing listens for one. A call the mind starts is deaf, and hangs up when it's done speaking, unless you were talking to it in the last three minutes; then it stays open a few seconds for your answer. Any open channel hangs up after a quiet minute. | Each thing you say, as a WAV under the state folder's `heard/` and its transcript in `heard.jsonl`. Clips with no words in them are deleted. | Transcribed by the speech service your Lodestar points at (`/api/v1/hear`; Whisper on your own box in the reference setup). |
| **Camera** | Only when asked for one picture over the control API. Nothing takes a photo on its own, and the console doesn't have a camera button. | Nothing on the robot. The JPEG goes to whoever asked. | Only to that caller. |
| **Head touch** (three zones on top) and **screen touch** | Always: they make the face react. | Each touch, what it was read as and which face it brought up, in the state folder's `touch.jsonl`. | Nowhere off the box. |
| **Speaker** | When the mind speaks. | Saved sound clips under `state/clips/`, if any. | Speech is rendered by the service your Lodestar points at (`/api/v1/speak`). |

**The lights tell the truth, and nothing can make them lie.** Green on the
rails means the mic is open. Blue means it's speaking. Red means the camera
sensor is on, lit *before* the sensor starts and put out *after* it stops.
These are held above everything else on the rails: a light show, a mood
color, an API call, none of them can overwrite them. The master dimmer and
night mode can make them dimmer, but never off.

All of it lives in the voice server's state folder, `~/seren-soma/state/` by default, outside this repo. Its page shows the log with what was heard replaced by a word count, so you can send it to someone helping you without sending what you said.

## What's here

| | |
|---|---|
| [`firmware/`](firmware/) | The robot's firmware, ESP-IDF 6.1 on the CoreS3. Sprite faces, the token-guarded control API, the light rails, the head sensor, sending a face over Wi-Fi. [`firmware/docs/custom-faces.md`](firmware/docs/custom-faces.md) is the face format. |
| [`SerenSoma/`](SerenSoma/) | `seren-soma`, the nervous system, as a Python package. Speaks the robot's voice protocol, hears through the speech service, reads the head sensor into strokes, pokes, scritches and resting hands, and turns them into faces. Owns night mode and work hours. |
| `firmware/tools/` | `face.py` (build a face from PNGs) and Face Studio, until they move to SerenPsycheion. |

A mind drives the body through [SerenWorkbench](https://github.com/ChadRoesler/SerenWorkbench)'s
built-in body tools (`soma_status`, `soma_say`, `soma_pose`...), which can be
switched off for a Workbench with no body. Watching it and making its faces is
[SerenPsycheion](https://github.com/ChadRoesler/SerenPsycheion).

## Setting up a robot

The firmware carries no secrets, so a release build is the same file for every robot. What makes
yours *yours* goes on its microSD card as one file, `soma.yaml`:

```yaml
wifi:
  ssid: MyNetwork
  password: "hunter2"
voice: 192.0.2.10        # the box running seren-soma
token: "long-and-random"    # what the control API asks for
```

The robot reads it once at boot, saves it, and **deletes it from the card**, so a password never
rides around on a card. Every key is optional, an empty one forgets what was saved, and
`GET /api/status` says where each came from (`"saved"` or `"build"`) without saying what it is.
A robot that's already on the network takes the same keys over `POST /api/config`, which is what
seren-soma's setup page uses.
[`firmware/soma.yaml.sample`](firmware/soma.yaml.sample) has all of it. (`wifi.txt`, SSID on the
first line and password on the second, still works for just the Wi-Fi.)

## The control API (firmware, port 8080)

Every call needs `X-StackChan-Token`, set in `soma.yaml` on the robot's SD card
(see [Setting up a robot](#setting-up-a-robot)), or at build time in
`firmware/sdkconfig.defaults.local` (git-ignored). A robot with no token takes
calls from anything on the network, and says so in its boot log.

| | |
|---|---|
| `GET /api/status` | Uptime, battery, heap, the face, what each LED shows, the settings. |
| `POST /api/config` | The `soma.yaml` keys over the network (Wi-Fi, voice server, token, tailnet), saved, never echoed back. `reboot: true` to apply them. |
| `POST /api/settings` | Read, or save, the master light dimmer, speaker volume and screen brightness (all 0 to 100; saved on the robot). `night` puts a cap over them that is never saved. |
| `POST /api/emotion`, `/api/react` | A pose, or a pose for a moment. `rest: true` makes a pose the one the robot goes back to. |
| `POST /api/leds`, `/api/led` | Light the rails, per LED or all at once. |
| `GET /api/touch-raw`, `/api/touch-spots` | The head sensor's readings; the face's touch spots and head touches. |
| `POST /api/face`, `/api/face-restore`, `/api/reboot` | Send a face (it goes in on the next boot), or swap back to the one before it. |
| `GET /api/photo` | One picture, red light first. See above. |

## The voice server, `seren-soma` (port 7480)

```
pip install seren-soma
seren-soma                  # reads ~/seren-soma/seren-soma.yaml
seren-soma --describe       # one line of JSON, for installers
```

It belongs on an always-on box on the robot's network: night mode, touch reactions and the mind's
voice all live in it. The robot finds it through `voice:` in `soma.yaml`, so moving it to another
box is one line on the SD card and a reboot, no flashing. [`SerenSoma/seren-soma.yaml.sample`](SerenSoma/seren-soma.yaml.sample) has
every setting.

**Two tokens.** The API token, `token` in the state folder (made the first time, or
`server.bearer_token_*`), guards everything below and never leaves the box. The robot gets its own
token for its voice connection, and the check-in only answers the robot's own address.

| | |
|---|---|
| `GET /viewer` | Is it alive, and its log, for sending to whoever's helping (no token to open the page; the 🔑 asks for it). |
| `POST /say` | Speak through the robot (`wake` opens the channel to do it). |
| `GET`/`POST /settings` | Night (hours and caps for lights, speaker and screen), work hours, and how long unbroken scratching takes to tip the face over. |
| `GET /touch` | The live touch feed, for a console. |
| `GET /api/logs` | The end of its log, what was heard replaced by a word count. |
| `POST /robot/config` | Set the robot up over the network: Wi-Fi, voice server (blank: this box), tailnet, `rotate_token` for a new token. The robot takes a new token first, then this box writes it down, then the robot reboots onto it. |
| `POST /robot/soma.yaml` | The same, as a `soma.yaml` for the SD card of a robot that isn't on the network yet, with this box's blanks filled in. |
| `GET /firmware`, `POST /firmware/fetch`, `PUT /firmware/upload`, `POST /firmware/install` | Firmware for the robot: fetch a `fw-v*` release (checked against its SHA256SUMS) or stage a build of your own, then install it. The robot is offered it once on its next check-in, downloads it from here (only the robot's address can), and reboots into it; one that can't check in afterwards rolls back. |

**Night mode** lives here, not in a console, so the bedroom stays dim with nothing else running. It's
off until you turn it on.

## Built on Stack-chan

Soma is a fork of [M5Stack's open-source Stack-chan](https://github.com/m5stack/StackChan),
the kawaii AI desktop robot M5Stack co-created with its community. The
hardware is theirs: an [M5Stack CoreS3](https://docs.m5stack.com/en/core/CoreS3)
(ESP32-S3, 240 MHz dual core, 16 MB flash, 8 MB PSRAM, a 2-inch capacitive
touch screen, a 0.3 MP camera, a 9-axis IMU, a 1 W speaker and dual
microphones) on a body with two feedback servos, 12 RGB LEDs in two rails, a
three-zone touch panel, IR and NFC.

- Buy one: [M5Stack](https://shop.m5stack.com/products/stackchan-kawaii-co-created-open-source-ai-desktop-robot)
- Their docs: [English](https://docs.m5stack.com/en/StackChan) | [日本語](https://docs.m5stack.com/ja/StackChan) | [中文](https://docs.m5stack.com/zh_CN/StackChan)
- Their board support package: [StackChan-BSP](https://github.com/m5stack/StackChan-BSP)

> ⚠️ Don't turn the moving parts by hand unless you know the servos are
> unpowered. Forcing them can damage the hardware.

Stack-chan's community made it what it is; thanks especially to
[@stack_chan](https://x.com/stack_chan) (Shinya Ishikawa) and
[@mongonta555](https://x.com/mongonta555) (Takao Akaki).

## License

Two licenses, split by folder:

- **`firmware/`** stays under the MIT License, as Stack-chan is: © 2026 M5Stack
  Technology CO LTD, and Soma's changes to it are MIT too, so a fix can go
  back upstream ([`firmware/LICENSE`](firmware/LICENSE)). Its voice stack is
  [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32), MIT, © 2025 Shenzhen
  Xinzhi Future Technology Co., Ltd.
  ([`firmware/xiaozhi-esp32/LICENSE`](firmware/xiaozhi-esp32/LICENSE)).
- **Everything else** (the voice server and the rest of Seren's own code) is
  AGPL-3.0-or-later, like the rest of the Seren family ([`LICENSE`](LICENSE)).
