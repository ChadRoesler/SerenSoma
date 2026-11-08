#!/usr/bin/env python3
"""
face.py -- author, preview and build StackChan sprite faces.

    python firmware/tools/face.py init    my_face          # starter project
    python firmware/tools/face.py preview my_face          # contact sheet, no hardware
    python firmware/tools/face.py preview my_face --emotion loving --scale 3
    python firmware/tools/face.py preview my_face --animate blink
    python firmware/tools/face.py build   my_face          # convert + install

`preview` composites your PNGs exactly the way the firmware layers them, so you
can iterate on artwork in seconds instead of reflashing. `build` converts to
LVGL .bin, resolves mirroring, and writes the compiled face.json that the
device reads.

You author YAML. The device parses the compiled JSON with ArduinoJson. No YAML
parser ever runs on the ESP32.

See firmware/docs/custom-faces.md
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parent.parent
CONVERTER = FIRMWARE_ROOT / "xiaozhi-esp32" / "scripts" / "Image_Converter" / "LVGLImage.py"
ASSETS_BIN = FIRMWARE_ROOT / "main" / "assets" / "assets_bin"

SCREEN_W, SCREEN_H = 320, 240
MAX_ASSET_NAME = 32
ASSETS_PARTITION_BYTES = 4 * 1024 * 1024
MANIFEST_VERSION = 2

CF_OPAQUE = "RGB565"
CF_ALPHA = "RGB565A8"

# Xiaozhi's full emotion vocabulary (emoji_collection.cc). Every one of these
# gets a pose so nothing falls through to a blank neutral face. Anything the
# author defines in YAML overrides the entry here.
DEFAULT_EMOTIONS = {
    "neutral":     dict(upper="open",       lower="neutral", mouth="neutral", eyeOpen=86,  brow=[0, 0]),
    "relaxed":     dict(upper="open",       lower="neutral", mouth="smile",   eyeOpen=82,  brow=[0, 0]),
    "sleepy":      dict(upper="closed",     lower="neutral", mouth="neutral", eyeOpen=45,  brow=[2, 0]),

    "happy":       dict(upper="tight",      lower="raised",  mouth="smile",   eyeOpen=70,  brow=[-3, -60]),
    "laughing":    dict(upper="tight",      lower="raised",  mouth="smile",   eyeOpen=50,  brow=[-4, -80]),
    "funny":       dict(upper="tight",      lower="raised",  mouth="smile",   eyeOpen=60,  brow=[-3, -70]),
    "loving":      dict(upper="tight",      lower="raised",  mouth="smile",   eyeOpen=60,  brow=[-3, -50],
                        decorator="heart"),
    "kissy":       dict(upper="tight",      lower="raised",  mouth="smile",   eyeOpen=50,  brow=[-3, -50],
                        decorator="heart"),
    "delicious":   dict(upper="tight",      lower="raised",  mouth="smile",   eyeOpen=55,  brow=[-3, -60],
                        decorator="sparkle"),
    "embarrassed": dict(upper="tight",      lower="raised",  mouth="neutral", eyeOpen=65,  brow=[-2, -40],
                        decorator="blush"),
    "winking":     dict(upper="tight",      lower="raised",  mouth="smile",   eyeOpen=65,  brow=[-3, -60]),

    "sad":         dict(upper="angle_up",   lower="neutral", mouth="frown",   eyeOpen=80,  brow=[-2, -140]),
    "crying":      dict(upper="angle_up",   lower="raised",  mouth="frown",   eyeOpen=60,  brow=[-2, -150],
                        decorator="sweat"),
    "confused":    dict(upper="angle_up",   lower="neutral", mouth="neutral", eyeOpen=85,  brow=[-4, 60],
                        decorator="question"),

    "angry":       dict(upper="angle_down", lower="neutral", mouth="frown",   eyeOpen=75,  brow=[4, 140],
                        decorator="angry"),
    "thinking":    dict(upper="angle_down", lower="neutral", mouth="neutral", eyeOpen=80,  brow=[-3, 70],
                        decorator="question"),
    "cool":        dict(upper="angle_down", lower="neutral", mouth="smile",   eyeOpen=70,  brow=[0, 0],
                        decorator="sparkle"),
    "confident":   dict(upper="angle_down", lower="neutral", mouth="smile",   eyeOpen=75,  brow=[1, 30],
                        decorator="sparkle"),

    "surprised":   dict(upper="wide",       lower="neutral", mouth="open",    eyeOpen=100, brow=[-6, -30],
                        decorator="exclaim"),
    "shocked":     dict(upper="wide",       lower="neutral", mouth="open",    eyeOpen=100, brow=[-8, -20],
                        decorator="sweat"),
    "silly":       dict(upper="wide",       lower="raised",  mouth="open",    eyeOpen=90,  brow=[-3, -80],
                        decorator="dizzy"),
}

STARTER_YAML = """\
# StackChan face -- see firmware/docs/custom-faces.md
face:
  name: My Face
  base: base.png              # 320x240 opaque backdrop (optional)
  background: "#101010"
  mirror_eyes: true           # draw one eye; the other is generated
  drawn_eye: right            # which SCREEN side you drew (left = -x, right = +x)

parts:
  eyes:
    sclera: eye_white.png
    iris: iris.png
    socket_size: [56, 48]     # the clip window; lids and sclera live inside it
    iris_parallax: 25         # % of gaze the whole eye follows. 0 = iris only
    # iris_travel: [12, 8]    # computed from the PNG sizes when omitted
    # clip_radius: 24         # round the clip for circular eyes
    right_pos: [52, -18]
    left_pos: [-52, -18]
    lids_upper:
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
      smile: [mouth_s_0.png, mouth_s_1.png, mouth_s_2.png]
      frown: [mouth_f_0.png, mouth_f_1.png, mouth_f_2.png]
      open: [mouth_o_0.png, mouth_o_1.png, mouth_o_2.png]

  brows:
    left: brow_l.png
    right: brow_r.png
    left_pos: [-52, -46]
    right_pos: [52, -46]

  decorators:
    heart: {image: dec_heart.png, anchor: right_eye, offset: [22, -28], animate: pulse}
    blush: {image: dec_blush.png, anchor: face, offset: [0, 8]}

# Every one of xiaozhi's 21 emotions already has a sensible default.
# List one here only to override it.
emotions:
  # loving: {upper: tight, lower: raised, mouth: smile, decorator: heart, eyeOpen: 60}
"""


def fail(message):
    print("error: " + message, file=sys.stderr)
    sys.exit(1)


def warn(message):
    print("  warning: " + message)


def need(module, package):
    try:
        return __import__(module)
    except ImportError:
        fail("missing Python package '%s'\n       pip install %s" % (package, package))


# --------------------------------------------------------------------------- #
#                                  YAML model                                  #
# --------------------------------------------------------------------------- #


# An eye part is one PNG (drawn once; the other side is its mirror when mirror_eyes)
# or {left: ..., right: ...}: a rig drawn per side. left/right are SCREEN sides.
SIDES = ("left", "right")


def side_src(value, side):
    if isinstance(value, dict):
        return value.get(side) or ""
    return value or ""


def all_srcs(value):
    if isinstance(value, dict):
        return [v for v in value.values() if v]
    return [value] if value else []


# The brow shape an emotion uses by default, when a face draws brow shapes.
BROW_SHAPE = {"angry": "angry", "sad": "sad", "crying": "sad", "confused": "sad", "thinking": "angry"}


def lights_rgb(lights):
    """A pose's rail color as the firmware wants it, or None. 168 is the most any channel gets, and
    the color is scaled rather than clipped so pink stays pink."""
    if not lights:
        return None
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(lights.get("color") or ""))
    if not m:
        raise ValueError(f"'{lights.get('color')}' isn't a color like #ff4fa0")
    brightness = max(0, min(100, int(lights.get("brightness", 40))))
    rgb = bytes.fromhex(m.group(1))
    return [round(v * 168 / 255 * brightness / 100) for v in rgb]


def half_up(x):
    """Round like JavaScript's Math.round, so the Studio and the build agree to the pixel."""
    import math
    return int(math.floor(x + 0.5))


def sided_pair(value):
    """A pair that's the same on both sides or {left, right} per side, always as {left, right}."""
    if isinstance(value, dict):
        return {side: [int(v) for v in (value.get(side) or [0, 0])[:2]] for side in ("left", "right")}
    pair = [int(v) for v in (value or [0, 0])[:2]]
    return {"left": pair, "right": list(pair)}


def lights_pulse(lights):
    """Milliseconds for one breath of a pose's rail color, or 0 to hold steady."""
    pulse = int((lights or {}).get("pulse") or 0)
    return max(300, min(10000, pulse)) if pulse > 0 else 0


TOUCH_GESTURES = ("tap", "drag", "left", "right", "up", "down")
HEAD_TOUCHES = ("stroke_front_to_back", "stroke_back_to_front", "poke_front", "poke_middle", "poke_back",
                "finger_rest", "hand", "scritch")


class Face:
    """The authored face, resolved against defaults and validated."""

    def __init__(self, src_dir, doc):
        self.src = Path(src_dir)
        face = doc.get("face") or {}
        parts = doc.get("parts") or {}

        self.name = face.get("name", "My Face")
        self.background = face.get("background", "#101010")
        self.base = face.get("base")
        self.mirror = bool(face.get("mirror_eyes", True))
        # Which SCREEN side the single eye was drawn for. left matches
        # left_pos (-x), right matches right_pos (+x). The other side is
        # the generated mirror.
        self.drawn_eye = str(face.get("drawn_eye", "right")).lower()
        # The rig (Face Studio): how lids move, what masks the iris, where brows sit.
        # slide/socket/below are what the firmware has always done.
        self.lid_mode = str(face.get("lid_mode", "slide")).lower()
        self.iris_mask = str(face.get("iris_mask", "socket")).lower()
        self.brows_above = bool(face.get("brows_above_eyes", False))
        # hair over the face: drawn above the rig, under the decorators (Face Studio)
        self.overlay = face.get("overlay") or ""

        eyes = parts.get("eyes") or {}
        self.sclera = eyes.get("sclera")
        self.iris = eyes.get("iris")
        self.socket_size = list(eyes.get("socket_size", [56, 48]))
        self.iris_parallax = int(eyes.get("iris_parallax", 25))
        self.iris_travel = eyes.get("iris_travel")
        self.glance_lag_ms = max(0, min(300, int(eyes.get("glance_lag_ms", 40))))
        self.clip_radius = int(eyes.get("clip_radius", 0))
        # Colour painted above the upper lid as it closes. Without it a lid
        # shorter than the socket can never occlude the eye. Sampled from the
        # lid art when not given.
        self.lid_fill = eyes.get("lid_fill")
        self.right_pos = list(eyes.get("right_pos", [52, -18]))
        self.left_pos = list(eyes.get("left_pos", [-52, -18]))
        self.lids_upper = dict(eyes.get("lids_upper") or {})
        self.lids_lower = dict(eyes.get("lids_lower") or {})

        mouths = parts.get("mouths") or {}
        self.mouth_pos = list(mouths.get("pos", [0, 46]))
        self.mouth_travel = list(mouths.get("travel", [8, 6]))
        self.mouth_shapes = dict(mouths.get("shapes") or {})

        brows = parts.get("brows") or {}
        self.brow_left = brows.get("left")
        self.brow_right = brows.get("right")
        self.brow_left_pos = list(brows.get("left_pos", [-52, -46]))
        self.brow_right_pos = list(brows.get("right_pos", [52, -46]))
        self.brow_shapes = dict(brows.get("shapes") or {})

        self.decorators = dict(parts.get("decorators") or {})
        self.emotions = self._resolve_emotions(doc.get("emotions") or {})
        self.touch = self._resolve_touch(doc.get("touch"))
        self.head = self._resolve_head(doc.get("head"))

    def _resolve_emotions(self, overrides):
        """Merge author overrides onto the defaults, then degrade any reference
        to a part that does not exist so nothing silently renders blank."""
        resolved = {}
        for name, base in DEFAULT_EMOTIONS.items():
            pose = dict(base)
            pose.update(overrides.get(name) or {})
            resolved[name] = pose
        # Author-only emotions (custom backends can emit anything).
        for name, pose in overrides.items():
            if name not in resolved:
                merged = dict(DEFAULT_EMOTIONS["neutral"])
                merged.update(pose)
                resolved[name] = merged

        for name, pose in resolved.items():
            ov = overrides.get(name) or {}
            if name == "winking" and "upper" not in ov and "closed" in self.lids_upper:
                # a wink is one eye
                # the other eye properly open: a squint beside a closed eye reads as both shut
                pose["upper"] = {"left": "open" if "open" in self.lids_upper else pose.get("upper"), "right": "closed"}
                if "lower" not in ov and "closed" in self.lids_lower:
                    pose["lower"] = {"left": "neutral" if "neutral" in self.lids_lower else pose.get("lower"), "right": "closed"}
            for part, avail, label in (("upper", self.lids_upper, "upper lid"), ("lower", self.lids_lower, "lower lid")):
                asked = pose.get(part)
                pose[part + "_sides"] = {sd: self._pick(side_src(asked, sd), avail, label, name) for sd in SIDES}
                # one name for firmware without sides: the left one (a wink's open eye),
                # so it never shuts both eyes for a one-eyed pose
                pose[part] = pose[part + "_sides"]["left"]
            pose["mouth"] = self._pick(pose.get("mouth"), self.mouth_shapes, "mouth shape", name)
            pose["motion"] = self._resolve_motion(pose.get("motion"), name)
            try:
                pose["lights_rgb"] = lights_rgb(pose.get("lights"))
                pose["lights_pulse"] = lights_pulse(pose.get("lights"))
            except (ValueError, AttributeError, TypeError) as e:
                print(f"  warning: {name}'s lights: {e}; no lights")
                pose["lights_rgb"] = None
            dec = pose.get("decorator")
            # one decorator or several (Face Studio); only the drawn ones are kept
            decs = [d for d in (dec if isinstance(dec, list) else [dec] if dec else []) if d in self.decorators]
            pose["decorators"] = decs
            pose["decorator"] = decs[0] if decs else ""
            if self.brow_shapes:
                # drawn shapes carry the expression: no default tilt on top (a face's own
                # override can still set one), and each emotion picks a shape
                if "brow" not in (overrides.get(name) or {}):
                    pose["brow"] = [0, 0]
                shapes = sorted(self.brow_shapes)
                want = (overrides.get(name) or {}).get("brow_shape") or BROW_SHAPE.get(name, "neutral")
                one = lambda w: w if w in self.brow_shapes else ("neutral" if "neutral" in self.brow_shapes else shapes[0])
                pose["brow_shape_sides"] = {sd: one(side_src(want, sd)) for sd in SIDES}
                pose["brow_shape"] = pose["brow_shape_sides"]["left"]
        return resolved

    def _resolve_touch(self, touch):
        """Rectangles of the screen that answer a tap with a pose. None: the face doesn't say."""
        if touch is None:
            return None
        spots = []
        for name, spot in touch.items():
            pose, rect = spot.get("pose"), spot.get("rect") or []
            if pose not in self.emotions:
                print(f"  warning: touch spot {name} wants pose '{pose}', which this face doesn't have; left out")
                continue
            if len(rect) != 4:
                print(f"  warning: touch spot {name} has no rectangle; left out")
                continue
            gesture = spot.get("gesture", "tap")
            if gesture not in TOUCH_GESTURES:
                print(f"  warning: touch spot {name} has gesture '{gesture}', which isn't one; left out")
                continue
            one = {"name": name, "pose": pose, "rect": [int(v) for v in rect], "ms": int(spot.get("ms", 2000)), "gesture": gesture}
            spots.append(one)
            if spot.get("mirror"):
                x, y, w, h = one["rect"]
                spots.append({**one, "name": f"{name} (mirror)", "rect": [SCREEN_W - x - w, y, w, h],
                              "gesture": {"left": "right", "right": "left"}.get(gesture, gesture)})
        return spots

    def _resolve_head(self, head):
        """What each kind of touch on top of the head shows. None: the face doesn't say."""
        if head is None:
            return None
        out = {}
        for kind, spec in head.items():
            if kind not in HEAD_TOUCHES:
                print(f"  warning: head touch '{kind}' isn't one; left out")
            elif spec.get("pose") not in self.emotions:
                print(f"  warning: head touch {kind} wants pose '{spec.get('pose')}', which this face doesn't have; left out")
            else:
                out[kind] = {"pose": spec["pose"], "ms": int(spec.get("ms", 2000))}
        return out

    def _resolve_motion(self, motion, emotion):
        """What a pose asks to do while it holds. Only what can actually run reaches the device."""
        out = {}
        cycle = int((motion or {}).get("mouth_cycle") or 0)
        if cycle > 0:
            out["mouth_cycle"] = max(300, min(20000, cycle))
        roll = int((motion or {}).get("roll") or 0)
        if roll > 0:
            out["roll"] = max(300, min(5000, roll))
        twitch = (motion or {}).get("twitch")
        if not twitch:
            return out
        lid = twitch.get("lid", "closed")
        if lid not in self.lids_upper:
            print(f"  warning: {emotion}'s twitch wants upper lid '{lid}', which isn't drawn; no twitch")
            return out
        every = list(twitch.get("every") or [2500, 6000])
        out["twitch"] = {"eye": twitch.get("eye", "right"), "lid": lid, "every": [int(every[0]), int(every[1])],
                         "flutters": int(twitch.get("flutters", 3))}
        return out

    @staticmethod
    def _pick(wanted, available, label, emotion):
        """Fall back to something that exists rather than referencing a missing part."""
        if not available:
            return ""
        if wanted in available:
            return wanted
        for fallback in ("open", "neutral"):
            if fallback in available:
                return fallback
        return sorted(available)[0]

    # ---------------------------- source resolution --------------------------- #

    def path(self, name):
        return self.src / name

    def all_source_files(self):
        out = []
        for n in (self.base, self.brow_left, self.brow_right):
            if n:
                out.append(n)
        out += all_srcs(self.sclera) + all_srcs(self.iris)
        for v in list(self.lids_upper.values()) + list(self.lids_lower.values()) + list(self.brow_shapes.values()):
            out += all_srcs(v)
        out += [p["base"] for p in self.emotions.values() if p.get("base")]
        if self.overlay:
            out.append(self.overlay)
        out += [p["overlay"] for p in self.emotions.values() if p.get("overlay")]
        for frames in self.mouth_shapes.values():
            out += list(frames)
        for spec in self.decorators.values():
            if spec.get("image"):
                out.append(spec["image"])
        return out

    def validate(self):
        problems = []
        if not self.sclera and not self.lids_upper:
            problems.append("no eyes defined -- need at least parts.eyes.sclera or lids_upper")
        for name in self.all_source_files():
            if not self.path(name).exists():
                problems.append("missing file: %s" % name)
        if self.drawn_eye not in ("left", "right"):
            problems.append("drawn_eye must be left or right, got %s" % self.drawn_eye)
        if self.lid_mode not in ("slide", "swap"):
            problems.append("lid_mode must be slide or swap, got %s" % self.lid_mode)
        if self.iris_mask not in ("socket", "sclera"):
            problems.append("iris_mask must be socket or sclera, got %s" % self.iris_mask)
        if self.lid_mode == "swap" and "closed" not in self.lids_upper:
            problems.append("lid_mode swap needs an upper lid called closed (what a blink shows)")
        if not 0 <= self.iris_parallax <= 100:
            problems.append("iris_parallax must be 0-100, got %s" % self.iris_parallax)
        return problems


def load_face(src_dir):
    yaml = need("yaml", "PyYAML")
    src = Path(src_dir)
    if not src.is_dir():
        fail("%s is not a directory" % src)

    candidates = [src / "face.yaml", src / "face.yml"]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        fail("no face.yaml in %s\n       create one with: python tools/face.py init %s" % (src, src))

    with io_open(path) as fh:
        doc = yaml.safe_load(fh) or {}

    face = Face(src, doc)
    problems = face.validate()
    if problems:
        fail("face.yaml problems:\n       - " + "\n       - ".join(problems))
    return face


def io_open(path):
    return open(str(path), "r", encoding="utf-8")


# --------------------------------------------------------------------------- #
#                                   Preview                                    #
# --------------------------------------------------------------------------- #


class Compositor:
    """Renders a face the way the firmware layers it, using Pillow."""

    def __init__(self, face):
        need("PIL", "Pillow")
        from PIL import Image

        self.Image = Image
        self.face = face
        self._cache = {}

        self.iris_travel = face.iris_travel or self._auto_travel()
        self.lid_fill = face.lid_fill or self._auto_lid_fill()

    def _load(self, name, mirror=False):
        key = (name, mirror)
        if key not in self._cache:
            im = self.Image.open(str(self.face.path(name))).convert("RGBA")
            if mirror:
                im = im.transpose(self.Image.FLIP_LEFT_RIGHT)
            self._cache[key] = im
        return self._cache[key]

    def _auto_travel(self):
        return self.iris_fit("right")["travel"]

    def iris_fit(self, side):
        """Where the iris rests and how far it goes, measured from the drawn pixels, not the PNGs:
        it rests in the middle of the eye white, and at full travel its middle is half an iris past
        the white's edge, rolled clean off. room is how far it goes and stays inside."""
        key = ("fit", side)
        if key in self._cache:
            return self._cache[key]
        f = self.face
        s, i = self._eye_part(f.sclera, side), self._eye_part(f.iris, side)
        sb = s.getchannel("A").getbbox() if s is not None else None
        ib = i.getchannel("A").getbbox() if i is not None else None
        if not sb or not ib:
            fit = {"offset": [0, 0], "travel": [12, 8], "room": None}
        else:
            sw, sh = f.socket_size
            sx, sy = int((sw - s.width) / 2), int((sh - s.height) / 2)
            ix, iy = int((sw - i.width) / 2), int((sh - i.height) / 2)
            ww, wh, iw, ih = sb[2] - sb[0], sb[3] - sb[1], ib[2] - ib[0], ib[3] - ib[1]
            fit = {"offset": [half_up(sx + (sb[0] + sb[2]) / 2 - ix - (ib[0] + ib[2]) / 2),
                              half_up(sy + (sb[1] + sb[3]) / 2 - iy - (ib[1] + ib[3]) / 2)],
                   "travel": [half_up(ww / 2 + iw / 2), half_up(wh / 2 + ih / 2)],
                   "room": [max(0, int((ww - iw) / 2)), max(0, int((wh - ih) / 2))]}
        self._cache[key] = fit
        return fit

    def _auto_lid_fill(self):
        """Most common opaque colour in the upper lid's topmost drawn row.

        That row is the edge that meets the brow, so it is the colour the skin
        above the lid should be.
        """
        if not self.face.lids_upper:
            return None
        first = self.face.lids_upper.get("open") or list(self.face.lids_upper.values())[0]
        name = side_src(first, "right") or side_src(first, "left")
        try:
            im = self._load(name)
        except Exception:
            return None
        px = im.load()
        for y in range(im.height):
            counts = {}
            for x in range(im.width):
                r, g, b, a = px[x, y]
                if a > 200:
                    counts[(r, g, b)] = counts.get((r, g, b), 0) + 1
            if counts:
                r, g, b = max(counts, key=counts.get)
                return "#%02X%02X%02X" % (r, g, b)
        return None

    @staticmethod
    def _hex(color):
        c = color.lstrip("#")
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)

    def _paste_centre(self, canvas, sprite, cx, cy, rotation=0):
        if rotation:
            sprite = sprite.rotate(-rotation / 10.0, resample=self.Image.BICUBIC, expand=False)
        x = int(cx - sprite.width / 2)
        y = int(cy - sprite.height / 2)
        canvas.alpha_composite(sprite, (x, y))

    def _eye_part(self, value, side):
        """A part's image for one side: its own, or the drawn one mirrored."""
        src = side_src(value, side)
        if not src:
            return None
        mirror = (not isinstance(value, dict)) and self.face.mirror and side != self.face.drawn_eye
        return self._load(src, mirror)

    def _render_eye(self, canvas, pose, is_left, gaze):
        f = self.face
        side = "left" if is_left else "right"
        anchor = f.left_pos if is_left else f.right_pos
        swap = f.lid_mode == "swap"
        shut = swap and pose.get("eyeOpen", 100) < 50

        look = sided_pair(pose.get("look"))[side]
        fit = self.iris_fit(side)
        gx = int(max(-1.0, min(1.0, gaze[0] + look[0] / 100.0)) * self.iris_travel[0])
        gy = int(max(-1.0, min(1.0, gaze[1] + look[1] / 100.0)) * self.iris_travel[1])

        sw, sh = f.socket_size
        # Socket follows a fraction of the gaze, only as far as the iris stays in the white; the iris takes all of it.
        room = fit["room"]
        fx = max(-room[0], min(room[0], gx)) if room else gx
        fy = max(-room[1], min(room[1], gy)) if room else gy
        scx = SCREEN_W / 2 + anchor[0] + fx * f.iris_parallax / 100.0
        scy = SCREEN_H / 2 + anchor[1] + fy * f.iris_parallax / 100.0

        socket = self.Image.new("RGBA", (sw, sh), (0, 0, 0, 0))

        s = self._eye_part(f.sclera, side)
        if s is not None:
            socket.alpha_composite(s, (int((sw - s.width) / 2), int((sh - s.height) / 2)))
        i = self._eye_part(f.iris, side)
        if i is not None:
            layer = self.Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            self._composite_clipped(layer, i, int((sw - i.width) / 2 + gx + fit["offset"][0]), int((sh - i.height) / 2 + gy + fit["offset"][1]))
            if f.iris_mask == "sclera" and s is not None:
                # the iris only shows where the eye white is
                white = self.Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
                white.alpha_composite(s, (int((sw - s.width) / 2), int((sh - s.height) / 2)))
                from PIL import ImageChops
                layer.putalpha(ImageChops.multiply(layer.getchannel("A"), white.getchannel("A")))
            socket.alpha_composite(layer)

        lower_name = "closed" if shut and "closed" in f.lids_lower else (pose.get("lower_sides") or {}).get(side, pose.get("lower"))
        if lower_name and lower_name in f.lids_lower:
            l = self._eye_part(f.lids_lower[lower_name], side)
            if l is not None:
                socket.alpha_composite(l, (int((sw - l.width) / 2), sh - l.height))

        upper_name = "closed" if shut and "closed" in f.lids_upper else (pose.get("upper_sides") or {}).get(side, pose.get("upper"))
        u = self._eye_part(f.lids_upper[upper_name], side) if upper_name and upper_name in f.lids_upper else None
        if u is not None and swap:
            # swap lids sit where they were drawn; the pose (or a blink) picks which
            socket.alpha_composite(u, (int((sw - u.width) / 2), 0))
        elif u is not None:
            # The lid travels the full socket height, not just its own, so a
            # short lash-line sprite can still shut the eye.
            travel = sh + u.height
            offset = int(sh - (pose.get("eyeOpen", 100) / 100.0) * travel)
            # Skin above the lid edge, which is what actually occludes.
            if self.lid_fill and offset > 0:
                # Match the lid's own width, so the skin above it follows the
                # eyelid silhouette instead of filling the whole socket.
                fw = min(u.width, sw)
                fill = self.Image.new("RGBA", (fw, min(offset, sh)), self._hex(self.lid_fill))
                socket.alpha_composite(fill, (int((sw - fw) / 2), 0))
            self._composite_clipped(socket, u, int((sw - u.width) / 2), offset)

        if f.clip_radius > 0:
            socket = self._round_clip(socket, f.clip_radius)

        canvas.alpha_composite(socket, (int(scx - sw / 2), int(scy - sh / 2)))

    def _composite_clipped(self, target, sprite, x, y):
        """alpha_composite rejects negative offsets; crop instead -- which is
        exactly what LVGL's parent clipping does on the device."""
        if y >= 0:
            target.alpha_composite(sprite, (x, y))
            return
        visible = sprite.height + y
        if visible <= 0:
            return
        target.alpha_composite(sprite.crop((0, -y, sprite.width, sprite.height)), (x, 0))

    def _round_clip(self, img, radius):
        from PIL import ImageDraw

        mask = self.Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=radius, fill=255)
        out = self.Image.new("RGBA", img.size, (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out

    def brow_srcs(self, shape):
        """(left, right, left_mirrored, right_mirrored) for a shape, or the plain brows."""
        f = self.face
        if f.brow_shapes:
            pair = f.brow_shapes.get(shape) or f.brow_shapes.get("neutral") or list(f.brow_shapes.values())[0]
            l, r = side_src(pair, "left"), side_src(pair, "right")
        else:
            l, r = f.brow_left, f.brow_right
        return (l or r, r or l, f.mirror and not l, f.mirror and not r)

    def _render_brows(self, canvas, pose):
        f = self.face
        brow = sided_pair(pose.get("brow"))
        sides = pose.get("brow_shape_sides") or {}
        src_l, _, mir_l, _ = self.brow_srcs(sides.get("left", pose.get("brow_shape", "neutral")))
        _, src_r, _, mir_r = self.brow_srcs(sides.get("right", pose.get("brow_shape", "neutral")))
        if src_l:
            self._paste_centre(canvas, self._load(src_l, mir_l), SCREEN_W / 2 + f.brow_left_pos[0],
                               SCREEN_H / 2 + f.brow_left_pos[1] + brow["left"][0], brow["left"][1])
        if src_r:
            self._paste_centre(canvas, self._load(src_r, mir_r), SCREEN_W / 2 + f.brow_right_pos[0],
                               SCREEN_H / 2 + f.brow_right_pos[1] + brow["right"][0], -brow["right"][1])

    def render(self, emotion, mouth_open=0.0, gaze=(0.0, 0.0), blink=None):
        f = self.face
        pose = dict(f.emotions.get(emotion) or f.emotions["neutral"])
        if blink is not None:
            pose["eyeOpen"] = int(blink * 100)

        canvas = self.Image.new("RGBA", (SCREEN_W, SCREEN_H), self._hex(f.background))

        base = pose.get("base") or f.base
        if base:
            b = self._load(base)
            canvas.alpha_composite(b, (int((SCREEN_W - b.width) / 2), int((SCREEN_H - b.height) / 2)))

        if not f.brows_above:
            self._render_brows(canvas, pose)
        self._render_eye(canvas, pose, True, gaze)
        self._render_eye(canvas, pose, False, gaze)
        if f.brows_above:
            self._render_brows(canvas, pose)

        shape = pose.get("mouth")
        frames = f.mouth_shapes.get(shape) or []
        if frames:
            idx = int(round(mouth_open * (len(frames) - 1)))
            m = self._load(frames[max(0, min(idx, len(frames) - 1))])
            self._paste_centre(canvas, m, SCREEN_W / 2 + f.mouth_pos[0], SCREEN_H / 2 + f.mouth_pos[1])

        overlay = pose["overlay"] if "overlay" in pose else f.overlay
        if overlay:
            o = self._load(overlay)
            canvas.alpha_composite(o, (int((SCREEN_W - o.width) / 2), int((SCREEN_H - o.height) / 2)))

        for dec in pose.get("decorators") or ([pose["decorator"]] if pose.get("decorator") else []):
            if dec not in f.decorators:
                continue
            spec = f.decorators[dec]
            anchor = {"left_eye": f.left_pos, "right_eye": f.right_pos,
                      "mouth": f.mouth_pos}.get(spec.get("anchor", "face"), [0, 0])
            off = spec.get("offset", [0, 0])
            self._paste_centre(canvas, self._load(spec["image"]),
                               SCREEN_W / 2 + anchor[0] + off[0],
                               SCREEN_H / 2 + anchor[1] + off[1])

        return canvas


def cmd_preview(args):
    face = load_face(args.source)
    comp = Compositor(face)
    from PIL import Image, ImageDraw

    out = Path(args.out) if args.out else Path(args.source) / "preview"
    out.mkdir(parents=True, exist_ok=True)

    if args.animate:
        frames = []
        if args.animate == "blink":
            seq = [1.0, 1.0, 1.0, 1.0, 0.6, 0.2, 0.0, 0.2, 0.6, 1.0]
            frames = [comp.render(args.emotion or "neutral", blink=b) for b in seq]
        elif args.animate == "talk":
            seq = [0.0, 0.8, 0.2, 1.0, 0.1, 0.7, 0.0]
            frames = [comp.render(args.emotion or "neutral", mouth_open=m) for m in seq]
        elif args.animate == "gaze":
            seq = [(0, 0), (1, 0), (1, -.5), (0, 0), (-1, 0), (-1, .5), (0, 0)]
            frames = [comp.render(args.emotion or "neutral", gaze=g) for g in seq]

        path = out / ("animate_%s.gif" % args.animate)
        scaled = [f.convert("RGB").resize((SCREEN_W * args.scale, SCREEN_H * args.scale), Image.NEAREST)
                  for f in frames]
        scaled[0].save(str(path), save_all=True, append_images=scaled[1:], duration=140, loop=0)
        print("wrote %s (%d frames)" % (path, len(scaled)))
        return

    if args.emotion:
        img = comp.render(args.emotion).convert("RGB")
        if args.scale != 1:
            img = img.resize((SCREEN_W * args.scale, SCREEN_H * args.scale), Image.NEAREST)
        path = out / ("emotion_%s.png" % args.emotion)
        img.save(str(path))
        print("wrote %s" % path)
        return

    # Contact sheet of every emotion, rendered at 1:1 so you can judge legibility.
    names = sorted(face.emotions)
    cols = 4
    rows = (len(names) + cols - 1) // cols
    pad, label_h = 8, 14
    sheet = Image.new("RGB", (cols * (SCREEN_W + pad) + pad,
                              rows * (SCREEN_H + label_h + pad) + pad), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)

    for i, name in enumerate(names):
        cx = pad + (i % cols) * (SCREEN_W + pad)
        cy = pad + (i // cols) * (SCREEN_H + label_h + pad)
        sheet.paste(comp.render(name).convert("RGB"), (cx, cy))
        draw.text((cx + 2, cy + SCREEN_H + 2), name, fill=(200, 200, 210))

    path = out / "contact_sheet.png"
    sheet.save(str(path))
    print("wrote %s  (%d emotions at 1:1 -- if it does not read here, it will not read on the panel)"
          % (path, len(names)))


# --------------------------------------------------------------------------- #
#                                    Build                                     #
# --------------------------------------------------------------------------- #


def check_converter_deps():
    # LVGLImage.py imports lz4 unconditionally even though we never compress.
    missing = [pkg for mod, pkg in (("png", "pypng"), ("PIL", "Pillow"), ("lz4", "lz4"))
               if not _has(mod)]
    if missing:
        fail("missing Python packages: %s\n       pip install %s"
             % (", ".join(missing), " ".join(missing)))


def _has(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


class Builder:
    def __init__(self, face, prefix, out_dir, dry_run):
        self.face = face
        self.prefix = prefix
        self.out = Path(out_dir)
        self.dry = dry_run
        self.written = {}
        self._tmp = None

    def __enter__(self):
        self._tmp = tempfile.mkdtemp()
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def asset(self, role, source, alpha=True, mirror=False, cf=None):
        """Convert one PNG, return the asset name the firmware will request.
        cf overrides the colour format: "A8" makes an alpha-only mask (the eye white
        that masks the iris)."""
        if not source:
            return ""
        name = "%s_%s%s.bin" % (self.prefix, role, "_m" if mirror else "")
        if len(name) > MAX_ASSET_NAME:
            fail("asset name '%s' is %d chars, limit %d. Use a shorter --prefix or part name."
                 % (name, len(name), MAX_ASSET_NAME))
        if name in self.written:
            return name
        if self.dry:
            self.written[name] = 0
            return name

        src = self.face.path(source)
        if mirror:
            from PIL import Image

            flipped = Path(self._tmp) / ("m_" + Path(source).name)
            with Image.open(str(src)) as im:
                im.convert("RGBA").transpose(Image.FLIP_LEFT_RIGHT).save(str(flipped))
            src = flipped

        stage = Path(self._tmp) / "out"
        stage.mkdir(exist_ok=True)
        cmd = [sys.executable, str(CONVERTER), "--ofmt", "BIN",
               "--cf", cf or (CF_ALPHA if alpha else CF_OPAQUE), "-o", str(stage), str(src)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            fail("converting %s failed:\n%s\n%s" % (source, res.stdout, res.stderr))

        produced = stage / (Path(src).stem + ".bin")
        if not produced.exists():
            fail("converter produced no output for %s" % source)

        shutil.copy(str(produced), str(self.out / name))
        self.written[name] = produced.stat().st_size
        return name

    def asset_map(self, role, mapping, mirror=False, side=None):
        return {key: self.eye_asset("%s_%s" % (role, key), src, side, mirror) for key, src in mapping.items()}

    def eye_asset(self, role, value, side, mirror, cf=None):
        """One side of an eye part: its own PNG (role_l / role_r), or the drawn one mirrored."""
        if isinstance(value, dict):
            return self.asset("%s_%s" % (role, side[0]), side_src(value, side), cf=cf)
        return self.asset(role, value, mirror=mirror, cf=cf)


def cmd_build(args):
    if not CONVERTER.exists():
        fail("converter not found at %s\n       run: python firmware/fetch_repos.py" % CONVERTER)
    check_converter_deps()

    face = load_face(args.source)
    if not re.fullmatch(r"[A-Za-z0-9]+", args.prefix):
        fail("--prefix must be letters and digits only")

    out_dir = Path(args.out)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    print("Building '%s' from %s" % (face.name, args.source))

    comp = Compositor(face)
    with Builder(face, args.prefix, out_dir, args.dry_run) as b:
        mir = face.mirror
        # Mirror the side that was not drawn.
        mir_left = mir and face.drawn_eye == "right"
        mir_right = mir and face.drawn_eye == "left"
        manifest = {
            **({"touch": face.touch} if face.touch is not None else {}),
            **({"head": face.head} if face.head is not None else {}),
            "name": face.name,
            "version": MANIFEST_VERSION,
            "background": face.background,
            "base": b.asset("base", face.base, alpha=False),
            # drawn above the rig and under the decorators; older firmware ignores it
            "overlay": b.asset("hair", face.overlay) if face.overlay else "",
            "eyes": {
                "scleraRight": b.eye_asset("scl", face.sclera, "right", mir_right),
                "scleraLeft": b.eye_asset("scl", face.sclera, "left", mir_left),
                "irisRight": b.eye_asset("iris", face.iris, "right", mir_right),
                "irisLeft": b.eye_asset("iris", face.iris, "left", mir_left),
                "lidsUpperRight": b.asset_map("lu", face.lids_upper, mirror=mir_right, side="right"),
                "lidsUpperLeft": b.asset_map("lu", face.lids_upper, mirror=mir_left, side="left"),
                "lidsLowerRight": b.asset_map("ll", face.lids_lower, mirror=mir_right, side="right"),
                "lidsLowerLeft": b.asset_map("ll", face.lids_lower, mirror=mir_left, side="left"),
                # the rig (Face Studio); firmware that predates it ignores these keys
                "lidMode": face.lid_mode,
                "irisMask": face.iris_mask,
                # the white again, alpha only: the iris's mask on the device
                "scleraMaskLeft": b.eye_asset("sclm", face.sclera, "left", mir_left, cf="A8")
                if face.iris_mask == "sclera" else "",
                "scleraMaskRight": b.eye_asset("sclm", face.sclera, "right", mir_right, cf="A8")
                if face.iris_mask == "sclera" else "",
                "leftPos": face.left_pos,
                "rightPos": face.right_pos,
                "socketSize": face.socket_size,
                "irisTravel": face.iris_travel or comp.iris_travel,
                "irisOffsetLeft": comp.iris_fit("left")["offset"],
                "irisOffsetRight": comp.iris_fit("right")["offset"],
                **({"irisRoom": comp.iris_fit("right")["room"]} if comp.iris_fit("right")["room"] else {}),
                "irisParallax": face.iris_parallax,
                "glanceLagMs": face.glance_lag_ms,
                "clipRadius": face.clip_radius,
                "lidFill": face.lid_fill or Compositor(face).lid_fill or "",
            },
            "mouth": {
                "pos": face.mouth_pos,
                "travel": face.mouth_travel,
                "shapes": {
                    shape: [b.asset("m_%s%d" % (shape, i), src) for i, src in enumerate(frames)]
                    for shape, frames in face.mouth_shapes.items()
                },
            },
            "brows": dict({
                # left/right stay the neutral (or only) brows, for firmware without shapes
                "left": b.asset("bl", comp.brow_srcs("neutral")[0], mirror=comp.brow_srcs("neutral")[2]),
                "right": b.asset("br", comp.brow_srcs("neutral")[1], mirror=comp.brow_srcs("neutral")[3]),
                "leftPos": face.brow_left_pos,
                "rightPos": face.brow_right_pos,
                "aboveEyes": face.brows_above,
            }, **({"shapes": {
                shape: {"left": b.asset("b%s_l" % shape[:6], comp.brow_srcs(shape)[0], mirror=comp.brow_srcs(shape)[2]),
                        "right": b.asset("b%s_r" % shape[:6], comp.brow_srcs(shape)[1], mirror=comp.brow_srcs(shape)[3])}
                for shape in face.brow_shapes}} if face.brow_shapes else {})),
            "decorators": {
                name: {
                    "image": b.asset("d_%s" % name, spec.get("image")),
                    "anchor": spec.get("anchor", "face"),
                    "offset": list(spec.get("offset", [0, 0])),
                    "animate": spec.get("animate", ""),
                    "lifetimeMs": int(spec.get("lifetime_ms", 0)),
                }
                for name, spec in face.decorators.items()
            },
            "emotions": {
                name: {
                    "upper": pose.get("upper", ""),
                    "lower": pose.get("lower", ""),
                    "mouth": pose.get("mouth", ""),
                    "decorator": pose.get("decorator", ""),
                    "decorators": list(pose.get("decorators") or []),
                    "base": b.asset("bg_%s" % name[:10], pose["base"], alpha=False) if pose.get("base") else "",
                    # "" = no hair over; absent = the face's own
                    **({"overlay": b.asset("hr_%s" % name[:10], pose["overlay"]) if pose["overlay"] else ""}
                       if "overlay" in pose else {}),
                    "eyeOpen": int(pose.get("eyeOpen", 100)),
                    "brow": sided_pair(pose.get("brow"))["left"],
                    **({"browLeft": sided_pair(pose.get("brow"))["left"], "browRight": sided_pair(pose.get("brow"))["right"]}
                       if sided_pair(pose.get("brow"))["left"] != sided_pair(pose.get("brow"))["right"] else {}),
                    **({"lookLeft": sided_pair(pose.get("look"))["left"], "lookRight": sided_pair(pose.get("look"))["right"]}
                       if pose.get("look") else {}),
                    "browShape": pose.get("brow_shape", ""),
                    # per side (Face Studio); older firmware reads upper/lower/browShape only
                    "upperLeft": pose["upper_sides"]["left"], "upperRight": pose["upper_sides"]["right"],
                    "lowerLeft": pose["lower_sides"]["left"], "lowerRight": pose["lower_sides"]["right"],
                    "browShapeLeft": (pose.get("brow_shape_sides") or {}).get("left", ""),
                    "browShapeRight": (pose.get("brow_shape_sides") or {}).get("right", ""),
                    **({"motion": pose["motion"]} if pose.get("motion") else {}),
                    **({"lights": pose["lights_rgb"]} if pose.get("lights_rgb") else {}),
                    **({"lightsPulse": pose["lights_pulse"]} if pose.get("lights_rgb") and pose.get("lights_pulse") else {}),
                }
                for name, pose in face.emotions.items()
            },
        }
        written = dict(b.written)

    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return

    (out_dir / "face.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Building anywhere other than the flashed assets dir means the microSD
    # path: no rebuild, no flash, just move the card.
    to_sd = out_dir.resolve() != ASSETS_BIN.resolve()
    if not to_sd:
        invalidate_stale_assets_bin()

    total = sum(written.values())
    print("  %d sprites, %d emotions" % (len(written), len(manifest["emotions"])))
    print("  %.0f KB (%.1f%% of the 4MB assets partition)"
          % (total / 1024.0, 100.0 * total / ASSETS_PARTITION_BYTES))
    print("\nNext:")
    if to_sd:
        print("  eject the card, put it in the StackChan, power cycle")
        print("  (the card is read only during early boot -- see docs/custom-faces.md)")
    else:
        print("  cd firmware && idf.py build && idf.py flash")


def invalidate_stale_assets_bin():
    """main/CMakeLists.txt does not list assets_bin/ under DEPENDS, so without
    this a changed sprite is silently ignored and you flash the old face."""
    stale = FIRMWARE_ROOT / "build" / "generated_assets.bin"
    if stale.exists():
        stale.unlink()
        print("  removed stale build/generated_assets.bin so the next build repacks")


# --------------------------------------------------------------------------- #
#                                     Init                                     #
# --------------------------------------------------------------------------- #


def cmd_init(args):
    src = Path(args.source)
    src.mkdir(parents=True, exist_ok=True)
    target = src / "face.yaml"
    if target.exists() and not args.force:
        fail("%s already exists (use --force to overwrite)" % target)
    target.write_text(STARTER_YAML, encoding="utf-8")

    print("Created %s" % target)
    print("\nDraw these PNGs into %s (RGBA with transparency, except base):" % src)
    print("  base.png 320x240 | eye_white.png | iris.png")
    print("  lid_open/wide/tight/sad/angry.png | low_neutral/raised.png")
    print("  mouth_{n,s,f,o}_{0,1,2}.png | brow_l.png brow_r.png")
    print("\nThen:  python tools/face.py preview %s" % src)


# --------------------------------------------------------------------------- #


def main():
    ap = argparse.ArgumentParser(description="Author, preview and build StackChan sprite faces.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write a starter face.yaml")
    p.add_argument("source", help="project folder")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("preview", help="render the face without flashing")
    p.add_argument("source", help="project folder containing face.yaml")
    p.add_argument("--emotion", help="render just this emotion")
    p.add_argument("--animate", choices=["blink", "talk", "gaze"], help="render an animated GIF")
    p.add_argument("--scale", type=int, default=1, help="pixel-double the output")
    p.add_argument("--out", help="output folder (default: <source>/preview)")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("build", help="convert to .bin and write face.json")
    p.add_argument("source", help="project folder containing face.yaml")
    p.add_argument("--prefix", default="f", help="asset name prefix (default: f)")
    p.add_argument("--out", default=str(ASSETS_BIN), help="output folder")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
