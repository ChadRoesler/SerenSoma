"""Soma as ears and a mouth, with Lodestar keeping the conversation (relay.py): a finished clip goes
there and the words come back, sound is pulled from there and played, and "off" from there closes
the microphone. Against a pretend Lodestar speaking seren_lodestar/routes/talking.py's contract."""
from __future__ import annotations

import asyncio
import io
import json
import time
import wave

import numpy as np
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from seren_soma import voice
from seren_soma.config import load_config


def _wav(seconds: float = 0.24) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes((np.sin(np.arange(int(24000 * seconds)) / 20) * 3000).astype(np.int16).tobytes())
    return out.getvalue()


class Lodestar:
    """What the pretend Lodestar was asked, and what it will answer."""

    def __init__(self):
        self.on = False
        self.heard_in = []                  # each clip the ears sent: the form's fields
        self.answer = {"text": "you call that a combo", "heard": "queued"}
        self.pieces = []                    # (seq, wav, emotion, last) for the mouth to take
        self.played = []
        self.said = []
        self.set = []
        self.audio_asks = 0
        self.since = "1794600000.000"       # when this mouth began here: changes when Lodestar restarts

    def app(self) -> web.Application:
        base = "/api/v1/talk/soma"
        app = web.Application()

        async def heard_in(request):
            form = await request.post()
            self.heard_in.append({k: (v if isinstance(v, str) else len(v.file.read())) for k, v in form.items()})
            return web.json_response({"ok": True, **self.answer, "on": self.on})

        async def audio(request):
            self.audio_asks += 1
            after = int(request.query["after"])
            for _ in range(4):              # a short wait, as the real one's long one
                nxt = next((p for p in self.pieces if p[0] > after), None)
                if nxt:
                    seq, wav, emotion, last = nxt
                    return web.Response(body=wav, content_type="audio/wav", headers={
                        "X-Seren-Seq-Max": str(max(p[0] for p in self.pieces)), "X-Seren-Since": self.since,
                        "X-Seren-Seq": str(seq), "X-Seren-Last": "1" if last else "0", "X-Seren-Clip-Seconds": "0.24",
                        "X-Seren-Talk": "on" if self.on else "off", **({"X-Seren-Emotion": emotion} if emotion else {})})
                await asyncio.sleep(0.05)
            return web.Response(status=204, headers={"X-Seren-Talk": "on" if self.on else "off", "X-Seren-Since": self.since,
                                                     "X-Seren-Seq-Max": str(max([p[0] for p in self.pieces], default=0))})

        async def played(request):
            self.played.append(await request.json())
            return web.json_response({"ok": True, "seq": self.played[-1]["seq"], "on": self.on})

        async def state(request):
            return web.json_response({"mouth": "soma", "on": self.on, "until": time.time() + 600 if self.on else None,
                                      "voice": "held" if self.on else "", "minutes_range": [1, 180]})

        async def talk_set(request):
            body = await request.json()
            self.set.append(body)
            if body.get("on") is False:
                self.on = False
            elif not isinstance(body.get("minutes"), (int, float)):
                return web.json_response({"error": 'send {"minutes": 1 to 180}, or {"on": false}'}, status=400)
            else:
                self.on = True
            return await state(request)

        async def say(request):
            self.said.append(await request.json())
            return web.json_response({"ok": True, "pieces": 2, "played": 2, "cut": False, "seconds": 3.5})

        async def heard_out(request):
            return web.json_response({"entries": [{"t": 1794549131.44, "seconds": 2.3, "text": "you call that a combo"}],
                                      "last": 1794549131.44, "now": 1794549133.0, "on": self.on})

        async def timing(request):
            return web.json_response({"ok": True, "mouth": "soma", "turns": [
                {"seq": 1, "kind": "heard", "t": 1.0, "audio_s": 2.0, "words": 5, "transcribed": True, "to_here_ms": 1320,
                 "transcribe_ms": 480}]})

        app.router.add_post(base + "/heard", heard_in)
        app.router.add_get(base + "/heard", heard_out)
        app.router.add_get(base + "/audio", audio)
        app.router.add_post(base + "/played", played)
        app.router.add_post(base + "/say", say)
        app.router.add_get(base + "/timing", timing)
        app.router.add_get(base, state)
        app.router.add_post(base, talk_set)
        return app


def _with_lodestar(tmp_path, go, kept="lodestar"):
    """The pretend Lodestar, and Soma told that Lodestar keeps the conversation; go(client, auth, link, rt, star)."""
    star = Lodestar()

    async def run():
        async with TestServer(star.app()) as ls:
            y = tmp_path / "soma.yaml"
            y.write_text(f"state_dir: {str(tmp_path / 'state').replace(chr(92), '/')}\nadvertise: 10.0.0.2\n"
                         f"conversation: {kept}\n"
                         "robot:\n  url: http://127.0.0.1:9\n  token: r\n"
                         f"lodestar:\n  url: {str(ls.make_url('')).rstrip('/')}\n  token: t\n", encoding="utf-8")
            rt = load_config(str(y)).runtime()
            link = voice.Link(rt)
            async with TestClient(TestServer(voice.make_app(link))) as client:
                return await go(client, {"Authorization": f"Bearer {link.token}"}, link, rt, star)
    return asyncio.run(run())


async def _robot(client, rt):
    ws = await client.ws_connect("/voice", headers={"Authorization": f"Bearer {rt.device_token}", "Device-Id": "test"})
    await ws.send_str(json.dumps({"type": "hello"}))
    assert (await ws.receive_json())["type"] == "hello"
    return ws


async def _closed(robot, within: float = 6):
    """Wait for the channel to close; what is said to the robot before that (the words on its screen) is skipped."""
    end = time.monotonic() + within
    while True:
        msg = await asyncio.wait_for(robot.receive(), max(0.1, end - time.monotonic()))
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            return True


QUIET = np.zeros(16000, dtype=np.int16)


def test_soma_keeps_its_own_conversation_unless_told(tmp_path):
    async def go(client, auth, link, rt, star):
        return link.loop, rt.conversation
    assert _with_lodestar(tmp_path, go, kept="own") == (None, "own")
    assert _with_lodestar(tmp_path, go, kept="somebody else")[1] == "own"      # a value it doesn't know is own


def test_a_finished_clip_goes_to_lodestar_and_the_words_come_back(tmp_path):
    async def go(client, auth, link, rt, star):
        await link.hear(QUIET, time.monotonic(), 1300)
        log = (tmp_path / "state" / "heard.jsonl").read_text(encoding="utf-8")
        return star.heard_in, json.loads(log.splitlines()[-1]), link.heard_recent
    sent, line, kept_here = _with_lodestar(tmp_path, go)
    assert sent[0]["language"] == "en" and sent[0]["file"] > 30000            # the whole clip went
    assert 1300 <= int(sent[0]["quiet_ms"]) < 1800                            # how long ago the talking stopped
    assert line["text"] == "you call that a combo" and line["seconds"] == 1.0  # Soma still keeps its own log
    assert kept_here == []                                                     # the queue is Lodestar's now


def test_a_sound_with_no_words_is_dropped_here_too(tmp_path):
    async def go(client, auth, link, rt, star):
        star.answer = {"text": "(laughs)", "heard": "wordless"}
        await link.hear(QUIET, time.monotonic(), 900)
        return list((tmp_path / "state" / "heard").glob("*.wav")), (tmp_path / "state" / "heard.jsonl").exists()
    assert _with_lodestar(tmp_path, go) == ([], False)


def test_the_mouth_plays_what_lodestar_hands_it_and_says_when_it_has(tmp_path):
    async def go(client, auth, link, rt, star):
        robot = await _robot(client, rt)
        star.pieces = [(1, _wav(), "happy", False), (2, _wav(), "", True)]
        texts, packets = [], 0
        while True:
            msg = await asyncio.wait_for(robot.receive(), 10)
            if msg.type == WSMsgType.BINARY:
                packets += 1
            elif msg.type == WSMsgType.TEXT:
                m = json.loads(msg.data)
                texts.append((m["type"], m.get("state") or m.get("emotion")))
                if m.get("type") == "tts" and m.get("state") == "stop":
                    break
        await asyncio.sleep(0.3)
        await robot.close()
        return texts, packets, star.played, star.audio_asks
    texts, packets, played, asks = _with_lodestar(tmp_path, go)
    assert texts[0] == ("tts", "start") and ("llm", "happy") in texts and texts[-1] == ("tts", "stop")
    assert texts.count(("tts", "start")) == 1                  # one line, though it came as two pieces
    assert packets >= 6                                        # both pieces' sound reached the robot
    assert [p["seq"] for p in played] == [1, 2]
    assert "started_ms" in played[0] and played[0]["started_ms"] >= 0 and "started_ms" not in played[1]
    assert asks >= 3                                           # it kept asking for the next while playing


def test_off_from_lodestar_closes_the_microphone(tmp_path):
    async def go(client, auth, link, rt, star):
        robot = await _robot(client, rt)
        started = await (await client.post("/talk", headers=auth, json={"minutes": 30})).json()
        was_on = link.talking()
        star.on = False                                        # Lodestar restarted, or someone stopped it there
        closed = await _closed(robot, 8)
        return started["on"], was_on, closed, link.talking(), star.set
    started, was_on, closed, still_on, told = _with_lodestar(tmp_path, go)
    assert started is True and was_on is True and told[0] == {"minutes": 30}
    assert closed is True and still_on is False
    assert {"on": False} not in told                           # Lodestar said so: nothing to tell it back


def test_stop_on_somas_page_tells_lodestar_and_a_tapped_channel_is_left_alone(tmp_path):
    async def go(client, auth, link, rt, star):
        # a channel the person opened with a tap, no back and forth: "off" from Lodestar means nothing to it
        robot = await _robot(client, rt)
        await link.hear(QUIET, time.monotonic(), 900)
        await asyncio.sleep(0.5)
        open_after_off = link.device is not None
        await client.post("/talk", headers=auth, json={"minutes": 30})
        stopped = await (await client.post("/talk", headers=auth, json={"on": False})).json()
        closed = await _closed(robot)
        return open_after_off, stopped["on"], closed, star.set
    open_after_off, on, closed, told = _with_lodestar(tmp_path, go)
    assert open_after_off is True
    assert on is False and closed is True
    assert told == [{"minutes": 30}, {"on": False}]


def test_stop_said_aloud_is_lodestars_call_and_closes_the_microphone_here(tmp_path):
    async def go(client, auth, link, rt, star):
        robot = await _robot(client, rt)
        await client.post("/talk", headers=auth, json={"minutes": 30})
        star.answer, star.on = {"text": "Alright, stop it.", "heard": "stop"}, False
        await link.hear(QUIET, time.monotonic(), 900)
        closed = await _closed(robot)
        return closed, link.talking()
    closed, on = _with_lodestar(tmp_path, go)
    assert closed is True and on is False


def test_somas_own_routes_pass_through_so_nothing_that_talks_to_soma_changes(tmp_path):
    async def go(client, auth, link, rt, star):
        said = await (await client.post("/say", headers=auth, json={"text": "Ha! Try again.", "emotion": "laughing"})).json()
        heard = await (await client.get("/heard?after=0", headers=auth)).json()
        talk = await (await client.get("/talk", headers=auth)).json()
        timing = await (await client.get("/api/voice-timing", headers=auth)).json()
        unasked = await client.get("/heard")
        return said, heard, talk, timing, unasked.status, star.said
    said, heard, talk, timing, unasked, sent = _with_lodestar(tmp_path, go)
    assert said == {"ok": True, "seconds": 3.5, "cut": False}
    assert sent[0]["text"] == "Ha! Try again." and sent[0]["emotion"] == "laughing" and sent[0]["whole"] is False
    assert set(heard) == {"entries", "now", "last"} and heard["entries"][0]["text"] == "you call that a combo"
    assert set(talk) == {"on", "until", "voice", "channel_open", "listening", "minutes_range"} and talk["on"] is False
    assert timing["kept_by"] == "lodestar" and timing["turns"][0]["wait_for_quiet_ms"] == 1320
    assert unasked == 401                                      # Soma's own token still guards Soma's routes


def test_a_restarted_lodestar_numbers_from_one_again_and_the_mouth_follows(tmp_path):
    async def go(client, auth, link, rt, star):
        robot = await _robot(client, rt)
        await asyncio.sleep(0.4)                 # a few empty answers: the mouth learns when this Lodestar began
        link.loop.after = 17                     # it had taken seventeen pieces from the one before
        star.since = "1794609999.000"            # ...which restarted: a new beginning, numbering from 1
        star.pieces = [(1, _wav(), "", True)]
        packets = 0
        while True:
            msg = await asyncio.wait_for(robot.receive(), 10)
            if msg.type == WSMsgType.BINARY:
                packets += 1
            elif msg.type == WSMsgType.TEXT and json.loads(msg.data).get("state") == "stop":
                break
        await asyncio.sleep(0.3)
        await robot.close()
        return packets, star.played, link.loop.after
    packets, played, after = _with_lodestar(tmp_path, go)
    assert packets >= 3 and [p["seq"] for p in played] == [1] and after == 1
