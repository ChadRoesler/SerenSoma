"""Firmware for the robot from the voice server: an image checked before it's kept, a release checked
against its sums, an offer made once on the check-in, and the image only for the robot."""
from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from seren_soma import firmware as fw
from seren_soma import voice
from seren_soma.config import load_config

APP = bytes([fw.IMAGE_MAGIC]) + b"\x05" * 4096          # enough like an app image for the checks


def _zip(v="0.1.1", app=APP):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"soma-{v}-update/stack-chan.bin", app)
        z.writestr(f"soma-{v}-update/flash_args", "--flash-mode dio\n0x20000 stack-chan.bin\n")
    return buf.getvalue()


def _release(v="0.1.1", **kw):
    names = [f"soma-{v}-update.zip", "SHA256SUMS", f"soma-{v}-full.bin"]
    return {"tag_name": f"fw-v{v}", "draft": False, "prerelease": False, "published_at": kw.get("at", "2026-11-08"),
            "assets": [{"name": n, "browser_download_url": f"https://x/{n}"} for n in names]}


# -- the checks ----------------------------------------------------------------------------------

@pytest.mark.parametrize("data, says", [
    (b"PK\x03\x04" + b"0" * 2000, "isn't an ESP32 app image"),
    (APP[:100], "too small"),
    (bytes([fw.IMAGE_MAGIC]) + b"0" * fw.MAX_APP_BYTES, "app slot holds"),
])
def test_what_isnt_an_app_image_is_refused(data, says):
    with pytest.raises(fw.FirmwareError, match=says):
        fw.check_image(data)


def test_versions_take_the_tag_or_the_number():
    assert fw.check_version("fw-v0.1.1") == "0.1.1" == fw.check_version("0.1.1")
    assert fw.check_version("0.2.0-mine") == "0.2.0-mine"
    for bad in ("", "../etc", "a b", "x" * 40):
        with pytest.raises(fw.FirmwareError):
            fw.check_version(bad)


def test_latest_skips_drafts_and_prereleases_and_other_tags():
    rels = [_release("0.1.0", at="2026-11-01"), _release("0.1.1", at="2026-11-07"),
            dict(_release("0.2.0", at="2026-11-09"), prerelease=True),
            {"tag_name": "v1.0.0", "published_at": "2026-11-10", "assets": []}]
    assert fw.pick_release(rels, "latest")["tag_name"] == "fw-v0.1.1"
    assert fw.pick_release(rels, "0.2.0")["tag_name"] == "fw-v0.2.0"
    with pytest.raises(fw.FirmwareError, match="no release"):
        fw.pick_release(rels, "9.9.9")


def test_a_release_is_checked_against_its_sums():
    z = _zip()
    good = f"{hashlib.sha256(z).hexdigest()}  soma-0.1.1-update.zip\n{'0' * 64}  soma-0.1.1-full.bin\n"
    assert fw.verified_app(_release(), z, good) == APP
    with pytest.raises(fw.FirmwareError, match="doesn't match"):
        fw.verified_app(_release(), z + b"tampered", good)
    with pytest.raises(fw.FirmwareError, match="doesn't list"):
        fw.verified_app(_release(), z, f"{'0' * 64}  other.zip\n")


def test_staging_keeps_the_newest_few_and_replaces_a_version(tmp_path):
    for v in ("0.1.0", "0.1.1", "0.1.2", "0.1.3"):
        fw.stage_bytes(tmp_path, v, APP, "uploaded")
    fw.stage_bytes(tmp_path, "0.1.3", APP + b"x", "uploaded")
    kept = fw.staged(tmp_path)
    assert [s.version for s in kept] == ["0.1.3", "0.1.2", "0.1.1"] and kept[0].size == len(APP) + 1
    assert not (tmp_path / "firmware" / "soma-0.1.0.bin").exists()


# -- through the voice server --------------------------------------------------------------------

def _serve(tmp_path, go, robot_ip="127.0.0.1"):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    rebooted = []

    async def reboot(request):
        rebooted.append(request.headers.get("X-StackChan-Token"))
        return web.json_response({"ok": True})

    async def status(request):
        return web.json_response({"ok": True, "firmware": "0.1.0"})

    async def run():
        robot = web.Application()
        robot.router.add_post("/api/reboot", reboot)
        robot.router.add_get("/api/status", status)
        async with TestServer(robot) as rs:
            y = tmp_path / "soma.yaml"
            y.write_text(f"state_dir: {str(state).replace(chr(92), '/')}\nadvertise: 10.0.0.2\n"
                         f"robot:\n  url: {str(rs.make_url('')).rstrip('/')}\n  token: robot-secret\n"
                         f"  device_from: [{robot_ip}]\nlodestar:\n  url: http://l:1\n  token: t\n", encoding="utf-8")
            link = voice.Link(load_config(str(y)).runtime())
            async with TestClient(TestServer(voice.make_app(link))) as client:
                return await go(client, {"Authorization": f"Bearer {link.token}"}, link, rebooted)
    return asyncio.run(run())


def test_install_offers_it_once_on_the_next_check_in(tmp_path):
    async def go(client, auth, link, rebooted):
        up = await client.put("/firmware/upload?version=0.2.0-mine", data=APP, headers=auth)
        before = await (await client.post("/ota/")).json()
        armed = await client.post("/firmware/install", json={"version": "0.2.0-mine"}, headers=auth)
        first = await (await client.post("/ota/")).json()
        second = await (await client.post("/ota/")).json()
        image = await client.get(first["firmware"]["url"].split(":7480", 1)[0].replace("http://10.0.0.2", "") +
                                 "/firmware/image/" + first["firmware"]["url"].rsplit("/", 1)[1])
        listing = await (await client.get("/firmware", headers=auth)).json()
        return up.status, before, await armed.json(), first, second, image.status, await image.read(), listing, rebooted
    up, before, armed, first, second, img_status, img, listing, rebooted = _serve(tmp_path, go)
    assert up == 200 and "firmware" not in before
    assert armed["rebooting"] and rebooted == ["robot-secret"]
    assert first["firmware"] == {"version": "0.2.0-mine", "force": 1,
                                 "url": "http://10.0.0.2:7480/firmware/image/soma-0.2.0-mine.bin"}
    assert "firmware" not in second                       # disarmed as it was offered
    assert img_status == 200 and img == APP
    assert listing["robot"] == "0.1.0" and listing["armed"] is None and listing["staged"][0]["source"] == "uploaded"


def test_the_image_is_for_the_robot_only(tmp_path):
    async def go(client, auth, link, rebooted):
        await client.put("/firmware/upload?version=0.2.0", data=APP, headers=auth)
        r = await client.get("/firmware/image/soma-0.2.0.bin")
        return r.status, await r.text()
    status, text = _serve(tmp_path, go, robot_ip="10.9.9.9")
    assert status == 403 and "robot" in text


def test_firmware_routes_need_the_api_token(tmp_path):
    async def go(client, auth, link, rebooted):
        return [(await client.request(m, p, **kw)).status for m, p, kw in (
            ("GET", "/firmware", {}), ("PUT", "/firmware/upload?version=1.0.0", {"data": APP}),
            ("POST", "/firmware/install", {"json": {"version": "1.0.0"}}), ("POST", "/firmware/fetch", {"json": {}}))]
    assert _serve(tmp_path, go) == [401, 401, 401, 401]


def test_a_bad_upload_or_an_unstaged_version_is_said_so(tmp_path):
    async def go(client, auth, link, rebooted):
        full = await client.put("/firmware/upload?version=1.0.0", data=b"PK" + b"0" * 4000, headers=auth)
        missing = await client.post("/firmware/install", json={"version": "1.0.0"}, headers=auth)
        return full.status, await full.text(), missing.status, link.firmware_armed, rebooted
    full, text, missing, armed, rebooted = _serve(tmp_path, go)
    assert full == 400 and "app image" in text and missing == 404 and armed is None and rebooted == []
