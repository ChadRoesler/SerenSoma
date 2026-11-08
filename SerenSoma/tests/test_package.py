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
    assert cfg.state_dir == "~/seren-soma/state" and cfg.robot.url == "http://192.168.0.201:8080"


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
    y.write_text(f"state_dir: {str(tmp_path / 'state').replace(chr(92), '/')}\nlodestar:\n  url: http://l:1\n  token: t\n",
                 encoding="utf-8")
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
