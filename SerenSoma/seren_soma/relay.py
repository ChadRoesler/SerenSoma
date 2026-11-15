"""The conversation, kept by Lodestar: Soma as ears and a mouth (PROTOTYPE, 13 Nov 2026).

With `conversation: lodestar` in seren-soma.yaml, Soma stops keeping the back and forth itself. What
it was doing that had nothing to do with a robot (queueing what was heard for whoever answers,
rendering a reply a sentence at a time, back and forth, the spoken stop, the timing of a turn) is
Lodestar's, once, for every place a conversation happens (seren_lodestar/routes/talking.py is the
contract). What stays here is what needs the robot or the room:

  the ears    deciding somebody has finished talking, then handing the finished clip over
  the mouth   keeping one request open for the next piece of sound, playing it, saying it was played
  the mic     who may open it, the tap, the Stop button

LODESTAR'S IS THE ONLY CLOCK. Soma posts {"minutes": n} when the person starts back and forth and
keeps no end time of its own: `talk_until` here is a copy of Lodestar's. Every answer to the ears and
the mouth says whether it is still on, and "off" in any of them means close the microphone. So if
Lodestar restarts and forgets the conversation, the microphone closes, which is the right way for a
microphone to fail. A channel the person opened with a tap and no back and forth is not touched by
that: "off" only ends what was on.

Soma's own /heard, /say, /talk and /api/voice-timing pass through to Lodestar while this is on, so
the Workbench's body tools and anything else that talks to Soma keeps working unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientTimeout, FormData, web

log = logging.getLogger("seren-soma")
DOWN = (ClientError, asyncio.TimeoutError, OSError)


@dataclass
class Piece:
    seq: int
    wav: bytes
    emotion: str
    last: bool
    taken: float        # when it arrived here (time.monotonic()): the sound starts some time after


class LodestarLoop:
    def __init__(self, link, mouth: str = "soma"):
        self.link, self.mouth = link, mouth
        self.base = f"{link.lodestar_url}/api/v1/talk/{mouth}"
        self.pieces: asyncio.Queue = asyncio.Queue()
        self.after = 0          # the last piece taken: the next request asks for what comes after it
        self.since = ""         # when this mouth began on Lodestar's side, as Lodestar says: changes when it restarts
        self.playing = False
        self.tasks: list = []

    # -- the calls ------------------------------------------------------------------------------

    async def _json(self, method: str, path: str = "", *, timeout: float = 15, **kw) -> tuple[int, dict]:
        async with self.link.http.request(method, self.base + path, headers=self.link.lodestar_headers(),
                                          timeout=ClientTimeout(total=timeout), **kw) as r:
            body = await r.json(content_type=None)
            return r.status, body if isinstance(body, dict) else {"error": str(body)[:200]}

    async def _off(self, answer: dict) -> None:
        """Lodestar said back and forth is off. If it was on here, it ends here too, at once."""
        if answer.get("on") is False and self.link.talking():
            log.info("back and forth: Lodestar says it is over")
            await self.link.stop_talking(tell=False, cut=True)

    # -- the ears -------------------------------------------------------------------------------

    async def hear(self, pcm, detected: float, quiet_ms: float) -> None:
        """A finished utterance, handed to Lodestar to transcribe and queue. Soma still keeps the
        clip and the line in heard.jsonl, and shows the words on the robot's screen."""
        from . import voice as v
        link = self.link
        name = time.strftime("%Y%m%d-%H%M%S")
        data = v.wav_bytes(pcm, v.IN_RATE)
        path = v.HEARD_DIR / f"{name}.wav"
        path.write_bytes(data)
        seconds = round(len(pcm) / v.IN_RATE, 1)
        form = FormData()
        form.add_field("file", data, filename=f"{name}.wav", content_type="audio/wav")
        form.add_field("language", "en")
        form.add_field("quiet_ms", str(int(quiet_ms + (time.monotonic() - detected) * 1000)))
        # what the one answering is called here, so a stop addressed by name is heard as one: the
        # person's to give (`partner:` in the config), and Lodestar's to use when it reads it
        if getattr(link.args, "partner", ""):
            form.add_field("names", link.args.partner)
        link.speech_used = time.monotonic()
        try:
            status, out = await self._json("POST", "/heard", data=form, timeout=120)
        except DOWN as e:
            log.warning("hear: Lodestar didn't take the clip: %s", e or type(e).__name__)
            status, out = 0, {}
        if status != 200:
            # a clip that wasn't transcribed may hold words: it is kept, with no text
            if status:
                log.warning("hear: %s %s", status, str(out)[:200])
            text, outcome = "", "failed"
        else:
            text, outcome = str(out.get("text") or "").strip(), str(out.get("heard") or "")
        if outcome == "wordless":
            path.unlink(missing_ok=True)
            log.info("dropped a clip with no words (%.1fs)", seconds)
            return
        with v.HEARD_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "wav": str(path), "seconds": seconds, "text": text}, ensure_ascii=False) + "\n")
        log.info("heard (%ss): %s", seconds, text)
        link.last_heard = time.monotonic()
        d = link.device
        if d is not None and text:
            d.last_words = time.monotonic()
            await link.send(d, {"type": "stt", "text": text})
        if outcome == "stop":
            # Lodestar took it as an order, ended back and forth, and handed it on to nobody
            log.info("back and forth: told to stop out loud")
            await link.stop_talking(tell=False, cut=True)
        elif status == 200:
            await self._off(out)

    # -- the mouth ------------------------------------------------------------------------------

    async def fetch(self) -> None:
        """One request for the next piece of sound, always open, playing or not: the next piece is
        ready about when the one playing ends, and a stop that came from anywhere else arrives here."""
        http, link = self.link.http, self.link
        while True:
            try:
                async with http.get(self.base + "/audio", params={"after": str(self.after), "wait": "25"},
                                    headers=link.lodestar_headers(), timeout=ClientTimeout(total=45)) as r:
                    on = r.headers.get("X-Seren-Talk")
                    # Lodestar numbers a mouth's pieces from 1 each time it starts. X-Seren-Since is when
                    # this mouth began there and changes on a restart; X-Seren-Seq-Max below what was last
                    # taken says the same. Either way: ask from the start, or the new pieces never come.
                    since, newest = r.headers.get("X-Seren-Since", ""), r.headers.get("X-Seren-Seq-Max", "")
                    if (since and self.since and since != self.since) or (newest.isdigit() and int(newest) < self.after):
                        log.info("the mouth: Lodestar started again; asking for its pieces from the start")
                        self.after = 0
                        self.since = since
                        await r.read()
                        continue
                    self.since = since or self.since
                    if r.status == 200:
                        seq = int(r.headers.get("X-Seren-Seq", "0"))
                        self.after = seq
                        await self.pieces.put(Piece(seq=seq, wav=await r.read(), emotion=r.headers.get("X-Seren-Emotion", ""),
                                                    last=r.headers.get("X-Seren-Last") == "1", taken=time.monotonic()))
                    else:
                        await r.read()
                    if on == "off":
                        await self._off({"on": False})
                    if r.status not in (200, 204):
                        log.warning("the mouth: Lodestar answered %s", r.status)
                        await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except DOWN:
                # Lodestar is down or restarting. A restarted one numbers its pieces from the start
                # again, so ask from the start: with nothing here in flight, nothing can play twice.
                if self.pieces.empty() and not self.playing:
                    self.after = 0
                await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001 - the mouth keeps asking whatever one answer did
                log.warning("the mouth: %s: %s", type(e).__name__, e)
                await asyncio.sleep(3)

    async def play(self) -> None:
        """Play what arrives, a line at a time: a line is its pieces up to the one marked last."""
        link = self.link
        while True:
            first = await self.pieces.get()
            line = [first]

            async def feed(piece=first):
                while True:
                    yield piece.wav
                    if piece.last:
                        return
                    try:
                        piece = await asyncio.wait_for(self.pieces.get(), 20)
                    except asyncio.TimeoutError:
                        return        # the rest of the line never came
                    line.append(piece)

            async def sent(index: int, first_audio: float | None) -> None:
                # for a line's first piece: how long after taking it the sound began (the robot may
                # have had a channel to open), which is the leg only the mouth can time
                started = int(max(0.0, first_audio - line[index].taken) * 1000) if index == 0 and first_audio else None
                asyncio.ensure_future(self._played(line[index].seq, started))

            self.playing = True
            try:
                await link.say("", link.args.voice, first.emotion or None, wake=True, feed=feed(), on_sent=sent)
            except asyncio.CancelledError:
                raise
            except web.HTTPException as e:
                log.warning("the mouth: couldn't play a line: %s", e.text or e.reason)
            except Exception as e:  # noqa: BLE001
                log.warning("the mouth: couldn't play a line: %s: %s", type(e).__name__, e)
            finally:
                self.playing = False

    async def _played(self, seq: int, started_ms: int | None) -> None:
        try:
            _, out = await self._json("POST", "/played", json={"seq": seq, **({"started_ms": started_ms} if started_ms is not None else {})})
            await self._off(out)
        except DOWN:
            pass

    def drop_waiting(self) -> None:
        """A stop: what is waiting here to be played never plays."""
        while not self.pieces.empty():
            self.pieces.get_nowait()

    def start(self) -> None:
        self.tasks = [asyncio.ensure_future(self.fetch()), asyncio.ensure_future(self.play())]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    # -- back and forth -------------------------------------------------------------------------

    async def tell_off(self) -> None:
        try:
            await self._json("POST", json={"on": False})
        except DOWN as e:
            log.info("back and forth: couldn't tell Lodestar it's over: %s", e or type(e).__name__)

    async def keep_talking(self) -> None:
        """While back and forth is on: open the channel again, listening, whenever it has closed, and
        look at Lodestar's clock every few seconds. Lodestar holds the voice; Soma doesn't."""
        link = self.link
        look_at = 0.0
        try:
            while link.talking():
                if time.monotonic() >= look_at:
                    look_at = time.monotonic() + 5
                    try:
                        _, st = await self._json("GET")
                        link.talk_voice = str(st.get("voice") or "")
                        if st.get("on") and st.get("until"):
                            link.talk_until = float(st["until"])      # a copy of Lodestar's, never Soma's own
                        elif st.get("on") is False:
                            log.info("back and forth: Lodestar says it is over")
                            break
                    except DOWN:
                        log.info("back and forth: Lodestar isn't answering, so it's over")
                        break
                if link.device is None and not link.waking and not link.say_lock.locked() and not link.saying:
                    try:
                        await link.wake_device(deaf=False)
                    except Exception as e:  # noqa: BLE001 - the robot is off or busy: try again shortly
                        log.info("back and forth: couldn't open the channel: %s", getattr(e, "text", None) or e)
                        await asyncio.sleep(8)
                await asyncio.sleep(2)
        finally:
            ended_here = link.talk_until != 0.0
            link.talk_until, link.talk_voice = 0.0, ""
            link.talk_task = None if link.talk_task is asyncio.current_task() else link.talk_task
            d = link.device
            if ended_here and d is not None:        # it ran out, or Lodestar went away: the microphone closes
                d.hang_up = True
                if not d.speaking and not link.saying:
                    await asyncio.shield(link.hang_up(d))
            log.info("back and forth: off")

    async def state(self) -> dict:
        link, d = self.link, self.link.device
        try:
            _, st = await self._json("GET")
        except DOWN as e:
            st = {"on": False, "voice": f"can't reach Lodestar: {e or type(e).__name__}"}
        on = bool(st.get("on")) and link.talking()
        return {"on": on, "until": st.get("until") if on else None, "voice": str(st.get("voice") or ""),
                "channel_open": d is not None, "listening": bool(d and not d.deaf and not d.speaking),
                "minutes_range": st.get("minutes_range") or [1, 180]}

    # -- Soma's own routes, passed through ------------------------------------------------------

    async def talk_get(self, request):
        self.link.authorized(request)
        return web.json_response(await self.state())

    async def talk_post(self, request):
        link = self.link
        link.authorized(request)
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="expected JSON")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="expected a JSON object")
        if body.get("on") is False:
            await link.stop_talking()
            return web.json_response(await self.state())
        try:
            status, st = await self._json("POST", json={"minutes": body.get("minutes")})
        except DOWN as e:
            raise web.HTTPBadGateway(text=f"can't reach Lodestar to start it: {e or type(e).__name__}")
        if status != 200 or not st.get("on"):
            raise web.HTTPBadRequest(text=str(st.get("error") or f"Lodestar answered {status}"))
        link.talk_until = float(st.get("until") or 0.0)
        if link.talk_task is None or link.talk_task.done():
            link.talk_task = asyncio.ensure_future(link.keep_talking())
        log.info("back and forth: on for %s minutes, kept by Lodestar", body.get("minutes"))
        return web.json_response(await self.state())

    async def say_route(self, request):
        link = self.link
        link.authorized(request)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="no text")
        send = {"text": text, "voice": body.get("voice") or link.args.voice or "", "whole": bool(body.get("whole")),
                **({"emotion": body["emotion"]} if body.get("emotion") else {})}
        try:
            status, out = await self._json("POST", "/say", json=send, timeout=300)
        except DOWN as e:
            raise web.HTTPBadGateway(text=f"can't reach Lodestar to say it: {e or type(e).__name__}")
        if status != 200:
            why = str(out.get("error") or f"Lodestar answered {status}")
            # 504 from Lodestar: nothing played it, which here means the robot is off or wouldn't wake
            raise (web.HTTPGatewayTimeout if status == 504 else web.HTTPBadGateway)(text=why)
        return web.json_response({"ok": True, "seconds": out.get("seconds", 0.0), "cut": bool(out.get("cut"))})

    async def heard_route(self, request):
        self.link.authorized(request)
        q = request.query
        try:
            wait = max(0.0, min(30.0, float(q.get("wait", "0") or 0)))
            _, out = await self._json("GET", "/heard", params={"after": q.get("after", "0") or "0", "wait": repr(wait)},
                                      timeout=wait + 15)
        except ValueError:
            raise web.HTTPBadRequest(text="after is a time and wait is seconds, both numbers")
        except DOWN as e:
            raise web.HTTPBadGateway(text=f"can't reach Lodestar: {e or type(e).__name__}")
        return web.json_response({"entries": out.get("entries", []), "now": out.get("now", time.time()),
                                  "last": out.get("last", 0.0)})

    async def voice_timing(self, request):
        from . import voice as v
        link = self.link
        link.authorized(request)
        try:
            _, out = await self._json("GET", "/timing")
        except DOWN:
            out = {}
        turns = []
        for t in out.get("turns", []):
            t = dict(t)
            if "to_here_ms" in t:       # the quiet the ears waited, plus getting the clip there
                t["wait_for_quiet_ms"] = t["to_here_ms"]
            turns.append(t)
        return web.json_response({"turns": turns, "end_silence_ms": link.end_silence_ms,
                                  "end_silence_range": list(v.END_SILENCE_RANGE), "kept_by": "lodestar"})
