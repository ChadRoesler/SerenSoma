"""Appendages: what's plugged into the body on top of the stock StackChan, and the plan for it.

firmware/docs/appendages.md is the design. Until the firmware has its registry this module holds the
plan: which appendages the robot should carry, on which pins, checked against the pins the base
already uses. It also passes the robot's own GET /api/appendages through once firmware answers it.
Until then the page says plainly that the robot doesn't report any.

Kept: <state>/appendages.json, the plan. Nothing here talks to the robot except that one GET.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web

# GPIOs the base firmware already uses (firmware/docs/appendages.md, "What the firmware already uses").
# An appendage can never claim these.
BASE_PINS: dict[int, str] = {
    0: "I2S MCLK / boot button", 3: "LCD CS", 4: "SD card CS", 6: "servo bus TX", 7: "servo bus RX",
    11: "internal I2C SCL", 12: "internal I2C SDA", 13: "I2S out", 14: "I2S in",
    15: "camera", 16: "camera", 19: "USB", 20: "USB", 33: "I2S WS", 34: "I2S BCLK",
    35: "LCD D/C + SD MISO", 36: "LCD/SD SCK", 37: "LCD/SD MOSI", 38: "camera", 39: "camera",
    40: "camera", 41: "camera", 42: "camera", 43: "console TX", 44: "console RX", 45: "camera",
    46: "camera", 47: "camera", 48: "camera",
}
# What's left, and where to reach it. The M-Bus side is from memory and not yet checked on the board.
FREE_PINS: dict[int, str] = {1: "Port A", 2: "Port A", 5: "M-Bus", 8: "Port B", 9: "Port B",
                             10: "M-Bus", 17: "Port C", 18: "Port C"}

# The kinds the firmware will know. "described": wired by a line of config, built from primitives the
# firmware carries. "compiled": a driver with its own protocol, built in from the Firmware tab.
KINDS: dict[str, dict] = {
    "flashlight": {
        "title": "Flashlight (M5 Unit FlashLight, AW3641E)", "how": "described",
        "pins": {"en": "pulse train / on-off", "flash": "PWM dimming and mode (only with the white-wire mod)"},
        "required": ["en"], "shareable": ["flash"],
        "power": "torch ~214 mA, flash ~584 mA for at most 220 ms (5 V)",
        "actions": ["on", "off", "level", "flash", "fade", "sweep"],
        "test": "Torch for a second, then the flash levels at 8 s spacing, then a fade if modded. Check by eye.",
    },
    "fan": {
        "title": "Fan", "how": "described",
        "pins": {"pwm": "speed, through a transistor"}, "required": ["pwm"], "shareable": [],
        "power": "depends on the fan; never from the pin directly",
        "actions": ["speed", "follow"],
        "test": "Ramps 0 to 100% and back; reports duty (and RPM if it has a tach).",
    },
    "dual_button": {
        "title": "Dual Button", "how": "described",
        "pins": {"a": "button A", "b": "button B"}, "required": ["a", "b"], "shareable": [],
        "power": "negligible", "actions": ["bind"],
        "test": "Live: every press, release and hold, timestamped, until you stop it.",
    },
    "switch": {
        "title": "Switch", "how": "described",
        "pins": {"in": "switch"}, "required": ["in"], "shareable": [],
        "power": "negligible", "actions": ["bind"],
        "test": "Live: each flip, timestamped.",
    },
    "ears": {
        "title": "LLM Module as ears (AX630C: wake word, VAD, streaming speech-to-text)", "how": "compiled",
        "pins": {"rx": "UART from the module", "tx": "UART to the module"}, "required": ["rx", "tx"], "shareable": [],
        "power": "~1.5 W, runs warm",
        "actions": ["listen", "stop", "hwinfo"],
        "test": "sys.ping, sys.hwinfo (load, memory, temperature), sys.lsmode, then 5 s of listening.",
    },
}

# The plan as of 16 Nov 2026: the parts on order, with the shared-FLASH dimming line and the pins the
# design doc sketches. Placeholders until the pin audit is checked on the board.
DEFAULT_PLAN: list[dict] = [
    {"name": "light_left", "kind": "flashlight", "on": True, "pins": {"en": 8, "flash": 5}, "opts": {"side": "left"}},
    {"name": "light_right", "kind": "flashlight", "on": True, "pins": {"en": 9, "flash": 5}, "opts": {"side": "right"}},
    {"name": "ears", "kind": "ears", "on": False, "pins": {"rx": 18, "tx": 17}, "opts": {}},
    {"name": "fan", "kind": "fan", "on": False, "pins": {"pwm": 10}, "opts": {"follows": "ears.temp"}},
    {"name": "buttons", "kind": "dual_button", "on": False, "pins": {"a": 1, "b": 2}, "opts": {"on_a": "interrupt"}},
]

NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
OPT_VALUE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
MAX_APPENDAGES = 16
MAX_OPTS = 6
CARD_LINE_MAX = 192           # soma_config.cpp's kMaxLine


class PlanError(ValueError):
    """What was sent isn't an appendage plan; the message says which part."""


def clean_one(a) -> dict:
    if not isinstance(a, dict):
        raise PlanError("each appendage must be an object")
    name, kind = a.get("name"), a.get("kind")
    if not isinstance(name, str) or not NAME.match(name):
        raise PlanError("a name is lowercase letters, digits and _, starting with a letter, at most 24")
    if kind not in KINDS:
        raise PlanError(f"{name}: kind must be one of {', '.join(KINDS)}")
    spec = KINDS[kind]
    pins = a.get("pins", {})
    if not isinstance(pins, dict):
        raise PlanError(f"{name}: pins must be an object")
    out_pins = {}
    for role, pin in pins.items():
        if role not in spec["pins"]:
            raise PlanError(f"{name}: a {kind} has no pin called {role!r} (it has {', '.join(spec['pins'])})")
        if isinstance(pin, bool) or not isinstance(pin, int) or not 0 <= pin <= 48:
            raise PlanError(f"{name}.{role}: a pin is a GPIO number from 0 to 48")
        out_pins[role] = pin
    for role in spec["required"]:
        if role not in out_pins:
            raise PlanError(f"{name}: a {kind} needs a pin for {role}")
    opts = a.get("opts", {})
    if not isinstance(opts, dict) or len(opts) > MAX_OPTS:
        raise PlanError(f"{name}: opts is an object of at most {MAX_OPTS} settings")
    for k, v in opts.items():
        if not NAME.match(str(k)) or not isinstance(v, str) or not OPT_VALUE.match(v):
            raise PlanError(f"{name}: an option is a short name and a value without spaces")
    on = a.get("on", False)
    if not isinstance(on, bool):
        raise PlanError(f"{name}: on is true or false")
    return {"name": name, "kind": kind, "on": on, "pins": out_pins, "opts": dict(opts)}


def clean_plan(body) -> list[dict]:
    if not isinstance(body, dict) or not isinstance(body.get("appendages"), list):
        raise PlanError('expected {"appendages": [...]}')
    if len(body["appendages"]) > MAX_APPENDAGES:
        raise PlanError(f"at most {MAX_APPENDAGES} appendages")
    out = [clean_one(a) for a in body["appendages"]]
    names = [a["name"] for a in out]
    if len(set(names)) != len(names):
        raise PlanError("two appendages share a name")
    return out


def card_line(a: dict) -> str:
    """The appendage as one soma.yaml line, the way the firmware will read it."""
    parts = [a["kind"]] + [f"{k}={v}" for k, v in a["pins"].items()] + [f"{k}={v}" for k, v in a["opts"].items()]
    return f"  {a['name']}: {' '.join(parts)}"


def check(plan: list[dict]) -> dict:
    """What the registry would say about this plan at boot, for the appendages switched on: who holds
    each pin, which claims it would refuse, and how much of the free pin budget is used."""
    # pin -> [(who, shareable)]. A pin may be shared only when every claim on it is the same kind's
    # shareable role (both lights' FLASH lines on one PWM: they dim together, switch separately).
    claims: dict[int, list[tuple[str, str]]] = {}
    for a in plan:
        if not a["on"]:
            continue
        shareable = KINDS[a["kind"]]["shareable"]
        for role, pin in a["pins"].items():
            claims.setdefault(pin, []).append((f"{a['name']}.{role}", f"{a['kind']}.{role}" if role in shareable else ""))
    problems = {}
    for pin, who in claims.items():
        shared_as = {s for _, s in who}
        if pin in BASE_PINS:
            problems[pin] = f"the base uses GPIO {pin} ({BASE_PINS[pin]})"
        elif len(who) > 1 and (len(shared_as) != 1 or "" in shared_as):
            problems[pin] = f"GPIO {pin} is claimed by {' and '.join(w for w, _ in who)}"
        elif pin not in FREE_PINS:
            problems[pin] = f"GPIO {pin} isn't one this board brings out"
    by_appendage = {}
    for a in plan:
        bad = [problems[p] for p in a["pins"].values() if a["on"] and p in problems]
        line = card_line(a)
        if len(line) > CARD_LINE_MAX:
            bad.append(f"its card line is {len(line)} characters; the firmware reads at most {CARD_LINE_MAX}")
        by_appendage[a["name"]] = {"ok": not bad, "problems": bad, "card": line}
    used = sorted(p for p in claims if p in FREE_PINS)
    return {
        "pins": {str(p): [w for w, _ in who] for p, who in sorted(claims.items())},
        "problems": [problems[p] for p in sorted(problems)],
        "budget": {"free": len(FREE_PINS), "used": len(used), "left": sorted(set(FREE_PINS) - set(claims))},
        "appendages": by_appendage,
    }


class Appendages:
    def __init__(self, link, state: Path):
        self.link = link
        self.file = state / "appendages.json"

    def load(self) -> list[dict]:
        try:
            return clean_plan(json.loads(self.file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return [dict(a, pins=dict(a["pins"]), opts=dict(a["opts"])) for a in DEFAULT_PLAN]

    async def robot(self) -> dict:
        """What the robot itself says it carries. Firmware without the registry has no such route."""
        url = f"{self.link.args.robot_url.rstrip('/')}/api/appendages"
        try:
            async with self.link.http.get(url, headers=dict(self.link.robot_headers()),
                                          timeout=ClientTimeout(total=4)) as r:
                if r.status == 404:
                    return {"reported": False, "why": "this firmware has no appendages yet: it needs a build with the registry"}
                if r.status != 200:
                    return {"reported": False, "why": f"the robot answered {r.status}"}
                return {"reported": True, "appendages": (await r.json()).get("appendages", [])}
        except (ClientError, asyncio.TimeoutError, ValueError) as e:
            return {"reported": False, "why": f"the robot didn't answer: {e or type(e).__name__}"}

    def answer(self, plan: list[dict], robot: dict) -> dict:
        return {"plan": plan, "check": check(plan), "kinds": KINDS, "free_pins": {str(k): v for k, v in FREE_PINS.items()},
                "robot": robot}

    async def get(self, request):
        self.link.authorized(request)
        return web.json_response(self.answer(self.load(), await self.robot()))

    async def put(self, request):
        """PUT /appendages: the whole plan, replacing what was kept. A plan with pin problems is still
        kept; they're the point of looking at it. The robot only gets it once the firmware reads it."""
        self.link.authorized(request)
        try:
            plan = clean_plan(await request.json())
        except PlanError as e:
            raise web.HTTPBadRequest(text=str(e))
        except ValueError:
            raise web.HTTPBadRequest(text="expected JSON")
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"appendages": plan}, indent=1), encoding="utf-8")
        tmp.replace(self.file)
        return web.json_response(self.answer(plan, await self.robot()))

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/appendages", self.get)
        app.router.add_put("/appendages", self.put)
