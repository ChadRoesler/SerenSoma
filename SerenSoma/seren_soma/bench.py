"""The bench: the robot's touch chip and neck servos as they are, and a chart of where the pads answer.

The page can't call the robot (it doesn't hold the robot's token, and shouldn't), so this server
passes a short list of the robot's own routes through: /bench/<name> is the robot's /api/<name>, behind
this server's API token. Nothing here interprets what the robot says; firmware/docs/bench.md says
what each answer means.

The touch chart is the one thing kept: <state>/touch_map.json, a list of points. Each is where a
finger was on a drawing of the head and what all 12 of the chip's channels did while it was there. It
is read back by GET /bench/map, by the page and by anyone working out which pad is where. The same
file holds a label for each of the 12 LEDs: where the one the firmware counts as number N really sits.
"""
import asyncio
import json
import time
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web

# The robot's routes the bench may call, and nothing else: this is not a general way to the robot.
ROBOT_GET = ("touch-probe", "touch-raw", "screen-touch", "servo", "status")
ROBOT_POST = ("touch-probe", "servo", "head", "leds")

CHANNELS = 12
LEDS = 12
MAX_POINTS = 600
MAX_LABEL = 60


class MapError(ValueError):
    """What was sent isn't a touch chart; the message says which part."""


def _number(value, what, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise MapError(f"{what} must be a number from {low} to {high}")
    return value


def _channels(value, what, high, whole):
    if not isinstance(value, list) or len(value) != CHANNELS:
        raise MapError(f"{what} must be a list of {CHANNELS} numbers")
    out = [_number(v, what, 0, high) for v in value]
    return [int(v) for v in out] if whole else [round(float(v), 3) for v in out]


def clean_point(p) -> dict:
    """One point of the chart, checked and cut down to what it's allowed to hold.

    x, y: where the finger was, 0..1 across and down the drawing (0,0 is back left, seen from above).
    share: for each channel, the part of the readings it was on at all, 0..1.
    peak: for each channel, the strongest it read, 0..3.
    """
    if not isinstance(p, dict):
        raise MapError("a point must be an object")
    label = p.get("label", "")
    if not isinstance(label, str) or len(label) > MAX_LABEL or "\n" in label:
        raise MapError(f"a label is one line of at most {MAX_LABEL} characters")
    out = {
        "x": round(float(_number(p.get("x"), "x", 0, 1)), 4),
        "y": round(float(_number(p.get("y"), "y", 0, 1)), 4),
        "label": label.strip(),
        "share": _channels(p.get("share"), "share", 1, whole=False),
        "peak": _channels(p.get("peak"), "peak", 3, whole=True),
        "samples": int(_number(p.get("samples"), "samples", 1, 100000)),
        "at": float(_number(p.get("at", time.time()), "at", 0, 1e11)),
    }
    sens = p.get("sensitivity")
    if sens is not None:
        if not isinstance(sens, dict) or sens.get("type") not in ("low", "high"):
            raise MapError('sensitivity must be {"type": "low" or "high", "level": 0 to 7}')
        out["sensitivity"] = {"type": sens["type"], "level": int(_number(sens.get("level"), "sensitivity.level", 0, 7))}
    return out


def clean_map(body) -> dict:
    """The whole chart as sent by the page: {"points": [...], "note": "..."}."""
    if not isinstance(body, dict) or not isinstance(body.get("points"), list):
        raise MapError('expected {"points": [...]}')
    if len(body["points"]) > MAX_POINTS:
        raise MapError(f"at most {MAX_POINTS} points")
    note = body.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise MapError("the note is text of at most 2000 characters")
    # where each LED physically sits, in the person's own words, by the number the firmware counts it as
    leds = body.get("leds", [""] * LEDS)
    if (not isinstance(leds, list) or len(leds) != LEDS
            or any(not isinstance(x, str) or len(x) > MAX_LABEL or "\n" in x for x in leds)):
        raise MapError(f"leds must be {LEDS} one-line labels of at most {MAX_LABEL} characters")
    return {"points": [clean_point(p) for p in body["points"]], "note": note, "leds": [x.strip() for x in leds]}


def summarize(points) -> dict:
    """What the chart says so far: for each channel that ever answered, how many points it answered
    at and the middle of them on the drawing. A channel that never answered isn't listed."""
    out = {}
    for c in range(CHANNELS):
        hits = [p for p in points if p["share"][c] > 0]
        if not hits:
            continue
        weight = sum(p["share"][c] for p in hits)
        out[str(c + 1)] = {
            "points": len(hits),
            "x": round(sum(p["x"] * p["share"][c] for p in hits) / weight, 3),
            "y": round(sum(p["y"] * p["share"][c] for p in hits) / weight, 3),
            "peak": max(p["peak"][c] for p in hits),
        }
    return out


class Bench:
    def __init__(self, link, state: Path):
        self.link = link
        self.map_file = state / "touch_map.json"

    def load(self) -> dict:
        try:
            return clean_map(json.loads(self.map_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {"points": [], "note": "", "leds": [""] * LEDS}

    async def map_get(self, request):
        self.link.authorized(request)
        chart = self.load()
        return web.json_response({**chart, "channels": summarize(chart["points"])})

    async def map_put(self, request):
        """PUT /bench/map: the whole chart, replacing what was kept."""
        self.link.authorized(request)
        try:
            chart = clean_map(await request.json())
        except MapError as e:
            raise web.HTTPBadRequest(text=str(e))
        except ValueError:
            raise web.HTTPBadRequest(text="expected JSON")
        tmp = self.map_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(chart, indent=1), encoding="utf-8")
        tmp.replace(self.map_file)
        return web.json_response({**chart, "channels": summarize(chart["points"])})

    async def _robot(self, request, method: str, allowed):
        self.link.authorized(request)
        what = request.match_info["what"]
        if what not in allowed:
            raise web.HTTPNotFound(text=f"the bench doesn't pass {method} {what} on to the robot")
        url = f"{self.link.args.robot_url.rstrip('/')}/api/{what}"
        body = await request.read() if method == "POST" else None
        headers = dict(self.link.robot_headers())
        if body:
            headers["Content-Type"] = "application/json"
        try:
            async with self.link.http.request(method, url, headers=headers, params=request.query, data=body,
                                              timeout=ClientTimeout(total=5)) as r:
                data = await r.read()
                if r.status == 404:
                    raise web.HTTPBadGateway(text=f"this robot's firmware has no /api/{what}: it needs a build with the bench")
                return web.Response(body=data, status=r.status, content_type=r.content_type or "text/plain")
        except (ClientError, asyncio.TimeoutError) as e:
            raise web.HTTPBadGateway(text=f"the robot didn't answer at {self.link.args.robot_url}: {e or type(e).__name__}")

    async def robot_get(self, request):
        return await self._robot(request, "GET", ROBOT_GET)

    async def robot_post(self, request):
        return await self._robot(request, "POST", ROBOT_POST)

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/bench/map", self.map_get)
        app.router.add_put("/bench/map", self.map_put)
        app.router.add_get("/bench/{what}", self.robot_get)
        app.router.add_post("/bench/{what}", self.robot_post)
