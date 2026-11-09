# Custom Faces

Give your StackChan a real face — your own artwork, animated by the firmware
you already have.

You draw parts, not expressions. A handful of eyelid shapes and mouth curves
compose into all 21 emotions the assistant can express. You preview on your
laptop in a second, and you never write C++.

---

## Table of contents

- [How the face is built](#how-the-face-is-built)
- [Quick start](#quick-start)
- [The one rule that trips everyone up](#the-one-rule-that-trips-everyone-up)
- [Drawing the art](#drawing-the-art)
- [Previewing](#previewing)
- [face.yaml reference](#faceyaml-reference)
- [Emotions](#emotions)
- [Building and flashing](#building-and-flashing)
- [Troubleshooting](#troubleshooting)
- [Under the hood](#under-the-hood)

---

## How the face is built

Layers, composed at runtime. That is what lets it animate.

```
        ┌──────────────────────────────────┐
        │  speech bubble                   │
        │        ╭───╮        ╭───╮        │  ← brows: offset + tilt per emotion
        │     ┌─────────┐  ┌─────────┐     │
        │     │▓▓▓▓▓▓▓▓▓│  │▓▓▓▓▓▓▓▓▓│     │  ← upper lid: shape per emotion ,slides down to blink
        │     │    ●    │  │    ●    │     │  ← iris on sclera, iris MOVES for gaze   
        │     │_________│  │_________│     │  
        │     └─────────┘  └─────────┘     │  ← lower lid
        │             ╰──────╯             │  ← mouth: shape per emotion,
        │           base.png               │     frames for lip sync
        └──────────────────────────────────┘
```

Each eye is four layers inside a **socket** — a container that clips whatever
strays outside it. That clipping is what keeps the iris inside the eye and lets
the upper lid slide out of view when the eye opens.

Everything is driven by the animation system that already ships:

| Behaviour | Source | Drives |
|---|---|---|
| Random blinking | `modifiers/blink.h` | upper lid position |
| Lip sync while speaking | `modifiers/speaking.h` | mouth frame, every 180 ms |
| Idle breathing drift | `modifiers/breath.h` | whole-eye and mouth position |
| Random glances | `modifiers/idle_expression.h` | iris position |
| Reacting to being picked up | `modifiers/imu.h` | visibility, mouth |
| Emotion from the assistant | `hal/board/stackchan_display.cc` | lids, mouth shape, brows, decorator |

**If no custom face is installed, the stock face is used.** A missing or broken
face can never stop the device from booting.

---

## Quick start

```bash
pip install PyYAML Pillow pypng lz4

python firmware/tools/face.py init my_face      # writes a starter face.yaml
# ... draw the PNGs it lists ...
python firmware/tools/face.py preview my_face   # contact sheet of all 21 emotions
python firmware/tools/face.py build my_face     # convert + install
cd firmware && idf.py build && idf.py flash
```

The loop you will actually live in is **draw → `preview` → draw**. It takes about
a second and needs no hardware. Only run `build` when you are happy.

---

## The one rule that trips everyone up

> **The upper lid is an occluder, not a decoration.**

It must be **opaque from its top edge down to a shaped bottom edge**, at the full
height of the socket. The firmware opens the eye by sliding it *up and out of
view*, and closes the eye by sliding it *down over the sclera*.

```
  eyeOpen: 100          eyeOpen: 50           eyeOpen: 0
  ┌─────────┐           ┌─────────┐           ┌─────────┐
  │         │           │▓▓▓▓▓▓▓▓▓│           │▓▓▓▓▓▓▓▓▓│
  │   ●     │           │─────────│           │▓▓▓▓▓▓▓▓▓│
  │         │           │   ●     │           │▓▓▓▓▓▓▓▓▓│
  └─────────┘           └─────────┘           └─────────┘
  lid parked above      lid halfway down      lid covering
```

If you draw the lid as a thin lash line floating on a transparent canvas, the
eye will look **wide open at every value**, including `sleepy`. This is the most
common mistake and it looks like a firmware bug. It isn't.

The **shape variants differ only in the bottom contour** — straight for `open`,
bulging for `tight`, sloping one way for `angle_up` (sad), the other for
`angle_down` (angry). The top is always flush and opaque.

The lower lid is the opposite: a small sprite anchored to the bottom of the
socket that never moves. `raised` versus `neutral` is what sells a squint.

---

## Drawing the art

### The screen is small

The panel is **320 × 240** and your face gets maybe 200 × 200 of it. Lines thinner
than 2 px disappear. Subtle shading turns to mud. Fine hair detail becomes noise.

**Draw at final size, at 100% zoom.** Do not draw big and scale down. Bold shapes,
high contrast, thick strokes — closer to a sticker than a portrait. Run `preview`
constantly; the contact sheet is rendered at 1:1 precisely so you can judge this.

### Coordinates are measured from the centre

```
              -120        0        +120
        -160 ───┼─────────┼─────────┼───
           0 ───┼───────(0,0)───────┼───   ← centre of the screen
        +160 ───┼─────────┼─────────┼───
```

`right_pos: [52, -18]` is 52 px right and 18 px **up** from centre. Y increases
downward. Set your artboard to 320 × 240 with a centred origin and read positions
straight off the canvas.

### Transparency

Everything except `base.png` must be exported **RGBA with real transparency**. A
white background paints an opaque box over your face.

`base.png` should be fully opaque at exactly 320 × 240 — it is the backdrop, so
it needs no alpha, which also makes it the cheapest layer to draw.

### Mirroring

With `mirror_eyes: true` you draw the **right** eye only. The build flips it for
the left side. This happens at conversion time because LVGL 9 cannot flip an
image at runtime — `lv_image_set_scale_x` is unsigned. Flash is cheap here, so a
mirrored face costs a few extra KB and zero CPU.

### Mouth frames are aperture, not width

A shape's frame list is **how far the jaw drops** — `.` `o` `O` — not how wide the
mouth stretches. Lip sync flips it open and shut every 180 ms, and a sideways
stretch at that rate does not read as talking.

`SpeakingModifier` picks a random weight each flip, which the rig maps to a frame:

| Weight | Frame (of 3) | Your art |
|---|---|---|
| 0–20 (shut) | 0 | closed line |
| 40–74 (open) | 1 | mid |
| 75–80 (open) | 2 | wide |

**Lip sync never exceeds weight 80**, so with 3 frames all three are used and a
5th would be unreachable. Three is the sweet spot; four is the most that earns
its flash.

Width and curve live on the **shape** axis instead — `smile` frame 0 is a closed
smile, `smile` frame 2 is an open laugh. Emotion picks the shape, speech picks
the aperture within it.

### Asset budget

A complete face is roughly **25 source PNGs** and lands around **460 KB** — about
11% of the 4 MB assets partition. You have plenty of room.

---

## Previewing

```bash
# contact sheet: all 21 emotions at 1:1
python tools/face.py preview my_face

# one emotion, pixel-doubled
python tools/face.py preview my_face --emotion loving --scale 3

# animated GIFs
python tools/face.py preview my_face --animate blink
python tools/face.py preview my_face --animate talk
python tools/face.py preview my_face --animate gaze
```

The previewer composites your PNGs the same way the firmware layers them —
including socket clipping, lid sliding, iris travel, brow mirroring and
decorator anchoring. It reads your **source PNGs directly**, so there is no
conversion step and no device involved.

Output lands in `my_face/preview/`.

### Face Studio: live, editable, animated

Double-click `Face Studio.cmd` in the StackChan folder (or run
`python firmware/tools/face_studio.py`). It opens a page that draws the face the
way the firmware's custom skin does, with blink, lip sync, breathing, idle
glances and decorator pulse running on the firmware's own timings.

- **Place things.** Drag the anchors on the canvas: eyes, brows, mouth, and the
  shown decorator. Arrow keys nudge by 1 px, or 5 px with Shift. "Mirror moves"
  keeps left and right symmetric.
- **Pose every emotion.** Pick the lids, mouth, eyeOpen, brow lift and tilt, and
  decorator. A `*` marks a pose changed from the default. "+ custom" adds a new
  emotion name.
- **Add parts.** Add mouth shapes (a `kiss`, say), lid variants and decorators,
  then pick their PNGs.
- **Import from PixlPunkt.** Pick a `.pxp` and read its layers. Roles are
  guessed from layer names like `lid_open_r`, `mouth_s_2` and `dec_heart`.
  Full-canvas layers are cropped and placed so they land where you drew them.
  Sides come from where the art sits on the canvas, not from `_l`/`_r` in the
  name.
- **How lids import.** Each upper lid slides down to the bottom of the socket and
  gets skin filled above it from the base, so it is a real occluder. The log
  tells you the eyeOpen value that shows the lid as you drew it.
- **Checks.** The panel flags missing files and lids that are the wrong height
  or not opaque at the top. It also flags mouth frames lip sync can never reach,
  and decorators an emotion asks for that you haven't drawn.

**Save face.yaml** writes the file and keeps the previous one as
`face.yaml.bak-<time>`. Only the comments at the top survive a save. Imported
PNGs that replace older ones keep the old copy in `.studio-backup/`. Use
`?face=<folder>&sheet=1` in the address to open a face straight to the
all-emotions sheet. `build` is still `face.py`'s job.

---

### Sending a face to the robot

Face Studio's **Robot** panel puts a face on the device without touching the
microSD card. Give it the robot's address, and the control token if the firmware
was built with one (`CONFIG_STACKCHAN_CONTROL_TOKEN`).

- **Try this pose** shows the pose, and its twitch, using the face already there.
- **Send this face** saves, builds, uploads and reboots, in about 45 seconds. The
  upload waits in flash, and early boot copies it onto the card, because the card
  can't be written once the display is running. The old face stays as `face_old`.
- **Previous face** swaps `face` and `face_old`.

The studio keeps the address and token in `~/.face_studio.json`.

## face.yaml reference

```yaml
face:
  name: Cartoon Me
  base: base.png              # 320x240 opaque backdrop (optional)
  background: "#101010"       # shows where nothing is drawn
  mirror_eyes: true           # draw the right eye; the left is generated

parts:
  eyes:
    sclera: eye_white.png
    iris: iris.png
    socket_size: [56, 48]     # the clip window
    iris_parallax: 25         # % of gaze the whole eye follows
    # iris_travel: [15, 11]   # computed from the PNG sizes when omitted
    # clip_radius: 24         # round the clip for circular eyes
    right_pos: [52, -18]
    left_pos: [-52, -18]
    lids_upper:               # occluders, full socket height
      open: lid_open.png
      wide: lid_wide.png
      tight: lid_tight.png
      angle_up: lid_sad.png
      angle_down: lid_angry.png
    lids_lower:
      neutral: low_neutral.png
      raised: low_raised.png

  mouths:
    pos: [0, 46]
    travel: [8, 6]
    shapes:                   # each list is APERTURE: shut -> jaw wide open
                              # think . o O  (not -  --  ---)
      neutral: [mouth_n_0.png, mouth_n_1.png, mouth_n_2.png]
      smile:   [mouth_s_0.png, mouth_s_1.png, mouth_s_2.png]
      frown:   [mouth_f_0.png, mouth_f_1.png, mouth_f_2.png]
      open:    [mouth_o_0.png, mouth_o_1.png, mouth_o_2.png]

  brows:
    left: brow_l.png
    right: brow_r.png         # omit to mirror the left one
    left_pos: [-52, -46]
    right_pos: [52, -46]

  decorators:
    heart: {image: dec_heart.png, anchor: right_eye, offset: [22, -28], animate: pulse}
    blush: {image: dec_blush.png, anchor: face, offset: [0, 8]}

emotions:                     # optional -- all 21 already have defaults
  loving: {upper: tight, lower: raised, mouth: smile, decorator: heart, eyeOpen: 60, brow: [-3, -50]}
```

### Key fields

| Field | Meaning |
|---|---|
| `socket_size` | The clip window. Sclera and upper lid should match this height. |
| `iris_parallax` | 0 = only the iris moves. 100 = the whole eye moves with it. **25 is a good default** — breathing reads as a gentle drift, a glance reads as the iris travelling inside a still eye. |
| `iris_travel` | Auto-computed as `(sclera − iris) / 2` so the iris can never escape. Override to tighten it. |
| `clip_radius` | Rounds the socket clip. Use for circular eyes if the iris peeks past the curve. |
| `eyeOpen` | 0–100. 100 is wide open, 0 is shut. |
| `brow` | `[verticalOffset, rotation]`. Rotation is in **tenths of a degree** — `140` is 14°. Mirrored automatically on the right brow. |
| `anchor` | `face`, `left_eye`, `right_eye` or `mouth`. Anchored decorators follow the face as it drifts. |

Anything referencing a part you have not drawn degrades to something that
exists, so a partial face still renders. The build prints what it substituted.

---

### Touch spots

Rectangles of the screen that answer a tap with a pose, then go back. Draw them in
Face Studio's **Touch spots** panel, or write them by hand, in screen pixels from
the top left:

```yaml
touch:
  boop: {rect: [135, 105, 70, 50], pose: surprised, ms: 1500}
  forehead kiss: {rect: [105, 30, 110, 42], pose: loving, ms: 3000}
```

Anywhere else is still tap to talk. The first in the list wins where they overlap.
The studio warns about anything under 40 px a side, narrower than a fingertip.

A spot can fire on something other than a tap: `gesture:` is `tap`, `drag` (any way),
`left`, `right`, `up` or `down`, measured from where the finger came down, which has
to be inside the rectangle. `mirror: true` adds the same spot on the other side, with
left and right swapped.

### Head touches

What a touch on top of the head shows. Once a face has this block, only these answer,
and the stock happy face for a pet goes away. Pressure can't be read: a light brush
looks the same as a firm one.

```yaml
head:
  stroke_front_to_back: {pose: loving, ms: 3000}
  stroke_back_to_front: {pose: confused}
  poke_front: {pose: surprised}
  poke_middle: {pose: confused}
  poke_back: {pose: annoyed}
  finger_rest: {pose: thinking}
  hand: {pose: relaxed}
  scritch: {pose: laughing}
```

### Motion per pose

A pose can carry motion of its own, which runs while it holds:

```yaml
emotions:
  angry: {motion: {twitch: {eye: right, lid: closed, every: [2500, 6000], flutters: 3}}}
```

`eye` is `left`, `right` or `both`. `lid` is any upper lid you've drawn: the eye
flicks to that drawing and back `flutters` times, 70 ms a flick, then pauses for a
random time between the two `every` values, in milliseconds. A lid that isn't
drawn means no twitch, with a warning at build time.

A pose can keep its mouth moving on its own, so a two-frame sleeping mouth breathes:

```yaml
emotions:
  sleepy: {motion: {mouth_cycle: 3000}}
```

A pose can also roll its eyes, once, as it starts: `motion: {roll: 1200}` is the
milliseconds for the whole roll, 300 to 5000.

The iris rests in the middle of the eye white, measured from the drawn pixels, so
empty space in its PNG doesn't move it. Unless `iris_travel` is set by hand, a full
look takes the middle of the iris half an iris past the white's edge: rolled clean
off. In an idle glance the right eye (screen left) leads and the other lands
`glance_lag_ms` later, under `parts: eyes:`: 40 unless set, 0 to 300, 0 for together.
The socket only follows a look as far as the iris stays inside the white, so
drawn lids stay put. Sprite eyes don't move with breathing; the mouth does.

`mouth_cycle` is the milliseconds to open and close once, 300 to 20000. Otherwise a
mouth only moves while talking. While talking the speech has the mouth, and this
waits, then picks back up when the speech is done.

### Where the eyes look, and brows per side

```yaml
emotions:
  thinking: {look: [40, -50]}                              # both eyes, up and to the right
  focused: {look: {left: [40, 0], right: [-40, 0]}}        # each its own: cross-eyed
  fancy: {brow: {left: [-6, 0], right: [2, 30]}}           # one brow up
```

`look` is x, y from -100 to 100 and adds to wherever the gaze already is. `brow` is
y, tilt as always, and takes the same `left` and `right` form. Sides are screen sides.

### Rail lights per pose

A pose can also light the rails while it holds:

```yaml
emotions:
  loving: {lights: {color: "#ff4fa0", brightness: 40}}
  annoyed: {lights: {color: "#ff7a00", brightness: 40}}
  dizzy: {lights: {color: "#30ff60", brightness: 60, pulse: 1500}}
```

`pulse` is how long one breath takes in milliseconds, 300 to 10000, full to 15% and
back. Leave it out to hold steady. The LEDs take 5 bits of red and blue and 6 of
green, so a dim pulse has few steps to work with; higher brightness breathes smoother.

`brightness` is 0 to 100 and defaults to 40. The color fades in over 300 ms on LEDs
2-5 and 8-11, and a pose with no `lights` fades them out. LEDs 0, 1, 6 and 7 stay
free for the green, blue and red status lights, which sit above everything else on
the rails. A face with no `lights` at all never touches them. LEDs mix color
differently from a screen, so judge it on the robot with **Try this pose**.

### Sounds

A face can carry its own sounds, and the robot plays them itself: no voice server, no call open.

```yaml
sounds:
  gasp: sounds/gasp.ogg
  giggle: sounds/giggle.ogg
```

Each one is **Ogg Opus**, mono, ideally voice quality (a few KB a second). Make one from a WAV with:

```bash
ffmpeg -i gasp.wav -ac 1 -c:a libopus -b:a 24k gasp.ogg
```

- Names are `a-z`, `0-9`, `-` and `_`, up to 24 characters. Each goes on the card as `snd_<name>.ogg`.
- Up to 96 KB a sound (about 25 seconds) and 256 KB for all of a face's sounds, so the art keeps its room in
  the 2 MB the robot loads. `face.py build` says which one was left out and why.
- They load into PSRAM at boot with the rest of the face (the card can only be read then). `GET /api/touch-spots`
  lists them as `sounds`; `POST /api/sound {"name": "gasp"}` plays one. One at a time, and never over a reply
  being spoken (that's a 409).

## Emotions

Xiaozhi's full vocabulary, from `emoji_collection.cc`. **All 21 have working
defaults** — you only list one in `face.yaml` to override it.

```
neutral   happy     laughing   funny      sad       angry     crying
loving    embarrassed  surprised  shocked  thinking  winking  cool
relaxed   delicious  kissy      confident  sleepy    silly     confused
```

Suggested decorators, if you draw them: `heart` (loving, kissy), `blush`
(embarrassed), `sweat` (crying, shocked), `angry`, `dizzy` (silly), `question`
(thinking, confused), `sparkle` (cool, confident, delicious), `exclaim`
(surprised).

**Custom emotions work too.** Once you self-host the backend you control the
strings — emit `smug` or `suspicious` and add a row under `emotions:`. Unknown
strings fall back to neutral and log once.

The built-in vector face maps all 21 onto its six poses, so even without a
custom face nothing renders blank any more.

---

## Building and flashing

```bash
python tools/face.py build my_face
cd firmware && idf.py build && idf.py flash
```

`idf.py flash` writes the app **and** the assets partition.

**First time adding the skin sources?** The build globs source files at configure
time — run `idf.py reconfigure` once.

**Changing art?** `face.py build` deletes `build/generated_assets.bin` for you,
which forces a repack. Without it, `main/CMakeLists.txt:429` lists only the
sdkconfig and build script under `DEPENDS`, so changed sprites are silently
ignored and you flash the previous face.

```bash
python tools/face.py build my_face --dry-run    # print the manifest, write nothing
python tools/face.py build my_face --prefix me  # keep several faces from colliding
```

---

## Troubleshooting

**Eyes look wide open at every emotion, including sleepy.**
Your upper lids are not occluders. See
[the one rule](#the-one-rule-that-trips-everyone-up).

**The stock face is still showing.** Check the serial log for:
```
I (…) custom-face: loaded face 'Cartoon Me': 21 emotions, 4 mouth shapes, 2 decorators
I (…) StackChanAvatarDisplay: Using custom face: Cartoon Me
```
`no face.json in assets partition` means the assets were not packed — rerun
`build`, then `idf.py build`.

**`face.json is version 1, this firmware needs 2`.**
Built with the old flat-PNG tool. Rebuild with `tools/face.py build`.

**I changed my art and nothing changed.** Stale assets. Delete
`firmware/build/generated_assets.bin` and rebuild.

**A part is missing but the rest works.** `skipping missing sprite` in the log
means a name in the manifest does not match a packed asset. Check the 32-character
limit — the build fails loudly if you exceed it.

**The iris slides outside the eye.** Set `clip_radius` for round eyes, or reduce
`iris_travel`.

**The whole eye slides around when it should just glance.** Lower
`iris_parallax`.

**Art looks muddy.** Redraw at 320 × 240 at 100% zoom with thicker strokes.

---

## Under the hood

**Assets are memory-mapped, not loaded.** `assets.cc:147` calls
`esp_partition_mmap()` over the partition and `GetAssetData()` returns a direct
pointer into flash. LVGL blits your sprites straight from there — **zero RAM,
zero decode, zero copy.**

**That is why nothing is compressed.** Compressed or PNG sources must be inflated
into RAM first, and a 320 × 240 RGB565A8 layer is ~230 KB of PSRAM *per image*.
The converter does all the work offline so the device does none.

**RGB565A8** is 3 bytes/px — 16-bit colour plus a separate 8-bit alpha plane. The
opaque base uses plain RGB565 at 2 bytes/px, the cheapest blit LVGL does.

**Clipping is free.** A `Container` is `lv_obj_create`, and LVGL clips children to
the parent unless `LV_OBJ_FLAG_OVERFLOW_VISIBLE` is set. The stock face already
relies on this to blink.

**Only damaged regions redraw.** An iris flick dirties two ~56 × 48 rectangles, not
the screen. That is why ~17 layers stay affordable.

**The renderer is swappable by design.** `Feature`
(`stackchan/avatar/avatar/elements/feature.h`) defines `setPosition`,
`setRotation`, `setWeight`, `setSize`, `setEmotion`, `setVisible` and says nothing
about *how* a part is drawn. The stock skin uses rounded rectangles; this one uses
sprites. Modifiers drive the interface, so both behave identically.

```
firmware/main/stackchan/avatar/skins/custom/
├── custom.h            declarations
├── face_manifest.cpp   face.json parsing, SpriteSet / SpriteMap
├── eyes.cpp            EyeRig — sclera, iris, lower lid, upper lid
├── mouth.cpp           MouthRig — shape per emotion, frames for openness
├── decorator.cpp       SpriteDecorator — anchored, optional pulse
└── custom.cpp          CustomAvatar, brows, emotion dispatch

firmware/tools/face.py  init / preview / build
```

Selection and fallback live in `hal/board/stackchan_display.cc`, in `SetupUI()`;
emotion routing is in `SetEmotion()`.

---

## Sharing your face

A face is a folder of PNGs plus `face.yaml`. Zip it and anyone can run
`face.py build` to install it. If you make something good, open a PR.
