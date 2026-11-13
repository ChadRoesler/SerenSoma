# The bench: charting the touch pads and testing the neck

Three tabs on the voice server's page (`/viewer`), **Touch**, **Servos** and **Lights**, over five routes on the
robot's control API. They show the hardware as it is, for working out where the pads are and whether
the neck is running true. Apart from a servo's zero, nothing they change is saved on the robot; a reboot puts the rest back.

The page talks to the voice server's `/bench/<name>` routes, which pass `touch-probe`, `touch-raw`,
`screen-touch`, `servo`, `status`, `head` and `leds` through to the robot with the robot's token. The page never holds that
token, and nothing else on the robot is reachable that way.

## How a touch gets from the pad to the face

1. **The chip.** An Si12T on the I2C bus at `0x68`. It has 12 channels and reports each as 0 to 3
   (none, low, mid, high) in three output registers, `0x10` to `0x12`, two bits a channel, channel 1
   in the low bits of the first. The number is how sure the chip is that something is there. It is
   not pressure: a light brush reads 3.
2. **What the firmware reads.** `hal_head_touch.cpp` reads the first output register every 50 ms and
   takes channels 1 to 3 from it as back, middle and front. Those are the only pads there are (see
   "What the board has", below). At boot every channel is enabled and the sensitivity is set to
   type low, level 3, on five of the six sensitivity registers (`0x02` to `0x06`; `0x07`, channels
   11 and 12, is left as it powers up, which reads back as `0xBB`).
3. **One position.** The three strengths become a position from -100 (back) to +100 (front):
   `(front - back) * 100 / (back + middle + front)`.
4. **Gestures on the robot.** A touch starting is a press, lifting is a release, and the position
   moving 40 or more from where the touch started is a swipe forward or backward. These drive the
   robot's own reactions (the happy face and the nod when stroked).
5. **The feed.** Readings taken while something is touching, and the one after it lifts, are kept
   in PSRAM, the last 2048 of them. `GET /api/touch-raw?since=<ms>` returns them
   as lines of `ms zone0 zone1 zone2 gesture`. Taps on the screen (nose, forehead, a face's touch
   spots) join the same feed as gestures 10, 11 and 100 up.
6. **Naming the touch.** The voice server reads the feed once a second and `touch_read.py` names
   each touch: stroke, poke (back, middle or front), scritch, a resting finger or a whole hand. It
   then asks the robot for the face that goes with it (`POST /api/react`).

## What the board has

From the head board's schematic (SCH_Touch) and from the robot itself, 12 Nov 2026.

- **Three touch pads**, in a line down the middle of the top of the head, back to front, with dead
  space either side. They are the Si12T's channels 1, 2 and 3, each through a 680 ohm resistor.
  Channels 5 to 9 are tied to ground and 10 to 12 are not connected, so no other channel can read
  anything. On the robot, only 1 to 3 ever have.
- **A touch interrupt line** from the chip to the head connector (`Touch_IRQ`). The firmware does
  not use it; it asks the chip every 50 ms, so a touch that begins and ends between two askings is
  never seen. The interrupt is the way to catch those, if it's ever wanted.
- **Twelve LEDs in one chain** on one data line, two rows of six, the end of the first row feeding
  the start of the second. So 0 to 5 run one way along the head and 6 to 11 come back the other:
  on this robot 0 and 11 are at the front, 5 and 6 at the back.
- **An NFC reader** (ST25R3916, with an antenna, on the I2C bus at `0x50`) and **an infrared
  receiver** (IRM-56384, on `IR_REC`). The firmware has no code for either.

## Touch

### `GET /api/touch-probe`

The chip right now, all of it:

```json
{"ok": true, "uptimeMs": 81234,
 "channels": [3, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
 "sensitivity": {"type": "low", "level": 3, "registers": [51, 51, 51, 51, 51, 0]},
 "ctrl1": 34, "ctrl2": 7, "hold": [0, 0]}
```

- `channels`: all 12, channel 1 first, each 0 to 3.
- `sensitivity.type` and `level` are what was last asked for; `registers` are `0x02` to `0x07` as the
  chip holds them, so a write that didn't take shows.
- `hold` is the two channel-hold registers: a set bit is a channel switched off.

503 if the chip didn't answer (the head is off, or the ribbon is).

### `POST /api/touch-probe`

`{"type": "low" | "high", "level": 0..7}` changes the sensitivity until the next boot, then answers
as GET does. Which direction is more sensitive is for the chart to show: record the same spots at
two settings and compare.

### Charting the pads

On the Touch tab, click the drawing where your finger is going to be, put your finger there, and
record. For the seconds you hold it the page keeps the robot's own readings from `/api/touch-raw`
(one every 50 ms while something touches) and stores, for that point, the share of the time each
pad was on and the strongest it read. The drawing is the head from above with the face toward you.
It is only somewhere to put points, so its edges can stand for the sides of the head.

The live view on the same tab has two sources, and the difference matters for quick touches. The
three cells are the chip asked right now, about seven times a second, so a tap shorter than the gap
between two askings is not in them. The strip underneath is drawn from `/api/touch-raw`, the
robot's own record at 20 readings a second, so anything the robot reacted to is on the strip, and
lights its cell for a moment when it arrives. Until 12 Nov 2026 the strip was drawn from the cells'
askings and quick taps the robot had plainly felt were missing from it.

The chart is kept by the voice server in `<state>/touch_map.json`, and `GET /bench/map` returns it
with a summary: for each channel that answered anywhere, how many points and the middle of them on
the drawing. A point's `share` and `peak` are lists of 12, one per channel of the chip; only the
first three, the pads, are ever anything but 0.

A point records the sensitivity it was taken at, so one chart can hold the same spot at several
settings.

## Servos

### `GET /api/servo`

```json
{"ok": true, "uptimeMs": 81234, "locked": false,
 "yaw":   {"ok": true, "angle": 0.0, "angleMin": -128, "angleMax": 128, "autoRelease": true,
           "raw": 460, "zero": 460, "rawMin": 0, "rawMax": 1000, "rawAngle": 0.0,
           "speed": 0, "load": 0, "current": 0, "voltage": 5.1, "temperature": 31,
           "moving": false, "torque": false},
 "pitch": {"...": "the same"}}
```

- `angle` is where the firmware takes the servo to be, in degrees, held to `angleMin`..`angleMax`.
- `raw` is the servo's own position in steps, 0.3125 degrees each. `zero` is the step that counts as
  0 degrees: the calibrated one, or the default (460 for yaw, 620 for pitch).
- `rawAngle` is `raw` turned into degrees and not held to the limits. With the torque off, turn the
  head by hand and this reads where it really is.
- `servoMin`, `servoMax` are the limits the servo keeps in its own memory, in steps; -1 if it didn't
  say. A position past them is one the servo itself won't go to, whatever the firmware asks.
- `rawMin`, `rawMax` are the raw limits in force. Stall protection on the pitch servo pulls them in
  when it meets something, and they stay pulled in until the next boot or calibration.
- `load` is how much of its drive the servo is putting out, in tenths of a percent, signed.
  `current` is in the servo's own units. `voltage` is volts, `temperature` degrees C.
- `autoRelease`: the firmware lets go of a servo that has come to rest. `locked`: something holds
  the motion lock, and the idle motion and the nod when petted stand aside.
- A servo that didn't answer has `"ok": false` and only `angle`, the angle limits and `autoRelease`.

### `POST /api/servo`

Any of these, then answers as GET does:

| | |
|---|---|
| `"lock": true` | Nothing else moves the head: no idle looking-around, no nod when petted. Picking the robot up clears it, because the IMU uses the same lock. |
| `"hold": true` | Torque stays on at rest, instead of being let go a moment after the servo stops. |
| `"torque": false` | Let go now. Send `"hold": false` with it, or a held servo is only let go until its next move. |

One thing is saved, in the robot's settings, and survives a reboot:

| | |
|---|---|
| `"servo": "yaw" \| "pitch", "zero": "here"` | Where that servo is now becomes its 0 degrees. The same home position the setup menu sets, one servo at a time. |
| `"servo": "yaw" \| "pitch", "zero": "default"` | Back to the build's default zero (460 for yaw, 620 for pitch). |

### Why it might not look all the way up

Pitch 0 is level and the top is up. Three things each cap how far up it goes, and the page's **Reach**
column works out which one is winning:

- **The firmware's limit is 87 degrees, not 90.** `/api/head` takes pitch 0 to 90, but the servo's
  angle limit is 3 to 87, so 90 has always meant 87.
- **The step range.** 87 degrees is 278 steps above the zero. With the default zero of 620 that is
  step 898, inside 0 to 1000. A zero calibrated above step 722 puts the top past 1000, where it is cut
  off. The same goes for the servo's own limits, if they are tighter.
- **A stall.** The pitch servo's stall protection pulls the upper limit in to wherever it met
  resistance, and keeps it there until the next boot or calibration. `rawMax` below 1000 says it has.

Moving is the route that was already there: `POST /api/head {"yaw": -128..128, "pitch": 0..90, "speed": 100..1000}`.

### The sweep

The Servos tab steps one axis from one angle to another and back, waits at each step for the servo
to report it has stopped, and keeps the target, `rawAngle`, `load` and `current`. Reaching the same
angle from both sides shows the slack in the neck: the gap between the two bars at one angle. A step
that lands short with the load up is the neck meeting something. It turns `lock` and `hold` on for
the run and puts `hold` back after. The rows download as CSV.

## The screen

### `GET /api/screen-touch`

```json
{"ok": true, "uptimeMs": 81234, "width": 320, "height": 240, "down": true, "x": 171, "y": 130,
 "touches": [{"ms": 60210, "x": 170, "y": 130, "x1": 171, "y1": 131, "gesture": "tap"}]}
```

- `down`, `x`, `y`: the finger on the screen right now, as the touch driver reports it, in the
  screen's own pixels with 0,0 at the top left as you face it. `x` and `y` are only there while a
  finger is down. This follows a finger anywhere on the screen, whatever is showing.
- `touches`: the last 16 touches the face saw, oldest first: where each came down, where it lifted,
  and what it was read as (`tap`, or `left`, `right`, `up`, `down` for a drag of 18 pixels or more).
  Only touches on the face are kept; the launcher and the setup menu don't add to it.

The Touch tab draws both over a picture of the screen, with a trail behind the live finger. To check
the screen, put a fingertip on something visible on the face and see whether the ring lands on it.
This firmware has no touch calibration to set, so a ring that lands off is a
finding about the panel or its mounting, and what can be done about it is move a face's touch spots.

## The lights

Nothing new on the robot: `POST /api/leds` already took one LED (`{"index": 3, "r": 0, "g": 50,
"b": 0}`) or a whole frame (`{"frame": [[r, g, b], ...]}` from LED 0 up), and `GET /api/status`
already reported what each was last told to show (`leds`, twelve `rrggbb` from LED 0). A value stops
at 168.

The firmware counts 12 LEDs: 0 to 5 on one rail, 6 to 11 on the other. LEDs 0, 1, 6 and 7 are the
robot's own (the microphone, reply and camera lights); a face's colours fade LEDs 2 to 5 and 8 to 11.
The Lights tab lights one at a time, or walks all twelve, so the number can be matched to where each
LED sits. The label written under each is kept with the touch chart (`leds` in `touch_map.json`, and
in `GET /bench/map`), which is where anything animating the rails LED by LED should read it from.

A pose with colours of its own takes LEDs 2 to 5 and 8 to 11 back when it's next shown.

## A shake

The motion sensor's shake used to hide both eyes and draw the face's `dizzy` decorator (or the
built-in spirals) with a blush, wobble the mouth, and send the head home for four seconds. A sprite
face's own dizzy pose, with its eyes, was never shown.

Now, on a sprite face that draws a pose named `dizzy`, a shake shows that pose whole for the four
seconds: its eyes, decorators, roll, mouth and lights. A face can name a different pose in
`face.yaml` with `head: {shake: {pose: <name>}}`. Being shaken again while it's up keeps it up. The
head still goes home and stays put for the duration. A face with no such pose gets the old reaction.
A held pose (`"hold": true` on `/api/emotion`) is not replaced by a shake.
