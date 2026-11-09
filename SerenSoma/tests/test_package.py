"""The package around the voice server: its card, its config, its two tokens, a check-in only the robot
gets an answer from, and a log a person can hand someone without handing over what they said."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

from seren_soma import voice
from seren_soma.config import load_config


def test_describe_is_one_line_of_json():
    out = subprocess.run([sys.executable, "-m", "seren_soma", "--describe"], capture_output=True, text=True, check=True)
    card = json.loads(out.stdout)
    assert card["name"] == "seren-soma" and card["port"] == 7480 and out.stdout.count("\n") == 1


def _cfg(tmp_path, robot="127.0.0.1"):
    y = tmp_path / "soma.yaml"
    y.write_text(f"state_dir: {str(tmp_path / 'state').replace(chr(92), '/')}\nadvertise: 10.0.0.2\n"
                 f"robot:\n  url: http://{robot}:8080\n  token: robot-secret\n"
                 "lodestar:\n  url: http://lodestar:6361\n  token: lode-secret\n", encoding="utf-8")
    return load_config(str(y))


def test_defaults_bind_the_lan_and_keep_state_at_home():
    cfg = load_config("does-not-exist.yaml")
    assert cfg.server.host == "0.0.0.0" and cfg.server.port == 7480
    assert cfg.state_dir == "~/seren-soma/state" and cfg.robot.url == "" and cfg.voice == ""
    assert cfg.lodestar.symposium_yaml == ""     # nothing of anyone's in the defaults


def test_runtime_makes_two_different_tokens(tmp_path):
    rt = _cfg(tmp_path).runtime()
    state = tmp_path / "state"
    assert (state / "token").read_text().strip() == rt.api_token
    assert (state / "device_token").read_text().strip() == rt.device_token
    assert rt.api_token != rt.device_token
    assert rt.robot_token == "robot-secret" and rt.lodestar_token == "lode-secret"
    assert rt.device_from == {"127.0.0.1"} and rt.advertise == "10.0.0.2"
    again = _cfg(tmp_path).runtime()
    assert again.api_token == rt.api_token     # made once, kept


def test_the_robot_token_defaults_to_the_state_folder(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "control_token").write_text("from-state\n", encoding="utf-8")
    y = tmp_path / "s.yaml"
    y.write_text(f"state_dir: {str(tmp_path / 'state').replace(chr(92), '/')}\nrobot:\n  url: http://127.0.0.1:8080\n"
                 "lodestar:\n  url: http://l:1\n  token: t\n", encoding="utf-8")
    assert load_config(str(y)).runtime().robot_token == "from-state"


def test_scrub_hides_what_was_said_and_tokens():
    assert voice.scrub("2026-11-07 18:00 INFO heard (1.8s): Tim Close.") == "2026-11-07 18:00 INFO heard (1.8s): [said 2 words]"
    assert voice.scrub("x heard (0.9s): hi") == "x heard (0.9s): [said 1 word]"
    assert voice.scrub("sent Authorization: Bearer abc.def-123 to x") == "sent Authorization: Bearer <token> to x"
    assert voice.scrub("night: on") == "night: on"


def _serve(cfg, coro):
    async def go():
        rt = cfg.runtime()
        link = voice.Link(rt)
        app = voice.make_app(link, "<html>the page</html>")
        async with TestClient(TestServer(app)) as client:
            return await coro(client, rt)
    return asyncio.run(go())


def test_only_the_robot_gets_a_check_in_and_never_the_api_token(tmp_path):
    async def from_the_robot(client, rt):
        r = await client.post("/ota/")
        body = await r.json()
        return r.status, body["websocket"]["token"], rt
    status, handed, rt = _serve(_cfg(tmp_path, robot="127.0.0.1"), from_the_robot)
    assert status == 200 and handed == rt.device_token and handed != rt.api_token

    async def from_someone_else(client, rt):
        r = await client.post("/ota/")
        return r.status, await r.text()
    status, text = _serve(_cfg(tmp_path, robot="10.9.9.9"), from_someone_else)
    assert status == 403 and "token" not in text


def test_the_api_takes_its_own_token_and_not_the_devices(tmp_path):
    async def go(client, rt):
        none = await client.get("/settings")
        device = await client.get("/settings", headers={"Authorization": f"Bearer {rt.device_token}"})
        api = await client.get("/settings", headers={"Authorization": f"Bearer {rt.api_token}"})
        return none.status, device.status, api.status
    assert _serve(_cfg(tmp_path), go) == (401, 401, 200)


def test_root_is_the_family_info_route(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.updates = False

    async def go(client, rt):
        r = await client.get("/")
        return r.status, await r.json()
    status, info = _serve(cfg, go)
    assert status == 200 and info["service"] == "seren-soma" and info["viewer"] == "/viewer"
    assert set(info["updates"]) == {"status", "distribution", "installed", "latest", "update_available", "detail", "checked_at"}
    assert info["updates"]["status"] == "disabled"


def test_page_and_health_are_public_logs_are_not(tmp_path):
    cfg = _cfg(tmp_path)

    async def go(client, rt):
        (tmp_path / "state" / "seren-soma.log").write_text(
            "a INFO heard (2.0s): this is private\nb INFO night: on\n", encoding="utf-8")
        page = await client.get("/viewer")
        health = await client.get("/health")
        logs_no = await client.get("/api/logs")
        logs = await client.get("/api/logs", headers={"Authorization": f"Bearer {rt.api_token}"})
        return page.status, await page.text(), (await health.json())["service"], logs_no.status, await logs.json()
    page_status, page, service, logs_no, logs = _serve(cfg, go)
    assert page_status == 200 and "the page" in page and service == "seren-soma"
    assert logs_no == 401
    assert logs["lines"] == ["a INFO heard (2.0s): [said 3 words]", "b INFO night: on"]
    assert "private" not in json.dumps(logs)


def test_no_robot_or_no_lodestar_is_said_at_startup(tmp_path):
    state = str(tmp_path / "state").replace(chr(92), "/")
    no_robot = tmp_path / "a.yaml"
    no_robot.write_text(f"state_dir: {state}\nlodestar:\n  url: http://l:1\n  token: t\n", encoding="utf-8")
    no_lode = tmp_path / "b.yaml"
    no_lode.write_text(f"state_dir: {state}\nrobot:\n  url: http://127.0.0.1:8080\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="robot.url"):
        load_config(str(no_robot)).runtime()
    with pytest.raises(SystemExit, match="lodestar.url"):
        load_config(str(no_lode)).runtime()


def test_overwhelm_is_off_and_nameless_until_set_up():
    d = voice.DEFAULT_SETTINGS["overwhelm"]
    assert d["enabled"] is False and d["pose"] == d["after"] == d["clip"] == ""
    ok = voice.merge_settings(voice.DEFAULT_SETTINGS, {"overwhelm": {"enabled": True, "pose": "flustered",
                                                                    "after": "melt-2", "clip": "gasp_1"}})
    assert ok["overwhelm"]["pose"] == "flustered" and ok["overwhelm"]["clip"] == "gasp_1"
    for bad in ("../x", "a b", "x" * 33):
        with pytest.raises(ValueError, match="is a name"):
            voice.merge_settings(voice.DEFAULT_SETTINGS, {"overwhelm": {"clip": bad}})


def test_overwhelm_shows_the_poses_its_settings_name(tmp_path, monkeypatch):
    from aiohttp import web as aweb
    posted = []

    async def emotion(request):
        posted.append(await request.json())
        return aweb.json_response({"ok": True})

    async def leds(request):
        return aweb.json_response({"ok": True})

    real_sleep = asyncio.sleep

    async def no_wait(*_):
        await real_sleep(0)

    async def go():
        robot = aweb.Application()
        robot.router.add_post("/api/emotion", emotion)
        robot.router.add_post("/api/leds", leds)
        async with TestServer(robot) as rs:
            link = voice.Link(_cfg(tmp_path).runtime())
            async with TestClient(TestServer(voice.make_app(link))):
                link.settings["overwhelm"].update(pose="flustered", after="", clip="")
                monkeypatch.setattr(voice.asyncio, "sleep", no_wait)
                await link.overwhelm(str(rs.make_url("/api")).rstrip("/"), {})
                monkeypatch.setattr(voice.asyncio, "sleep", real_sleep)
    asyncio.run(go())
    assert posted == [{"name": "flustered", "rest": True}, {"name": "neutral"}]


def test_a_blank_voice_is_left_to_lodestar(tmp_path):
    from aiohttp import web as aweb
    bodies = []

    async def speak(request):
        bodies.append(await request.json())
        return aweb.Response(body=b"RIFF")

    async def go():
        lode = aweb.Application()
        lode.router.add_post("/api/v1/speak", speak)
        async with TestServer(lode) as ls:
            cfg = _cfg(tmp_path)
            cfg.lodestar.url = str(ls.make_url("")).rstrip("/")
            link = voice.Link(cfg.runtime())
            async with TestClient(TestServer(voice.make_app(link))):
                await link.speak_wav("hi", "")
                await link.speak_wav("hi", "alto")
    asyncio.run(go())
    assert "voice" not in bodies[0] and bodies[1]["voice"] == "alto"


def test_the_page_builds_the_body_set_up_and_firmware_included():
    from pathlib import Path

    from seren_meninges.viewer import render_from_dir
    html = render_from_dir(Path(voice.__file__).parent / "viewer" / "ui", title="SerenSoma", brand="Seren<b>Soma</b>")
    for want in ('data-tab="setup"', 'data-tab="firmware"', 'id="suSend"', 'id="fwFetch"', "/robot/config", "/firmware/install"):
        assert want in html, want


def test_the_dead_air_timer_starts_from_the_yaml_and_then_is_live(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.idle_close_seconds = 45

    async def go(client, rt):
        auth = {"Authorization": f"Bearer {rt.api_token}"}
        first = (await (await client.get("/settings", headers=auth)).json())["channel"]["dead_air_seconds"]
        ok = await client.post("/settings", json={"channel": {"dead_air_seconds": 120}}, headers=auth)
        low = await client.post("/settings", json={"channel": {"dead_air_seconds": 5}}, headers=auth)
        high = await client.post("/settings", json={"channel": {"dead_air_seconds": 601}}, headers=auth)
        after = (await (await client.get("/settings", headers=auth)).json())["channel"]["dead_air_seconds"]
        return first, ok.status, low.status, await low.text(), high.status, after
    first, ok, low, why, high, after = _serve(cfg, go)
    assert first == 45 and ok == 200 and after == 120
    assert low == 400 and high == 400 and "10 to 600" in why


def test_a_saved_dead_air_beats_the_yaml_and_the_silence_watcher_reads_it_live(tmp_path):
    cfg = _cfg(tmp_path)
    rt = cfg.runtime()
    (tmp_path / "state" / "settings.json").write_text('{"channel": {"dead_air_seconds": 90}}', encoding="utf-8")
    link = voice.Link(rt)
    assert link.dead_air == 90                       # saved wins over the yaml's 60
    link.settings = voice.merge_settings(link.settings, {"channel": {"dead_air_seconds": 30}})
    assert link.dead_air == 30                       # what watch_silence reads, every second
    (tmp_path / "state" / "settings.json").unlink()
    assert voice.load_settings(7)["channel"]["dead_air_seconds"] == 10   # a yaml value is held to the range
