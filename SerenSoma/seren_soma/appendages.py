"""Appendages: what's plugged into the body on top of the stock StackChan, and the plan for it.

firmware/docs/appendages.md is the design. This module holds the plan: which appendages the robot
should carry, on which pins, checked against the pins the base already uses. It passes the robot's
own GET /api/appendages through, sends the plan to the robot when asked (POST /appendages/apply),
and passes one appendage's actions on to it (POST /appendages/<name>). Against firmware from before
the registry, the page says plainly that the robot doesn't report any.

Kept: <state>/appendages.json, the plan.
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
    0: "I2S MCLK / boot button", 3: "LCD CS", 4: "SD card CS", 5: "the body's infrared LED",
    6: "servo bus TX", 7: "servo bus RX", 10: "the body's infrared receiver",
    11: "internal I2C SCL", 12: "internal I2C SDA", 13: "I2S out", 14: "I2S in",
    15: "camera", 16: "camera", 19: "USB", 20: "USB", 33: "I2S WS", 34: "I2S BCLK",
    35: "LCD D/C + SD MISO", 36: "LCD/SD SCK", 37: "LCD/SD MOSI", 38: "camera", 39: "camera",
    40: "camera", 41: "camera", 42: "camera", 43: "console TX", 44: "console RX", 45: "camera",
    46: "camera", 47: "camera", 48: "camera",
}
# What's left, and where to reach it: the same six the firmware's registry allows (hal_appendages.cpp).
# G5 and G10 came off this list on 18 Nov 2026, when the body's schematic showed its infrared pair on them.
FREE_PINS: dict[int, str] = {1: "Port A", 2: "Port A", 8: "Port B", 9: "Port B", 17: "Port C", 18: "Port C"}

# The kinds. "described": wired by a line of config, built from primitives the firmware carries.
# "compiled": a driver with its own protocol, built in from the Firmware tab. "built": the firmware
# has it today; the rest are the plan, and the robot refuses a line of a kind it doesn't know.
KINDS: dict[str, dict] = {
    "flashlight": {
        "title": "Flashlight (M5 Unit FlashLight, AW3641E), stock, switch on torch", "how": "described", "built": True,
        "pins": {"en": "on, off and dimming: the yellow wire"},
        "required": ["en"], "shareable": [],
        "power": "torch ~214 mA at level 100 (5 V), in proportion below it",
        "actions": ["on", "off", "level", "chop"],
        "test": "none yet. Switch it on at a level and look. A level is a share of torch's light: 1 is 0.6%, "
                "1 to 32 (3%) is comfortable in a dark room, 43 (5%) is already too bright there, 100 is plain "
                "torch. Every on ends by itself.",
    },
    "fan": {
        "title": "Fan", "how": "described", "built": False,
        "pins": {"pwm": "speed, through a transistor"}, "required": ["pwm"], "shareable": [],
        "power": "depends on the fan; never from the pin directly",
        "actions": ["speed", "follow"],
        "test": "Ramps 0 to 100% and back; reports duty (and RPM if it has a tach).",
    },
    "dual_button": {
        "title": "Dual Button", "how": "described", "built": False,
        "pins": {"a": "button A", "b": "button B"}, "required": ["a", "b"], "shareable": [],
        "power": "negligible", "actions": ["bind"],
        "test": "Live: every press, release and hold, timestamped, until you stop it.",
    },
    "switch": {
        "title": "Switch", "how": "described", "built": False,
        "pins": {"in": "switch"}, "required": ["in"], "shareable": [],
        "power": "negligible", "actions": ["bind"],
        "test": "Live: each flip, timestamped.",
    },
    "ears": {
        "title": "LLM Module as ears (AX630C: wake word, VAD, streaming speech-to-text)", "how": "compiled", "built": False,
        "pins": {"rx": "UART from the module", "tx": "UART to the module"}, "required": ["rx", "tx"], "shareable": [],
        "power": "~1.5 W, runs warm",
        "actions": ["listen", "stop", "hwinfo"],
        "test": "sys.ping, sys.hwinfo (load, memory, temperature), sys.lsmode, then 5 s of listening.",
    },
}

# The plan a fresh install starts from: what is really plugged in. 19 Nov 2026: one stock flashlight
# on Port B, its enable on G9 (found on the bench the day before). The placeholders this list held
# until then (two lights sharing a dimming line on G5, ears, fan, buttons) are gone with it.
DEFAULT_PLAN: list[dict] = [
    {"name": "light", "kind": "flashlight", "on": True, "pins": {"en": 9}, "opts": {}},
]
# POST /appendages/<name> is an appendage's actions, so no appendage can carry a route's own name.
RESERVED_NAMES = {"apply"}

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
    if name in RESERVED_NAMES:
        raise PlanError(f"{name!r} is a route's name here; call it something else")
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


def card_value(a: dict) -> str:
    """What follows the appendage's name on its line: the kind, then its wiring and options."""
    return " ".join([a["kind"]] + [f"{k}={v}" for k, v in a["pins"].items()] + [f"{k}={v}" for k, v in a["opts"].items()])


def card_line(a: dict) -> str:
    """The appendage as one soma.yaml line, the way the firmware reads it."""
    return f"  {a['name']}: {card_value(a)}"


def robot_list(plan: list[dict]) -> dict:
    """The ticked appendages as the robot's /api/config takes them: {"name": "kind key=value ..."}.
    Raises PlanError with every reason the plan can't go as it stands."""
    chk, wrong = check(plan), []
    for a in plan:
        if not a["on"]:
            continue
        if not KINDS[a["kind"]].get("built"):
            wrong.append(f"{a['name']}: the firmware has no {a['kind']} yet, so untick it")
        wrong += [f"{a['name']}: {p}" for p in chk["appendages"][a["name"]]["problems"]]
    if wrong:
        raise PlanError("; ".join(wrong))
    return {a["name"]: card_value(a) for a in plan if a["on"]}


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

    async def _to_robot(self, path: str, body: dict):
        """POST to the robot and hand back (status, its JSON). The robot's own refusals come back as
        they are: its reasons are written for a person."""
        url = f"{self.link.args.robot_url.rstrip('/')}{path}"
        try:
            async with self.link.http.post(url, headers=dict(self.link.robot_headers()), json=body,
                                           timeout=ClientTimeout(total=8)) as r:
                try:
                    return r.status, await r.json(content_type=None)
                except ValueError:
                    return r.status, None       # not JSON: firmware with no such route answers in plain text
        except (ClientError, asyncio.TimeoutError) as e:
            raise web.HTTPBadGateway(text=f"the robot didn't answer: {e or type(e).__name__}")

    async def apply(self, request):
        """POST /appendages/apply: save the ticked appendages on the robot as its whole list, and restart
        it, since the registry reads the list at boot. Refused, with the reasons, while a ticked one has a
        pin problem or is of a kind the firmware doesn't have: the robot would only refuse it at boot."""
        self.link.authorized(request)
        plan = self.load()
        try:
            lines = robot_list(plan)
        except PlanError as e:
            raise web.HTTPBadRequest(text=str(e))
        status, answer = await self._to_robot("/api/config", {"appendages": lines, "reboot": True})
        if status == 404:
            raise web.HTTPBadGateway(text="this robot's firmware has no /api/config: update it first")
        if status != 200 or "appendages" not in (answer or {}).get("changed", []):
            raise web.HTTPBadGateway(text="the robot didn't take the list: its firmware is from before the "
                                          "appendage registry, or it answered " + str(status))
        return web.json_response({"ok": True, "sent": lines, "rebooting": bool(answer.get("rebooting"))})

    async def act(self, request):
        """POST /appendages/<name> {"action": "on", "level": 24, "ms": 5000}: one of that appendage's own
        actions, passed to the robot as it is. The robot's answer and status come back unchanged."""
        self.link.authorized(request)
        name = request.match_info["name"]
        if not NAME.match(name):
            raise web.HTTPBadRequest(text="no appendage has a name like that")
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="expected JSON")
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            raise web.HTTPBadRequest(text='expected {"action": "..."}')
        status, answer = await self._to_robot(f"/api/appendages/{name}", body)
        if answer is None:
            raise web.HTTPBadGateway(text="this firmware has no appendages yet: it needs a build with the registry"
                                     if status == 404 else f"the robot answered {status} with something that isn't JSON")
        return web.json_response(answer, status=status)

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/appendages", self.get)
        app.router.add_put("/appendages", self.put)
        app.router.add_post("/appendages/apply", self.apply)
        app.router.add_post("/appendages/{name}", self.act)
