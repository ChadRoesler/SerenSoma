"""Appendages: the plan kept in the state folder, checked against the pins the base uses, and the
robot's own list passed through once firmware has one."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from seren_soma import appendages as ap, voice
from seren_soma.config import load_config


def _light(name, en, flash=None, on=True):
    pins = {"en": en} | ({"flash": flash} if flash is not None else {})
    return {"name": name, "kind": "flashlight", "on": on, "pins": pins, "opts": {}}


# -- the plan ------------------------------------------------------------------------------------

def test_the_default_plan_is_clean_and_fits_the_budget():
    plan = ap.clean_plan({"appendages": ap.DEFAULT_PLAN})
    c = ap.check(plan)
    assert c["problems"] == [] and all(a["ok"] for a in c["appendages"].values())
    assert c["budget"]["used"] <= c["budget"]["free"]


@pytest.mark.parametrize("bad, says", [
    ({"name": "Left", "kind": "flashlight", "pins": {"en": 8}}, "lowercase"),
    ({"name": "x", "kind": "taser", "pins": {}}, "kind must be"),
    ({"name": "x", "kind": "flashlight", "pins": {}}, "needs a pin for en"),
    ({"name": "x", "kind": "flashlight", "pins": {"en": 8, "beam": 9}}, "no pin called 'beam'"),
    ({"name": "x", "kind": "flashlight", "pins": {"en": 49}}, "GPIO number"),
    ({"name": "x", "kind": "flashlight", "pins": {"en": True}}, "GPIO number"),
    ({"name": "x", "kind": "fan", "pins": {"pwm": 10}, "opts": {"follows": "a b"}}, "without spaces"),
])
def test_a_plan_that_isnt_one_is_refused_and_says_why(bad, says):
    with pytest.raises(ap.PlanError, match=says):
        ap.clean_plan({"appendages": [bad]})


def test_two_with_one_name_are_refused():
    with pytest.raises(ap.PlanError, match="share a name"):
        ap.clean_plan({"appendages": [_light("l", 8), _light("l", 9)]})


def test_both_lights_may_share_one_dimming_line_but_not_an_enable():
    shared = ap.check(ap.clean_plan({"appendages": [_light("a", 8, 5), _light("b", 9, 5)]}))
    assert shared["problems"] == [] and shared["pins"]["5"] == ["a.flash", "b.flash"]
    clash = ap.check(ap.clean_plan({"appendages": [_light("a", 8), _light("b", 8)]}))
    assert clash["problems"] == ["GPIO 8 is claimed by a.en and b.en"]
    assert not clash["appendages"]["a"]["ok"] and not clash["appendages"]["b"]["ok"]


def test_a_dimming_line_cant_share_with_something_else():
    plan = [_light("a", 8, 10), {"name": "fan", "kind": "fan", "on": True, "pins": {"pwm": 10}, "opts": {}}]
    assert ap.check(ap.clean_plan({"appendages": plan}))["problems"] == ["GPIO 10 is claimed by a.flash and fan.pwm"]


def test_base_pins_and_pins_the_board_doesnt_bring_out_are_refused():
    c = ap.check(ap.clean_plan({"appendages": [_light("a", 6), _light("b", 21)]}))
    assert c["problems"] == ["the base uses GPIO 6 (servo bus TX)", "GPIO 21 isn't one this board brings out"]


def test_an_appendage_switched_off_claims_nothing():
    c = ap.check(ap.clean_plan({"appendages": [_light("a", 8), _light("b", 8, on=False)]}))
    assert c["problems"] == [] and c["pins"] == {"8": ["a.en"]} and c["appendages"]["b"]["ok"]


def test_the_card_line_is_what_the_firmware_will_read():
    a = ap.clean_one({"name": "fan", "kind": "fan", "on": True, "pins": {"pwm": 10}, "opts": {"follows": "ears.temp"}})
    assert ap.card_line(a) == "  fan: fan pwm=10 follows=ears.temp"


# -- the routes ----------------------------------------------------------------------------------

def _serve(tmp_path, go, robot_has_appendages=False):
    async def listing(request):
        return web.json_response({"appendages": [{"name": "light_left", "kind": "flashlight", "ok": True}]})

    async def run():
        robot = web.Application()
        if robot_has_appendages:
            robot.router.add_get("/api/appendages", listing)
        async with TestServer(robot) as robot_server:
            state = tmp_path / "state"
            y = tmp_path / "soma.yaml"
            y.write_text(f"state_dir: {str(state).replace(chr(92), '/')}\nadvertise: 10.0.0.2\n"
                         f"robot:\n  url: {str(robot_server.make_url('')).rstrip('/')}\n  token: robot-secret\n"
                         "lodestar:\n  url: http://lodestar:6361\n  token: t\n", encoding="utf-8")
            link = voice.Link(load_config(str(y)).runtime())
            async with TestClient(TestServer(voice.make_app(link))) as client:
                return await go(client, {"Authorization": f"Bearer {link.token}"}, state)
    return asyncio.run(run())


def test_the_plan_is_kept_and_the_robot_without_a_registry_is_said_plainly(tmp_path):
    async def go(client, auth, state):
        first = await (await client.get("/appendages", headers=auth)).json()
        bad = await client.put("/appendages", headers=auth, json={"appendages": [{"name": "x", "kind": "taser"}]})
        put = await client.put("/appendages", headers=auth, json={"appendages": [_light("a", 8), _light("b", 8)]})
        again = await (await client.get("/appendages", headers=auth)).json()
        unasked = await client.get("/appendages")
        return first, bad.status, put.status, again, unasked.status, state
    first, bad, put, again, unasked, state = _serve(tmp_path, go)
    assert [a["name"] for a in first["plan"]] == [a["name"] for a in ap.DEFAULT_PLAN]
    assert first["robot"] == {"reported": False, "why": "this firmware has no appendages yet: it needs a build with the registry"}
    assert (bad, put, unasked) == (400, 200, 401)
    assert again["check"]["problems"] == ["GPIO 8 is claimed by a.en and b.en"]     # kept anyway: that's the point
    assert len(json.loads((state / "appendages.json").read_text(encoding="utf-8"))["appendages"]) == 2


def test_the_robots_own_list_is_passed_through_once_it_has_one(tmp_path):
    async def go(client, auth, state):
        return await (await client.get("/appendages", headers=auth)).json()
    body = _serve(tmp_path, go, robot_has_appendages=True)
    assert body["robot"] == {"reported": True, "appendages": [{"name": "light_left", "kind": "flashlight", "ok": True}]}
