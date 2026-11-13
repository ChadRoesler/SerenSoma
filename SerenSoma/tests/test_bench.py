"""The bench: a short list of the robot's routes passed through behind this box's token, and the touch
chart kept in the state folder."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from seren_soma import bench, voice
from seren_soma.config import load_config


def _point(**over):
    p = {"x": 0.5, "y": 0.2, "label": "back middle", "share": [1, 0.25] + [0] * 10, "peak": [3, 1] + [0] * 10,
         "samples": 20, "at": 1794440000.0, "sensitivity": {"type": "low", "level": 3}}
    p.update(over)
    return p


# -- the chart -----------------------------------------------------------------------------------

def test_a_point_keeps_what_every_channel_did():
    p = bench.clean_point(_point(extra="dropped"))
    assert p["share"][:2] == [1.0, 0.25] and p["peak"][:2] == [3, 1] and len(p["share"]) == 12
    assert p["sensitivity"] == {"type": "low", "level": 3} and "extra" not in p


@pytest.mark.parametrize("change, says", [
    ({"x": 1.2}, "x must be"),
    ({"y": True}, "y must be"),
    ({"share": [0] * 11}, "share must be a list of 12"),
    ({"share": [2] + [0] * 11}, "share must be"),
    ({"peak": [4] + [0] * 11}, "peak must be"),
    ({"label": "two\nlines"}, "label"),
    ({"samples": 0}, "samples"),
    ({"sensitivity": {"type": "medium", "level": 3}}, "sensitivity"),
    ({"sensitivity": {"type": "high", "level": 8}}, "sensitivity.level"),
])
def test_what_isnt_a_point_is_refused_by_name(change, says):
    with pytest.raises(bench.MapError, match=says):
        bench.clean_point(_point(**change))


def test_a_chart_is_points_and_has_a_ceiling():
    with pytest.raises(bench.MapError, match="points"):
        bench.clean_map({"point": []})
    with pytest.raises(bench.MapError, match="at most"):
        bench.clean_map({"points": [_point()] * (bench.MAX_POINTS + 1)})
    assert bench.clean_map({"points": []}) == {"points": [], "note": "", "leds": [""] * 12}


def test_the_chart_keeps_where_each_led_sits():
    leds = ["left rail, top"] + [""] * 11
    assert bench.clean_map({"points": [], "leds": leds})["leds"] == leds
    with pytest.raises(bench.MapError, match="leds must be 12"):
        bench.clean_map({"points": [], "leds": ["only one"]})


def test_the_summary_says_where_each_channel_answers_and_skips_the_silent_ones():
    points = [bench.clean_point(_point(x=0.2, y=0.1, share=[1] + [0] * 11, peak=[3] + [0] * 11)),
              bench.clean_point(_point(x=0.8, y=0.1, share=[1] + [0] * 11, peak=[2] + [0] * 11)),
              bench.clean_point(_point(x=0.5, y=0.9, share=[0, 0, 0.5] + [0] * 9, peak=[0, 0, 1] + [0] * 9)),
              bench.clean_point(_point(x=0.5, y=0.5, share=[0] * 12, peak=[0] * 12))]
    s = bench.summarize(points)
    assert set(s) == {"1", "3"}                       # channel 2 never answered; a dead spot adds nothing
    assert s["1"] == {"points": 2, "x": 0.5, "y": 0.1, "peak": 3}
    assert s["3"] == {"points": 1, "x": 0.5, "y": 0.9, "peak": 1}


# -- the routes ----------------------------------------------------------------------------------

def _with_robot(tmp_path, go, routes=None):
    """A fake robot and the voice server pointed at it; go(client, auth, seen, state)."""
    seen = []

    async def probe(request):
        seen.append({"method": request.method, "path": request.path, "query": dict(request.query),
                     "token": request.headers.get("X-StackChan-Token"), "body": await request.text()})
        return web.json_response({"ok": True, "channels": [3, 0, 0] + [0] * 9})

    async def run():
        robot = web.Application()
        for method, path in routes or [("GET", "/api/touch-probe"), ("POST", "/api/touch-probe"), ("POST", "/api/head"),
                                       ("GET", "/api/screen-touch"), ("POST", "/api/leds")]:
            robot.router.add_route(method, path, probe)
        async with TestServer(robot) as robot_server:
            state = tmp_path / "state"
            y = tmp_path / "soma.yaml"
            y.write_text(f"state_dir: {str(state).replace(chr(92), '/')}\nadvertise: 10.0.0.2\n"
                         f"robot:\n  url: {str(robot_server.make_url('')).rstrip('/')}\n  token: robot-secret\n"
                         "lodestar:\n  url: http://lodestar:6361\n  token: t\n", encoding="utf-8")
            link = voice.Link(load_config(str(y)).runtime())
            async with TestClient(TestServer(voice.make_app(link))) as client:
                return await go(client, {"Authorization": f"Bearer {link.token}"}, seen, state)
    return asyncio.run(run())


def test_the_bench_passes_the_robots_answer_through_with_the_robots_token(tmp_path):
    async def go(client, auth, seen, state):
        r = await client.get("/bench/touch-probe", headers=auth)
        tuned = await client.post("/bench/touch-probe", headers=auth, json={"type": "high", "level": 5})
        screen = await client.get("/bench/screen-touch", headers=auth)
        leds = await client.post("/bench/leds", headers=auth, json={"index": 3, "r": 60, "g": 60, "b": 60})
        return r.status, await r.json(), tuned.status, seen, screen.status, leds.status
    status, body, tuned, seen, screen, leds = _with_robot(tmp_path, go)
    assert status == 200 and body["channels"][0] == 3 and tuned == 200 and (screen, leds) == (200, 200)
    assert {s["token"] for s in seen} == {"robot-secret"} and len(seen) == 4      # the page never held it
    assert json.loads(seen[1]["body"]) == {"type": "high", "level": 5}


def test_the_bench_needs_this_boxes_token_and_passes_only_its_list(tmp_path):
    async def go(client, auth, seen, state):
        none = await client.get("/bench/touch-probe")
        reboot = await client.post("/bench/reboot", headers=auth, json={})
        face = await client.get("/bench/photo", headers=auth)
        return none.status, reboot.status, face.status, seen
    none, reboot, face, seen = _with_robot(tmp_path, go)
    assert (none, reboot, face) == (401, 404, 404) and seen == []       # nothing reached the robot


def test_old_firmware_and_a_robot_thats_off_are_said_plainly(tmp_path):
    async def go(client, auth, seen, state):
        r = await client.get("/bench/servo", headers=auth)      # this fake robot has no /api/servo
        return r.status, await r.text()
    status, text = _with_robot(tmp_path, go)
    assert status == 502 and "needs a build with the bench" in text


def test_the_chart_is_kept_and_read_back_with_its_summary(tmp_path):
    async def go(client, auth, seen, state):
        empty = await (await client.get("/bench/map", headers=auth)).json()
        bad = await client.put("/bench/map", headers=auth, json={"points": [_point(x=7)]})
        put = await client.put("/bench/map", headers=auth, json={"points": [_point()], "note": "top pad"})
        again = await (await client.get("/bench/map", headers=auth)).json()
        unasked = await client.get("/bench/map")
        return empty, bad.status, await bad.text(), put.status, again, unasked.status, state
    empty, bad, why, put, again, unasked, state = _with_robot(tmp_path, go)
    assert empty == {"points": [], "note": "", "leds": [""] * 12, "channels": {}}
    assert bad == 400 and "x must be" in why and put == 200 and unasked == 401
    assert again["note"] == "top pad" and again["channels"]["1"]["points"] == 1
    assert json.loads((state / "touch_map.json").read_text(encoding="utf-8"))["points"][0]["label"] == "back middle"
