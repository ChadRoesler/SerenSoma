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


def test_two_lights_cant_share_an_enable():
    clash = ap.check(ap.clean_plan({"appendages": [_light("a", 8), _light("b", 8)]}))
    assert clash["problems"] == ["GPIO 8 is claimed by a.en and b.en"]
    assert not clash["appendages"]["a"]["ok"] and not clash["appendages"]["b"]["ok"]


def test_a_stock_flashlight_has_no_dimming_line():
    # the white-wire mod isn't built, and the firmware refuses a line with flash= in it
    with pytest.raises(ap.PlanError, match="no pin called 'flash'"):
        ap.clean_plan({"appendages": [_light("a", 8, 5)]})


def test_the_bodys_infrared_pins_are_not_free():
    plan = [{"name": "fan", "kind": "fan", "on": True, "pins": {"pwm": 10}, "opts": {}},
            {"name": "sw", "kind": "switch", "on": True, "pins": {"in": 5}, "opts": {}}]
    assert ap.check(ap.clean_plan({"appendages": plan}))["problems"] == [
        "the base uses GPIO 5 (the body's infrared LED)", "the base uses GPIO 10 (the body's infrared receiver)"]
    assert set(ap.FREE_PINS) == {1, 2, 8, 9, 17, 18}


def test_the_default_plan_is_what_is_plugged_in():
    assert ap.robot_list(ap.clean_plan({"appendages": ap.DEFAULT_PLAN})) == {"light": "flashlight en=9"}


def test_what_goes_to_the_robot_is_the_ticked_ones_and_only_if_they_can_work():
    off = {"name": "fan", "kind": "fan", "on": False, "pins": {"pwm": 1}, "opts": {}}
    assert ap.robot_list(ap.clean_plan({"appendages": [_light("light", 9), off]})) == {"light": "flashlight en=9"}
    with pytest.raises(ap.PlanError, match="the firmware has no fan yet"):
        ap.robot_list(ap.clean_plan({"appendages": [_light("light", 9), dict(off, on=True)]}))
    with pytest.raises(ap.PlanError, match="a: GPIO 8 is claimed by a.en and b.en"):
        ap.robot_list(ap.clean_plan({"appendages": [_light("a", 8), _light("b", 8)]}))
    assert ap.robot_list(ap.clean_plan({"appendages": [_light("light", 9, on=False)]})) == {}
    with pytest.raises(ap.PlanError, match="route's name"):
        ap.clean_plan({"appendages": [_light("apply", 9)]})


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

def _serve(tmp_path, go, robot_has_appendages=False, seen=None):
    """seen: a list the stand-in robot appends (path, token header, body) to, for every POST it gets."""
    async def listing(request):
        return web.json_response({"appendages": [{"name": "light_left", "kind": "flashlight", "ok": True}]})

    async def config(request):
        body = await request.json()
        seen.append((request.path, request.headers.get("X-StackChan-Token"), body))
        return web.json_response({"ok": True, "changed": ["appendages"] if "appendages" in body else [], "rebooting": True})

    async def act(request):
        body = await request.json()
        seen.append((request.path, request.headers.get("X-StackChan-Token"), body))
        if request.match_info["name"] != "light":
            return web.json_response({"ok": False, "error": "no appendage is called that"}, status=404)
        if body.get("level", 1) > 100:
            return web.json_response({"ok": False, "error": "level is 1 to 100"}, status=400)
        return web.json_response({"ok": True, "name": "light", "state": {"on": body["action"] != "off"}})

    async def run():
        robot = web.Application()
        if robot_has_appendages:
            robot.router.add_get("/api/appendages", listing)
            robot.router.add_post("/api/config", config)
            robot.router.add_post("/api/appendages/{name}", act)
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


def test_send_to_the_robot_saves_the_ticked_ones_and_restarts_it(tmp_path):
    seen = []

    async def go(client, auth, state):
        sent = await client.post("/appendages/apply", headers=auth)
        await client.put("/appendages", headers=auth, json={"appendages": [_light("a", 8), _light("b", 8)]})
        clash = await client.post("/appendages/apply", headers=auth)
        return sent.status, await sent.json(), clash.status, await clash.text(), (await client.post("/appendages/apply")).status
    status, body, clash, why, unasked = _serve(tmp_path, go, robot_has_appendages=True, seen=seen)
    assert (status, body) == (200, {"ok": True, "sent": {"light": "flashlight en=9"}, "rebooting": True})
    assert seen == [("/api/config", "robot-secret", {"appendages": {"light": "flashlight en=9"}, "reboot": True})]
    assert clash == 400 and "GPIO 8 is claimed" in why and unasked == 401      # a plan that can't work never reaches it


def test_an_action_goes_to_the_robot_and_its_answer_comes_back_as_it_is(tmp_path):
    seen = []

    async def go(client, auth, state):
        on = await client.post("/appendages/light", headers=auth, json={"action": "on", "level": 24, "ms": 5000})
        too = await client.post("/appendages/light", headers=auth, json={"action": "on", "level": 150})
        nobody = await client.post("/appendages/lamp", headers=auth, json={"action": "on"})
        shapeless = await client.post("/appendages/light", headers=auth, json={"level": 3})
        unasked = await client.post("/appendages/light", json={"action": "off"})
        return (on.status, await on.json(), too.status, await too.json(), nobody.status, await nobody.json(),
                shapeless.status, unasked.status)
    on, on_body, too, too_body, nobody, nobody_body, shapeless, unasked = _serve(
        tmp_path, go, robot_has_appendages=True, seen=seen)
    assert (on, on_body) == (200, {"ok": True, "name": "light", "state": {"on": True}})
    assert seen[0] == ("/api/appendages/light", "robot-secret", {"action": "on", "level": 24, "ms": 5000})
    assert (too, too_body["error"]) == (400, "level is 1 to 100")          # the robot's own reason, untouched
    assert (nobody, nobody_body["error"]) == (404, "no appendage is called that")
    assert (shapeless, unasked) == (400, 401) and len(seen) == 3            # neither of those reached the robot


def test_firmware_from_before_the_registry_is_said_plainly_for_actions_too(tmp_path):
    async def go(client, auth, state):
        r = await client.post("/appendages/light", headers=auth, json={"action": "on"})
        a = await client.post("/appendages/apply", headers=auth)
        return r.status, await r.text(), a.status
    status, text, apply_status = _serve(tmp_path, go)
    assert status == 502 and "needs a build with the registry" in text and apply_status == 502

