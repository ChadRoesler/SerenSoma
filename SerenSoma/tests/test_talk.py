"""Talking back and forth: a line goes out a sentence at a time, what was heard reaches whoever
answers the moment it's transcribed, and every leg of a turn is timed without keeping a word of it."""
from __future__ import annotations

import asyncio
import io
import json
import time
import wave

import numpy as np
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from seren_soma import voice
from seren_soma.config import load_config
from seren_soma.timing import VoiceClock


# -- the pieces a line is spoken in --------------------------------------------------------------

def test_a_line_splits_at_its_sentences_and_the_first_stays_short():
    assert voice.split_sentences("Ha! You call that a combo? I've seen better from a Roomba. Try again.") == \
        ["Ha!", "You call that a combo?", "I've seen better from a Roomba. Try again."]
    assert voice.split_sentences("No stop at the end") == ["No stop at the end"]
    assert voice.split_sentences("Version 3.5 is out. Good.") == ["Version 3.5 is out.", "Good."]
    assert voice.split_sentences('He said "no." Then he left.') == ['He said "no."', "Then he left."]
    assert voice.split_sentences("   ") == []


# -- the clock -----------------------------------------------------------------------------------

def test_the_first_thing_said_after_words_is_their_answer_and_carries_the_whole_gap():
    c = VoiceClock()
    c.heard(speech_end=10.0, detected=10.9, done=11.4, audio_s=2.0, words=5, transcribed=True)
    first = c.said(asked=13.4, rendered_first=14.0, first_audio=14.1, done=16.0, pieces=2, chars=40, audio_s=1.9,
                   answering=c.claim())
    second = c.said(asked=17.0, rendered_first=17.5, first_audio=17.5, done=18.0, pieces=1, chars=9, audio_s=0.5,
                    answering=c.claim())
    heard = c.recent()[0]
    assert (heard["wait_for_quiet_ms"], heard["transcribe_ms"]) == (900, 500)
    assert (first["answers"], first["mind_ms"], first["render_first_ms"], first["last_word_to_first_sound_ms"]) == (1, 2000, 600, 4100)
    assert "answers" not in second                       # a second line isn't an answer to the same words


def test_a_clip_with_no_words_is_timed_but_nothing_answers_it():
    c = VoiceClock()
    c.heard(speech_end=1.0, detected=1.9, done=2.2, audio_s=1.0, words=0, transcribed=True)
    assert "answers" not in c.said(asked=3.0, rendered_first=3.2, first_audio=3.3, done=4.0, pieces=1, chars=5, audio_s=0.7,
                                   answering=c.claim())


def test_words_heard_while_a_reply_is_rendering_wait_for_the_next_reply():
    c = VoiceClock()
    c.heard(speech_end=10.0, detected=10.9, done=11.4, audio_s=2.0, words=5, transcribed=True)
    answering = c.claim()                                # the reply is asked for at 13.4...
    c.heard(speech_end=30.0, detected=30.9, done=31.4, audio_s=1.0, words=3, transcribed=True)   # ...and is slow
    slow = c.said(asked=13.4, rendered_first=37.0, first_audio=37.5, done=40.0, pieces=1, chars=9, audio_s=2.5, answering=answering)
    nxt = c.said(asked=45.0, rendered_first=45.5, first_audio=45.6, done=47.0, pieces=1, chars=9, audio_s=1.4, answering=c.claim())
    assert (slow["answers"], slow["mind_ms"], slow["last_word_to_first_sound_ms"]) == (1, 2000, 27500)
    assert nxt["answers"] == 2 and nxt["mind_ms"] == 13600


# -- the settings --------------------------------------------------------------------------------

def test_how_much_quiet_ends_an_utterance_is_a_live_setting_with_its_own_range():
    s = voice.merge_settings(voice.load_settings(), {"channel": {"end_silence_ms": 500}})
    assert s["channel"] == {"dead_air_seconds": 60, "end_silence_ms": 500}
    with pytest.raises(ValueError, match="milliseconds, 300 to 2500"):
        voice.merge_settings(s, {"channel": {"end_silence_ms": 100}})
    with pytest.raises(ValueError, match="seconds, 10 to 600"):
        voice.merge_settings(s, {"channel": {"dead_air_seconds": 5}})


# -- the whole path, against a pretend speech service and a pretend robot ------------------------

def _wav(seconds: float) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes((np.sin(np.arange(int(24000 * seconds)) / 20) * 3000).astype(np.int16).tobytes())
    return out.getvalue()


def _with_speech(tmp_path, go, heard_text="you call that a combo"):
    """A pretend Lodestar and the voice server pointed at it; go(client, auth, link, rt, spoken)."""
    spoken = []

    async def speak(request):
        spoken.append((await request.json())["text"])
        return web.Response(body=_wav(0.24), content_type="audio/wav")

    async def hear(request):
        await request.read()
        return web.json_response({"text": heard_text})

    async def run():
        lodestar = web.Application()
        lodestar.router.add_post("/api/v1/speak", speak)
        lodestar.router.add_post("/api/v1/hear", hear)
        async with TestServer(lodestar) as ls:
            y = tmp_path / "soma.yaml"
            y.write_text(f"state_dir: {str(tmp_path / 'state').replace(chr(92), '/')}\nadvertise: 10.0.0.2\n"
                         "robot:\n  url: http://127.0.0.1:9\n  token: r\n"
                         f"lodestar:\n  url: {str(ls.make_url('')).rstrip('/')}\n  token: t\n", encoding="utf-8")
            rt = load_config(str(y)).runtime()
            link = voice.Link(rt)
            async with TestClient(TestServer(voice.make_app(link))) as client:
                return await go(client, {"Authorization": f"Bearer {link.token}"}, link, rt, spoken)
    return asyncio.run(run())


async def _robot(client, rt):
    ws = await client.ws_connect("/voice", headers={"Authorization": f"Bearer {rt.device_token}", "Device-Id": "test"})
    await ws.send_str(json.dumps({"type": "hello"}))
    assert (await ws.receive_json())["type"] == "hello"
    return ws


async def _until_stop(ws):
    """What the robot was sent, up to the end of the line: (the text messages, how many packets of sound)."""
    texts, packets = [], 0
    while True:
        msg = await asyncio.wait_for(ws.receive(), 10)
        if msg.type == WSMsgType.BINARY:
            packets += 1
        elif msg.type == WSMsgType.TEXT:
            m = json.loads(msg.data)
            texts.append((m["type"], m.get("state")))
            if m == {**m, "type": "tts", "state": "stop"}:
                return texts, packets
        else:
            raise AssertionError(f"the channel closed early: {msg.type}")


def test_a_line_is_rendered_and_sent_a_sentence_at_a_time(tmp_path):
    async def go(client, auth, link, rt, spoken):
        ws = await _robot(client, rt)
        say = asyncio.create_task(client.post("/say", headers=auth, json={"text": "Ha! You call that a combo? Try again, slowly."}))
        texts, packets = await _until_stop(ws)
        answer = await (await say).json()
        timing = await (await client.get("/api/voice-timing", headers=auth)).json()
        await ws.close()
        return texts, packets, answer, timing, spoken
    texts, packets, answer, timing, spoken = _with_speech(tmp_path, go)
    # three renders, in order, after the throwaway line that loaded the voice when the channel opened
    assert spoken[0] == voice.WARM_LINE and [s for s in spoken if s != voice.WARM_LINE] ==         ["Ha!", "You call that a combo?", "Try again, slowly."]
    assert texts[0] == ("tts", "start") and texts[-1] == ("tts", "stop")
    assert packets >= 9 and answer["seconds"] == pytest.approx(packets * 0.06)    # every piece's sound arrived
    turn = timing["turns"][-1]
    assert turn["kind"] == "said" and turn["pieces"] == 3 and turn["to_first_sound_ms"] >= turn["render_first_ms"]
    assert "combo" not in json.dumps(timing)                                      # how long, never what


def test_whole_renders_the_line_in_one_piece(tmp_path):
    async def go(client, auth, link, rt, spoken):
        ws = await _robot(client, rt)
        say = asyncio.create_task(client.post("/say", headers=auth, json={"text": "One. Two. Three.", "whole": True}))
        await _until_stop(ws)
        await say
        await ws.close()
        return spoken
    assert [s for s in _with_speech(tmp_path, go) if s != voice.WARM_LINE] == ["One. Two. Three."]


def test_the_speech_service_is_warmed_when_the_channel_opens_and_not_again_while_its_in_use(tmp_path):
    async def go(client, auth, link, rt, spoken):
        first = await _robot(client, rt)
        await asyncio.sleep(0.3)
        await first.close()
        again = await _robot(client, rt)
        await asyncio.sleep(0.3)
        await again.close()
        return spoken
    assert _with_speech(tmp_path, go) == [voice.WARM_LINE]       # the second opening found it just used


def test_what_was_heard_reaches_whoever_is_waiting_the_moment_its_transcribed(tmp_path):
    async def go(client, auth, link, rt, spoken):
        quiet = np.zeros(16000, dtype=np.int16)
        before = await (await client.get("/heard?after=0", headers=auth)).json()
        await link.hear(quiet, time.monotonic(), 900)
        first = await (await client.get("/heard?after=0", headers=auth)).json()
        # someone waiting: asked before the words exist, answered when they do
        started = time.monotonic()
        waiting = asyncio.create_task(client.get(f"/heard?after={first['last']}&wait=20", headers=auth))
        await asyncio.sleep(0.3)
        await link.hear(quiet, time.monotonic(), 900)
        second = await (await waiting).json()
        took = time.monotonic() - started
        nothing = await (await client.get(f"/heard?after={second['last']}&wait=0.2", headers=auth)).json()
        unasked = await client.get("/heard")
        timing = await (await client.get("/api/voice-timing", headers=auth)).json()
        return before, first, second, took, nothing, unasked.status, timing
    before, first, second, took, nothing, unasked, timing = _with_speech(tmp_path, go)
    assert before["entries"] == [] and unasked == 401
    assert [e["text"] for e in first["entries"]] == ["you call that a combo"]
    assert [e["text"] for e in second["entries"]] == ["you call that a combo"] and second["last"] > first["last"]
    assert took < 5                                             # woken by the words, not by the 20 s running out
    assert nothing["entries"] == []
    heard = [t for t in timing["turns"] if t["kind"] == "heard"]
    assert len(heard) == 2 and heard[0]["words"] == 5 and heard[0]["wait_for_quiet_ms"] == 900
    assert "combo" not in json.dumps(timing)


# -- back and forth ------------------------------------------------------------------------------

def _with_speaker(tmp_path, go):
    """A pretend Lodestar that keeps a book of who holds the speaker; go(client, auth, link, book)."""
    book = []

    async def ensure(request):
        body = await request.json()
        book.append(("ensure", body["holder"], body["ttl_seconds"]))
        return web.json_response({"ok": True, "ready": True})

    async def release(request):
        book.append(("release", (await request.json())["holder"]))
        return web.json_response({"ok": True})

    async def run():
        lodestar = web.Application()
        lodestar.router.add_post("/api/v1/service/speaker/ensure", ensure)
        lodestar.router.add_post("/api/v1/service/speaker/release", release)
        async with TestServer(lodestar) as ls:
            y = tmp_path / "soma.yaml"
            y.write_text(f"state_dir: {str(tmp_path / 'state').replace(chr(92), '/')}\nadvertise: 10.0.0.2\n"
                         "robot:\n  url: http://127.0.0.1:9\n  token: r\n"
                         f"lodestar:\n  url: {str(ls.make_url('')).rstrip('/')}\n  token: t\n", encoding="utf-8")
            link = voice.Link(load_config(str(y)).runtime())
            async with TestClient(TestServer(voice.make_app(link))) as client:
                return await go(client, {"Authorization": f"Bearer {link.token}"}, link, book)
    return asyncio.run(run())


def test_back_and_forth_holds_the_voice_in_its_own_name_and_lets_it_go_when_turned_off(tmp_path):
    async def go(client, auth, link, book):
        off = await (await client.get("/talk", headers=auth)).json()
        bad = await client.post("/talk", headers=auth, json={"minutes": 0})
        on = await (await client.post("/talk", headers=auth, json={"minutes": 30})).json()
        await asyncio.sleep(0.4)
        during = await (await client.get("/talk", headers=auth)).json()
        held_dead_air = link.talking()
        stopped = await (await client.post("/talk", headers=auth, json={"on": False})).json()
        await asyncio.sleep(0.3)
        unasked = await client.post("/talk", json={"minutes": 5})
        return off, bad.status, on, during, held_dead_air, stopped, unasked.status, list(book)
    off, bad, on, during, held, stopped, unasked, book = _with_speaker(tmp_path, go)
    assert off["on"] is False and bad == 400 and unasked == 401
    assert on["on"] is True and during["voice"] == "held" and held is True
    assert stopped["on"] is False
    assert book[0] == ("ensure", voice.TALK_HOLDER, voice.TALK_LEASE_S) and book[-1] == ("release", voice.TALK_HOLDER)
    assert voice.TALK_HOLDER != "seren-voice"      # /speak drops its own lease after every line; ours must outlive that


def test_stopping_back_and_forth_closes_the_microphone_at_once(tmp_path):
    async def go(client, auth, link, book):
        await client.post("/talk", headers=auth, json={"minutes": 30})
        robot = await client.ws_connect("/voice", headers={"Authorization": f"Bearer {link.device_token}", "Device-Id": "test"})
        await robot.send_str(json.dumps({"type": "hello"}))
        await robot.receive_json()
        open_before = (await (await client.get("/talk", headers=auth)).json())["channel_open"]
        started = time.monotonic()
        await client.post("/talk", headers=auth, json={"on": False})
        closed = await asyncio.wait_for(robot.receive(), 5)
        return open_before, closed.type, time.monotonic() - started
    open_before, closed, took = _with_speaker(tmp_path, go)
    assert open_before is True
    assert closed in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED) and took < 2    # not after the dead-air timer


def test_tapping_the_robot_off_ends_back_and_forth_rather_than_being_undone(tmp_path):
    async def go(client, auth, link, book):
        await client.post("/talk", headers=auth, json={"minutes": 30})
        robot = await client.ws_connect("/voice", headers={"Authorization": f"Bearer {link.device_token}", "Device-Id": "test"})
        await robot.send_str(json.dumps({"type": "hello"}))
        await robot.receive_json()
        await robot.close()                      # the tap: the robot hangs up, cleanly
        await asyncio.sleep(0.5)
        return (await (await client.get("/talk", headers=auth)).json())["on"], list(book)
    on, book = _with_speaker(tmp_path, go)
    assert on is False and book[-1] == ("release", voice.TALK_HOLDER)
