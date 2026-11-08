"""Setting a robot up from the voice server: the blanks it fills, the card file it writes, and the one
step that can lock us out (a new token) done in the order that can't."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from seren_soma import robot_setup as rs
from seren_soma import voice
from seren_soma.config import load_config


# -- the keys ------------------------------------------------------------------------------------

def test_voice_defaults_to_this_box():
    assert rs.keys_from({}, advertise="10.0.0.2", port=7480) == {"voice": "http://10.0.0.2:7480/ota/"}
    assert rs.keys_from({"voice": "auto"}, advertise="10.0.0.2", port=7480)["voice"] == "http://10.0.0.2:7480/ota/"
    assert rs.keys_from({"voice": "nuc:7480"}, advertise="x", port=1)["voice"] == "nuc:7480"
    assert "voice" not in rs.keys_from({"voice": None}, advertise="x", port=1)


def test_a_token_in_the_request_is_ignored():
    # the token is this box's to choose, never the caller's
    assert "token" not in rs.keys_from({"token": "mine"}, advertise="x", port=1)


@pytest.mark.parametrize("body, says", [
    ({"wifi": {"password": "p"}}, "ssid"),
    ({"wifi": {"ssid": "a\nb"}}, "line break"),
    ({"wifi": {"ssid": "x" * 33}}, "wifi.ssid"),
    ({"tailnet": {"key": "k" * 129}}, "tailnet.key"),
    ({"voice": "two words"}, "no spaces"),
    ({"wifi": "home"}, "object"),
    ([], "JSON object"),
])
def test_what_the_robot_cant_take_is_refused_by_name(body, says):
    with pytest.raises(rs.SetupError, match=says):
        rs.keys_from(body, advertise="x", port=1)


# -- the card file -------------------------------------------------------------------------------

def _read_like_the_robot(text: str) -> dict[str, str]:
    """soma_config.cpp's parser, in Python: key: value, one level of nesting, # comments, a quoted
    value taken whole up to the matching quote, no escapes."""
    out, section = {}, ""
    for raw in text.splitlines():
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        key, _, rest = body.partition(":")
        key, v = key.strip().lower(), rest.strip()
        if v[:1] in ("'", '"') and v.find(v[0], 1) > 0:
            v = v[1:v.find(v[0], 1)]
        elif v.startswith("#"):
            v = ""
        elif " #" in v:
            v = v[:v.index(" #")].strip()
        if raw[:1] not in (" ", "\t"):
            if key in ("wifi", "tailnet") and not v:
                section = key
                continue
            section = ""
        out[f"{section}.{key}" if section else key] = v
    return out


def test_the_card_reads_back_the_way_the_robot_reads_it():
    keys = rs.keys_from({"wifi": {"ssid": "Home #2", "password": 'say "hi"'}}, advertise="10.0.0.2", port=7480)
    text = rs.card_text(keys, "tok-123")
    assert _read_like_the_robot(text) == {"wifi.ssid": "Home #2", "wifi.password": 'say "hi"',
                                          "voice": "http://10.0.0.2:7480/ota/", "token": "tok-123"}


def test_a_card_without_wifi_leaves_the_robots_network_alone():
    text = rs.card_text(rs.keys_from({}, advertise="h", port=1), "t")
    assert "wifi.ssid" not in _read_like_the_robot(text) and "# wifi:" in text


def test_a_value_with_both_quotes_cant_go_on_a_card():
    with pytest.raises(rs.SetupError, match="wifi.txt"):
        rs.card_text({"wifi": {"ssid": "a", "password": "it's \"x\""}}, "t")


def test_a_new_token_fits_the_firmware_and_needs_no_quoting():
    t = rs.new_token()
    assert len(t) <= rs.CAPS["token"] and "'" not in t and '"' not in t and " " not in t


def test_writing_a_token_keeps_the_old_one(tmp_path):
    f = tmp_path / "control_token"
    rs.write_token(f, "first")
    rs.write_token(f, "second")
    assert f.read_text() == "second" and (tmp_path / "control_token.previous").read_text() == "first"
    assert not (tmp_path / "control_token.new").exists()


# -- against a robot -----------------------------------------------------------------------------

def _cfg(tmp_path, robot_url, token_in_file=True):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    robot = f"robot:\n  url: {robot_url}\n"
    if token_in_file:
        (state / "control_token").write_text("old-token\n", encoding="utf-8")
    else:
        robot += "  token: old-token\n"
    y = tmp_path / "soma.yaml"
    y.write_text(f"state_dir: {str(state).replace(chr(92), '/')}\nadvertise: 10.0.0.2\n{robot}"
                 "lodestar:\n  url: http://lodestar:6361\n  token: t\n", encoding="utf-8")
    return load_config(str(y)), state


def _with_robot(tmp_path, robot_answers, go, token_in_file=True):
    """Serve a fake robot and the voice server pointed at it; run go(client, api_headers, link, seen)."""
    seen = []

    async def api_config(request):
        seen.append({"token": request.headers.get("X-StackChan-Token"), "body": await request.json()})
        status, body = robot_answers(seen[-1])
        return web.json_response(body, status=status)

    async def run():
        robot = web.Application()
        robot.router.add_post("/api/config", api_config)
        async with TestServer(robot) as robot_server:
            cfg, state = _cfg(tmp_path, str(robot_server.make_url("")).rstrip("/"), token_in_file)
            link = voice.Link(cfg.runtime())
            async with TestClient(TestServer(voice.make_app(link))) as client:
                auth = {"Authorization": f"Bearer {link.token}"}
                return await go(client, auth, link, seen, state)
    return asyncio.run(run())


def _saves_everything(seen):
    body = seen["body"]
    names = [k for k in ("voice", "token") if k in body] + (["wifi"] if "wifi" in body else [])
    return 200, {"ok": True, "changed": names, "rebooting": bool(body.get("reboot"))}


def test_a_new_token_goes_to_the_robot_first_then_gets_written_down(tmp_path):
    async def go(client, auth, link, seen, state):
        r = await client.post("/robot/config", headers=auth,
                              json={"rotate_token": True, "reboot": False, "wifi": {"ssid": "Home", "password": "pw"}})
        return r.status, await r.text(), seen, state, link
    status, text, seen, state, link = _with_robot(tmp_path, _saves_everything, go)
    assert status == 200, text
    asked = seen[0]
    new = (state / "control_token").read_text()
    assert asked["token"] == "old-token"                       # asked with the token the robot has now
    assert asked["body"]["token"] == new != "old-token"        # handed the one now written down
    assert asked["body"]["reboot"] is True                     # a new token always reboots onto it
    assert asked["body"]["voice"] == "http://10.0.0.2:7480/ota/"
    assert (state / "control_token.previous").read_text().strip() == "old-token"
    assert link.robot_headers()["X-StackChan-Token"] == new    # the loops' shared headers too
    answer = json.loads(text)
    assert answer["token_rotated"] and answer["changed"] == ["voice", "token", "wifi"]
    assert new not in text and "pw" not in text                # nothing secret comes back


def test_a_robot_that_says_no_changes_nothing_here(tmp_path):
    async def go(client, auth, link, seen, state):
        r = await client.post("/robot/config", headers=auth, json={"rotate_token": True})
        return r.status, (state / "control_token").read_text().strip(), link.robot_token
    status, on_disk, held = _with_robot(tmp_path, lambda s: (401, {"error": "bad token"}), go)
    assert status == 502 and on_disk == "old-token" and held == "old-token"


def test_a_robot_that_didnt_save_the_token_changes_nothing_here(tmp_path):
    async def go(client, auth, link, seen, state):
        r = await client.post("/robot/config", headers=auth, json={"rotate_token": True})
        return r.status, (state / "control_token").read_text().strip()
    status, on_disk = _with_robot(tmp_path, lambda s: (200, {"ok": True, "changed": ["voice"], "rebooting": True}), go)
    assert status == 502 and on_disk == "old-token"


def test_old_firmware_is_told_what_to_do(tmp_path):
    async def go(client, auth, link, seen, state):
        r = await client.post("/robot/config", headers=auth, json={})
        return r.status, await r.text()
    status, text = _with_robot(tmp_path, lambda s: (404, {}), go)
    assert status == 502 and "update it" in text


def test_a_token_that_isnt_in_a_file_cant_be_rotated(tmp_path):
    async def go(client, auth, link, seen, state):
        r = await client.post("/robot/config", headers=auth, json={"rotate_token": True})
        return r.status, seen
    status, seen = _with_robot(tmp_path, _saves_everything, go, token_in_file=False)
    assert status == 409 and seen == []                        # the robot was never asked


def test_setup_needs_the_api_token(tmp_path):
    async def go(client, auth, link, seen, state):
        a = await client.post("/robot/config", json={})
        b = await client.post("/robot/soma.yaml", json={},
                              headers={"Authorization": f"Bearer {link.device_token}"})
        return a.status, b.status, seen
    assert _with_robot(tmp_path, _saves_everything, go) == (401, 401, [])


def test_the_card_carries_the_token_this_box_holds(tmp_path):
    async def go(client, auth, link, seen, state):
        r = await client.post("/robot/soma.yaml", headers=auth, json={"wifi": {"ssid": "Home", "password": "pw"}})
        return r.status, r.headers.get("Content-Disposition", ""), await r.text()
    status, disposition, text = _with_robot(tmp_path, _saves_everything, go)
    assert status == 200 and "soma.yaml" in disposition
    assert _read_like_the_robot(text)["token"] == "old-token"


def test_a_card_for_a_robot_with_no_token_makes_one_and_keeps_it(tmp_path):
    async def go(client, auth, link, seen, state):
        (state / "control_token").unlink()
        link.robot_token = ""
        r = await client.post("/robot/soma.yaml", headers=auth, json={})
        return await r.text(), (state / "control_token").read_text(), link.robot_token
    text, on_disk, held = _with_robot(tmp_path, _saves_everything, go)
    assert _read_like_the_robot(text)["token"] == on_disk == held and len(held) > 20
