"""The body's nervous system: stands where Xiaozhi's cloud stood.

The robot checks in at POST /ota/, is told to use our websocket, and speaks Xiaozhi's protocol to
/voice (see firmware/docs/voice-link.md). What it hears goes to Lodestar's hear route and lands in
<state>/heard.jsonl. What it says comes from Lodestar's speak route, by POST /say. It reads the head
sensor into touches and faces, and it owns night mode and work hours.

There is no mind in here: whoever answers reads heard.jsonl and POSTs /say.

TWO TOKENS. The API token (<state>/token, or server.bearer_token_*) guards /say, /settings, /touch,
/clip, /status and /api/logs, and never leaves this box. The device token (<state>/device_token) is
what /ota/ hands the robot for its websocket, and /ota/ answers only the robot's own address. Until
7 Nov 2026 they were one token, and /ota/ gave it to anything on the LAN that asked.
"""
import asyncio
import io
import json
import logging
import re
import secrets
import time
import wave
from pathlib import Path

import av
import numpy as np
from aiohttp import ClientSession, ClientTimeout, FormData, WSMsgType, web

from .touch_read import HEAD_KINDS, Overwhelm, TouchReader, in_window, reaction_for, scritching, scritchy, work_hours

log = logging.getLogger("seren-soma")
# Where the body keeps what it keeps. use_state() moves all of it; the default is ~/seren-soma/state.
STATE = Path.home() / "seren-soma" / "state"
HEARD_DIR = STATE / "heard"
HEARD_LOG = STATE / "heard.jsonl"
TOUCH_LOG = STATE / "touch.jsonl"
CLIPS = STATE / "clips"     # saved sounds, played as they are: ahegao.wav is the one overwhelm plays
SETTINGS = STATE / "settings.json"
SERVICE_LOG = STATE / "seren-soma.log"


def use_state(folder: Path) -> None:
    """Point every file this module keeps at one folder."""
    global STATE, HEARD_DIR, HEARD_LOG, TOUCH_LOG, CLIPS, SETTINGS, SERVICE_LOG
    STATE = Path(folder)
    HEARD_DIR, HEARD_LOG, TOUCH_LOG = STATE / "heard", STATE / "heard.jsonl", STATE / "touch.jsonl"
    CLIPS, SETTINGS, SERVICE_LOG = STATE / "clips", STATE / "settings.json", STATE / "seren-soma.log"
# What the console changes. Night is off until it's turned on; the rest are what tonight settled on.
DEFAULT_SETTINGS = {
    "night": {"enabled": False, "start": "22:30", "end": "07:00", "dimmer": 15, "volume": 30, "brightness": 20},
    "work_hours": {"days": [0, 1, 2, 3, 4], "start": 8, "end": 17},
    "overwhelm": {"enabled": True, "seconds": 15, "gap_seconds": 3, "rest_seconds": 120},
}


def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
    except (OSError, ValueError):
        pass
    return s


def merge_settings(current, change):
    """Only known keys, with sane values. Raises ValueError with what was wrong."""
    out = json.loads(json.dumps(current))
    for section, values in (change or {}).items():
        if section not in DEFAULT_SETTINGS or not isinstance(values, dict):
            raise ValueError(f"no setting called {section}")
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS[section]:
                raise ValueError(f"no setting called {section}.{k}")
            want = DEFAULT_SETTINGS[section][k]
            if isinstance(want, bool):
                if not isinstance(v, bool):
                    raise ValueError(f"{section}.{k} is true or false")
            elif isinstance(want, int):
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise ValueError(f"{section}.{k} is a whole number")
                if section == "night" and v > 100:
                    raise ValueError(f"{section}.{k} is 0 to 100")
                if section == "work_hours" and v > 24:
                    raise ValueError(f"{section}.{k} is an hour, 0 to 24")
            elif isinstance(want, str):
                if not isinstance(v, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                    raise ValueError(f"{section}.{k} is a time like 22:30")
            elif isinstance(want, list):
                if not isinstance(v, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in v):
                    raise ValueError(f"{section}.{k} is a list of days, Monday 0 to Sunday 6")
            out[section][k] = v
    return out

IN_RATE = 16000       # what the device sends
OUT_RATE = 24000      # what we send it
FRAME_MS = 60

LIVE_S = 180          # words this recent mean a call back is a reply, not me barging in
REPLY_WINDOW_S = 10   # how long a call back stays open for his answer
SPEECH_RMS = 500      # 16-bit RMS above this is speech; tune against a real room
END_SILENCE_MS = 900  # this much quiet after speech ends an utterance
MIN_SPEECH_MS = 300   # shorter than this was a noise
MAX_UTTERANCE_S = 20

# A clip whose transcript is nothing but sound tags, like "(keyboard clacking)" or "[music]".
SOUND_TAGS = re.compile(r"^(?:\s|[.,!?…♪-]|\([^)]*\)|\[[^\]]*\]|\*[^*]*\*)*$")


def has_words(text: str) -> bool:
    return SOUND_TAGS.match(text) is None


def token_file(name: str = "token") -> str:
    """A token kept in the state folder, made the first time it's asked for."""
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / name
    if not p.exists():
        p.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


# What was heard is the one thing in the log a person said. "Copy for help" never carries it.
HEARD_LINE = re.compile(r"(heard \([^)]*\): )(.*)$")
BEARER = re.compile(r"(Bearer\s+)\S+", re.I)


def scrub(line: str) -> str:
    """A log line fit to hand someone: what was said becomes how many words, a bearer becomes <token>."""
    m = HEARD_LINE.search(line)
    if m:
        words = len(m.group(2).split())
        line = line[:m.start(2)] + f"[said {words} word{'s' if words != 1 else ''}]"
    return BEARER.sub(r"\1<token>", line)


def lodestar_from_symposium(path: Path) -> tuple[str, str]:
    """The Lodestar route Symposium already holds on this box, rather than a second copy of its token."""
    text = path.read_text(encoding="utf-8")
    block = re.search(r"^lodestar:\s*\n((?:[ \t]+.*\n)+)", text, re.M)
    if not block:
        raise SystemExit(f"no lodestar block in {path}")
    url = re.search(r"^\s*url:\s*(\S+)", block.group(1), re.M)
    tok = re.search(r"^\s*bearer_token:\s*(\S+)", block.group(1), re.M)
    if not url or not tok:
        raise SystemExit(f"lodestar block in {path} lacks url or bearer_token")
    return url.group(1), tok.group(1).strip("'\"")


def wav_bytes(pcm: np.ndarray, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return out.getvalue()


def wav_to_opus(data: bytes) -> list[bytes]:
    """A WAV as the Opus packets the device plays: mono, 24 kHz, one packet per 60 ms."""
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = OUT_RATE
    enc.layout = "mono"
    enc.format = "s16"
    enc.bit_rate = 32000
    enc.options = {"frame_duration": str(FRAME_MS), "application": "voip"}
    enc.open()
    size = OUT_RATE * FRAME_MS // 1000
    resampler = av.AudioResampler(format="s16", layout="mono", rate=OUT_RATE)
    chunks = []
    with av.open(io.BytesIO(data), format="wav") as container:
        for frame in container.decode(audio=0):
            for r in resampler.resample(frame):
                chunks.append(r.to_ndarray().reshape(-1))
    for r in resampler.resample(None):
        chunks.append(r.to_ndarray().reshape(-1))
    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    packets = []
    for start in range(0, len(pcm), size):
        piece = pcm[start:start + size]
        if len(piece) < size:
            piece = np.pad(piece, (0, size - len(piece)))
        frame = av.AudioFrame.from_ndarray(piece.reshape(1, -1).astype(np.int16), format="s16", layout="mono")
        frame.sample_rate = OUT_RATE
        frame.pts = start
        packets += [bytes(p) for p in enc.encode(frame)]
    packets += [bytes(p) for p in enc.encode(None)]
    return packets


class Device:
    """One connected websocket: its audio decoder and the utterance being collected."""

    def __init__(self, ws, session_id):
        self.ws = ws
        self.session_id = session_id
        self.decoder = av.CodecContext.create("libopus", "r")
        self.decoder.sample_rate = IN_RATE
        self.decoder.layout = "mono"
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=IN_RATE)
        self.speaking = False
        self.ready = False                  # its hello has been answered
        self.deaf = False                   # opened by me to say something: what the mic hears is dropped
        self.last_words = time.monotonic()  # the channel hangs up after too long without any
        self.reset()

    def reset(self):
        self.pcm = []
        self.speech_ms = 0
        self.quiet_ms = 0
        self.in_speech = False

    def feed(self, packet: bytes):
        """Decode one frame. Returns a finished utterance's samples, or None."""
        chunks = []
        for frame in self.decoder.decode(av.Packet(packet)):
            # the Opus decoder hands back 48 kHz whatever it was told; bring it to the rate we count in
            for r in self.resampler.resample(frame):
                chunks.append(r.to_ndarray().reshape(-1))
        if not chunks:
            return None
        pcm = np.concatenate(chunks).astype(np.int16)
        ms = 1000 * len(pcm) / IN_RATE
        rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64))))) if len(pcm) else 0.0

        if self.in_speech or rms >= SPEECH_RMS:
            self.pcm.append(pcm)
        if rms >= SPEECH_RMS:
            self.in_speech = True
            self.speech_ms += ms
            self.quiet_ms = 0
        elif self.in_speech:
            self.quiet_ms += ms

        total_ms = sum(len(p) for p in self.pcm) * 1000 / IN_RATE
        if self.in_speech and (self.quiet_ms >= END_SILENCE_MS or total_ms >= MAX_UTTERANCE_S * 1000):
            done = np.concatenate(self.pcm)
            enough = self.speech_ms >= MIN_SPEECH_MS
            self.reset()
            return done if enough else None
        return None


class Link:
    def __init__(self, args):
        """args: what config.SomaConfig.runtime() hands over: host, port, advertise, voice, device,
        idle_close, api_token, device_token, robot_token, device_from, lodestar_url, lodestar_token."""
        self.args = args
        self.token = args.api_token
        self.device_token = args.device_token
        self.robot_token = args.robot_token
        self.lodestar_url, self.lodestar_token = args.lodestar_url, args.lodestar_token
        self.started = time.time()
        self.device: Device | None = None
        self.http: ClientSession | None = None
        self.say_lock = asyncio.Lock()
        self.saying = 0      # replies being rendered or spoken: the channel stays up for them
        self.last_heard = 0.0  # when Chad last said real words: within LIVE_S, we're mid-conversation
        self.waking = False
        self.settings = load_settings()
        self.feed = []              # recent touches for the console: what the hand did, what it was read as, what showed
        self.feed_seq = 0
        self.overwhelm_state = {"running": False, "progress": 0.0}
        self.night_now = None       # what was last sent: True, False, or None before the first look
        HEARD_DIR.mkdir(parents=True, exist_ok=True)

    def lodestar_headers(self):
        return {"Authorization": f"Bearer {self.lodestar_token}"}

    async def speak_wav(self, text: str, voice: str) -> bytes:
        async with self.http.post(f"{self.lodestar_url}/api/v1/speak", headers=self.lodestar_headers(),
                                  json={"text": text, "voice": voice, "holder": "seren-voice"},
                                  timeout=ClientTimeout(total=240)) as r:
            if r.status != 200:
                raise web.HTTPBadGateway(text=f"speak: {r.status} {(await r.text())[:200]}")
            return await r.read()

    async def hear(self, pcm: np.ndarray):
        name = time.strftime("%Y%m%d-%H%M%S")
        data = wav_bytes(pcm, IN_RATE)
        path = HEARD_DIR / f"{name}.wav"
        path.write_bytes(data)
        form = FormData()
        form.add_field("file", data, filename=f"{name}.wav", content_type="audio/wav")
        form.add_field("language", "en")
        form.add_field("holder", "seren-voice")
        text = ""
        transcribed = False
        try:
            async with self.http.post(f"{self.lodestar_url}/api/v1/hear", headers=self.lodestar_headers(), data=form,
                                      timeout=ClientTimeout(total=120)) as r:
                body = await r.json(content_type=None)
                text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
                transcribed = r.status == 200
                if not transcribed:
                    log.warning("hear: %s %s", r.status, str(body)[:200])
        except Exception as e:
            log.warning("hear failed: %s", e)
        # Only a clip that was really transcribed can be called noise: a failed hear may hold words.
        if transcribed and not has_words(text):
            path.unlink(missing_ok=True)
            log.info("dropped a clip with no words (%.1fs)", len(pcm) / IN_RATE)
            return
        entry = {"t": time.time(), "wav": str(path), "seconds": round(len(pcm) / IN_RATE, 1), "text": text}
        with HEARD_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info("heard (%ss): %s", entry["seconds"], text)
        self.last_heard = time.monotonic()
        d = self.device
        if d is not None and text:
            d.last_words = time.monotonic()
            await self.send(d, {"type": "stt", "text": text})

    async def send(self, d: Device, message: dict):
        await d.ws.send_str(json.dumps({"session_id": d.session_id, **message}))

    async def wake_device(self):
        """Open the channel from this end. It stays deaf, and hangs up after the line: I can get
        Chad's attention without the mic listening to a room he didn't open it in."""
        headers = self.robot_headers()
        self.waking = True
        try:
            async with self.http.post(f"http://{self.args.device}:8080/api/wake", headers=headers, data=b"{}",
                                      timeout=ClientTimeout(total=6)) as r:
                if r.status != 200:
                    raise web.HTTPConflict(text=f"the robot wouldn't wake: {r.status} {(await r.text())[:120]}")
            end = time.monotonic() + 12
            while self.device is None or not self.device.ready:
                if time.monotonic() > end:
                    raise web.HTTPGatewayTimeout(text="the robot didn't open its channel")
                await asyncio.sleep(0.1)
        finally:
            self.waking = False

    async def say(self, text: str, voice: str, emotion: str | None, wake: bool = False, wav: bytes | None = None):
        self.saying += 1
        try:
            return await self._say(text, voice, emotion, wake, wav)
        finally:
            self.saying -= 1

    async def _say(self, text: str, voice: str, emotion: str | None, wake: bool, wav: bytes | None = None):
        # the words first: rendering is the slow part, so the channel is open for as short a time as can be.
        # A saved clip is already rendered.
        if wav is None:
            wav = await self.speak_wav(text, voice)
        packets = await asyncio.to_thread(wav_to_opus, wav)
        woke = False
        if self.device is None and wake:
            await self.wake_device()
            woke = True
        async with self.say_lock:
            d = self.device
            if d is None:
                raise web.HTTPConflict(text="the device has no audio channel open: wake it first")
            d.speaking = True
            try:
                await self.send(d, {"type": "tts", "state": "start"})
                if emotion:
                    await self.send(d, {"type": "llm", "emotion": emotion, "text": ""})
                # no text: spoken out loud, the words need no bubble, and Chad's art gets the screen
                await self.send(d, {"type": "tts", "state": "sentence_start"})
                started = time.monotonic()
                for i, packet in enumerate(packets):
                    if not d.speaking:
                        break
                    await d.ws.send_bytes(packet)
                    # a little ahead of real time, so the device's queue never runs dry
                    lead = started + (i * FRAME_MS / 1000) - 0.2 - time.monotonic()
                    if lead > 0:
                        await asyncio.sleep(lead)
                remaining = started + (len(packets) * FRAME_MS / 1000) - time.monotonic()
                if remaining > 0 and d.speaking:
                    await asyncio.sleep(remaining)
                await self.send(d, {"type": "tts", "state": "stop"})
            finally:
                d.speaking = False
                d.last_words = time.monotonic()
                d.reset()
            if woke or d.deaf:
                if time.monotonic() - self.last_heard < LIVE_S:
                    # he's talking to me: stay for his answer, then the usual hang-up applies
                    d.deaf = False
                    d.last_words = time.monotonic() - max(0, self.args.idle_close - REPLY_WINDOW_S)
                else:
                    await asyncio.sleep(0.5)    # the tail of the audio, still in the device's queue
                    await d.ws.close()
        return len(packets) * FRAME_MS / 1000

    # -- routes ---------------------------------------------------------------------------

    def robot_headers(self) -> dict:
        return {"X-StackChan-Token": self.robot_token} if self.robot_token else {}

    async def ota(self, request):
        """The check-in. No mqtt, no activation, no firmware: the websocket is all it gets, and only
        the robot gets it. Anyone else asking is told no and logged."""
        if self.args.device_from and request.remote not in self.args.device_from:
            log.warning("check-in from %s refused: only %s get the voice channel", request.remote,
                        ", ".join(sorted(self.args.device_from)))
            raise web.HTTPForbidden(text="not this body's robot")
        host = self.args.advertise
        return web.json_response({
            "websocket": {"url": f"ws://{host}:{self.args.port}/voice", "token": self.device_token, "version": 1},
            "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": -time.timezone // 60},
        })

    async def voice(self, request):
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {self.device_token}"):
            return web.Response(status=401, text="bad token")
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, heartbeat=None)
        await ws.prepare(request)
        d = Device(ws, secrets.token_hex(4))
        d.deaf = self.waking      # set before anything can arrive from this device
        self.device = d
        log.info("device connected: %s", request.headers.get("Device-Id", "?"))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if d.speaking or d.deaf:
                        continue
                    done = d.feed(msg.data)
                    if done is not None:
                        asyncio.create_task(self.hear(done))
                elif msg.type == WSMsgType.TEXT:
                    await self.on_json(d, json.loads(msg.data))
                elif msg.type == WSMsgType.ERROR:
                    log.warning("websocket error: %s", ws.exception())
        finally:
            if self.device is d:
                self.device = None
            log.info("device gone")
        return ws

    async def on_json(self, d: Device, m: dict):
        kind = m.get("type")
        if kind == "hello":
            log.info("hello: %s", json.dumps(m)[:300])
            await d.ws.send_str(json.dumps({
                "type": "hello", "transport": "websocket", "session_id": d.session_id,
                "audio_params": {"format": "opus", "sample_rate": OUT_RATE, "channels": 1, "frame_duration": FRAME_MS},
            }))
            d.ready = True
        elif kind == "listen":
            log.info("listen: %s %s", m.get("state"), m.get("text", ""))
            if m.get("state") == "start":
                d.last_words = time.monotonic()
                d.reset()
        elif kind == "abort":
            log.info("abort: %s", m.get("reason", ""))
            d.speaking = False
        else:
            log.info("%s: %s", kind, json.dumps(m)[:200])

    def authorized(self, request):
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {self.token}"):
            raise web.HTTPUnauthorized(text="bad token")

    async def say_route(self, request):
        self.authorized(request)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="no text")
        seconds = await self.say(text, body.get("voice") or self.args.voice, body.get("emotion"), bool(body.get("wake")))
        return web.json_response({"ok": True, "seconds": seconds})

    def clip(self, name: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""):
            raise web.HTTPBadRequest(text="clip names are letters, digits, _ and -")
        p = CLIPS / f"{name}.wav"
        if not p.exists():
            raise web.HTTPNotFound(text=f"no clip {name}")
        return p.read_bytes()

    async def clip_route(self, request):
        """POST /clip {"name": "ahegao_take2", "emotion": "ahegao"}: play a saved sound, waking the robot for it."""
        self.authorized(request)
        body = await request.json()
        seconds = await self.say("", self.args.voice, body.get("emotion"), True, self.clip(body.get("name", "")))
        return web.json_response({"ok": True, "seconds": seconds})

    async def overwhelm(self, base, headers):
        """Tip over: the face, held through the call; a flash on the rails; the clip; then melty, then back."""
        async def post(path, body):
            try:
                async with self.http.post(f"{base}{path}", headers=headers, json=body, timeout=ClientTimeout(total=4)) as r:
                    await r.read()
            except Exception as e:
                log.warning("overwhelm %s: %s", path, e)

        log.info("overwhelmed")
        await post("/emotion", {"name": "ahegao", "rest": True})   # the voice code's "neutral" means ahegao for now
        for _ in range(2):
            await post("/leds", {"frame": [[168, 0, 110]] * 12})
            await asyncio.sleep(0.15)
            await post("/leds", {"frame": [[0, 0, 0]] * 12})
            await asyncio.sleep(0.1)
        try:
            await self.say("", self.args.voice, None, True, self.clip("ahegao"))
        except Exception as e:
            log.warning("overwhelm clip: %s", e)
            await asyncio.sleep(3)
        await post("/emotion", {"name": "melty"})                   # and the resting pose goes with it
        await asyncio.sleep(5)
        await post("/emotion", {"name": "neutral"})

    async def watch_night(self):
        """Night caps for the robot's lights, speaker and screen, every minute while it's night, and off
        when it isn't. Every minute rather than once, so a reboot at night comes back dim."""
        headers = self.robot_headers()
        while True:
            n = self.settings["night"]
            night = n["enabled"] and in_window(time.localtime(), n["start"], n["end"])
            body = {"night": {"dimmer": n["dimmer"], "volume": n["volume"], "brightness": n["brightness"]} if night else None}
            try:
                async with self.http.post(f"http://{self.args.device}:8080/api/settings", headers=headers, json=body,
                                          timeout=ClientTimeout(total=6)) as r:
                    await r.read()
                    if night != self.night_now:
                        log.info("night: %s", "on" if night else "off")
                    self.night_now = night
            except Exception:
                pass
            await asyncio.sleep(60)

    async def settings_get(self, request):
        self.authorized(request)
        return web.json_response({**self.settings, "night_now": self.night_now})

    async def settings_post(self, request):
        """POST /settings {"night": {"enabled": true, "start": "22:30"}}: only what's sent changes, saved at once."""
        self.authorized(request)
        try:
            self.settings = merge_settings(self.settings, await request.json())
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        tmp = SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, indent=1), encoding="utf-8")
        tmp.replace(SETTINGS)
        self.night_task.cancel()                      # the new hours or caps go to the robot now
        self.night_task = asyncio.create_task(self.watch_night())
        return web.json_response(self.settings)

    async def touch_feed(self, request):
        """GET /touch?after=<seq>: touches since then, and where overwhelm stands."""
        self.authorized(request)
        after = int(request.query.get("after", "0") or 0)
        return web.json_response({"events": [e for e in self.feed if e["seq"] > after], "last": self.feed_seq,
                                  "overwhelm": self.overwhelm_state})

    async def health(self, request):
        """Public: is it up, and which version. Nothing about what it holds."""
        from . import __version__
        return web.json_response({"ok": True, "service": "seren-soma", "version": __version__})

    async def logs(self, request):
        """GET /api/logs?lines=200: the end of the service log, scrubbed (see scrub), for a person to send
        someone helping them. Behind the API token like everything else that isn't the page itself."""
        self.authorized(request)
        try:
            n = max(1, min(2000, int(request.query.get("lines", "200"))))
        except ValueError:
            n = 200
        try:
            with SERVICE_LOG.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 400_000))
                tail = f.read().decode("utf-8", "replace").splitlines()[-n:]
        except OSError:
            tail = []
        return web.json_response({"lines": [scrub(line) for line in tail], "file": str(SERVICE_LOG)})

    async def about(self, request):
        """What the viewer's front page shows: the body as this server sees it."""
        self.authorized(request)
        from . import __version__
        d = self.device
        return web.json_response({
            "version": __version__, "up_seconds": round(time.time() - self.started),
            "robot": self.args.device, "advertise": self.args.advertise,
            "channel_open": d is not None, "speaking": bool(d and d.speaking), "deaf": bool(d and d.deaf),
            "night_now": self.night_now, "overwhelm": self.overwhelm_state, "state": str(STATE),
        })

    async def status(self, request):
        self.authorized(request)
        return web.json_response({"ok": True, "device": self.device is not None,
                                  "speaking": bool(self.device and self.device.speaking)})

    async def watch_touch(self):
        """Read the head sensor's raw feed and log what the hand is doing, so I can feel it:
        each touch, and a summary of each session once the hand has gone away (touch_read.py)."""
        base = f"http://{self.args.device}:8080/api"
        headers = self.robot_headers()
        reader = TouchReader()
        since = 0
        spots = None        # the names of the face's touch spots, read again after a reboot and every 30 s
        spots_at = 0.0
        recent = []         # the last few head touches, to tell scritching from what each bit looks like
        overwhelm = Overwhelm()
        knobs = None
        kept_at = 0.0       # when melty was last renewed under a hand that hasn't lifted
        head = None         # what the face says its head touches show, None if it doesn't
        uptime = None       # the robot's clock, and when it was read
        read_at = 0.0

        def write(entry):
            with TOUCH_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), **entry}) + "\n")

        while True:
            await asyncio.sleep(1.0)
            o = self.settings["overwhelm"]
            if knobs != o:       # the console changed them
                knobs = dict(o)
                overwhelm.after_ms, overwhelm.gap_ms, overwhelm.rest_ms = o["seconds"] * 1000, o["gap_seconds"] * 1000, o["rest_seconds"] * 1000
            try:
                if uptime is None or time.monotonic() - read_at > 5:
                    async with self.http.get(f"{base}/status", headers=headers, timeout=ClientTimeout(total=4)) as r:
                        now_up = (await r.json(content_type=None))["uptimeMs"]
                    if uptime is None:      # first look: what's already in the feed happened before this server
                        since = now_up
                    elif now_up < uptime:   # the robot rebooted, and its clock with it (a new face, maybe)
                        reader.reset()
                        since = 0
                        spots = None
                        recent = []
                    uptime, read_at = now_up, time.monotonic()
                if spots is None or time.monotonic() - spots_at > 30:
                    async with self.http.get(f"{base}/touch-spots", headers=headers, timeout=ClientTimeout(total=4)) as r:
                        about = await r.json(content_type=None) if r.status == 200 else {}
                    spots = about.get("spots", [])
                    head = about.get("head")
                    spots_at = time.monotonic()
                async with self.http.get(f"{base}/touch-raw?since={since}", headers=headers,
                                         timeout=ClientTimeout(total=4)) as r:
                    text = await r.text() if r.status == 200 else ""
            except Exception:
                continue
            rows = [tuple(int(x) for x in line.split()) for line in text.splitlines() if len(line.split()) == 5]
            if rows:
                since = rows[-1][0]
            events, sessions = reader.feed(rows, now_ms=uptime + int((time.monotonic() - read_at) * 1000))
            for e in events:
                if e["kind"] == "touch_spot" and e["index"] < len(spots):
                    e["spot"] = spots[e["index"]]
                write(e)
            recent = (recent + [e for e in events if e["kind"] in HEAD_KINDS])[-6:]
            # overwhelm runs on the sensor's clock: every head touch, then a hand that hasn't lifted yet
            tip = False
            for e in events:
                if e["kind"] in HEAD_KINDS:
                    tip |= overwhelm.touch(e["t_ms"], e["t_ms"] + e["ms"], scritchy(e) or (overwhelm.running and scritching(recent)))
            held = reader.open_touch()
            if held:
                tip |= overwhelm.touch(held[0], held[1], True, still_going=True)
            # only the last one: a burst shouldn't queue up a parade of faces
            react = None
            if events:
                last = events[-1]
                scritch = last["kind"] in HEAD_KINDS and (last["kind"] == "scritch" or scritching(recent))
                react = reaction_for({"kind": "scritch"} if scritch else last, head)
            elif held and overwhelm.running and time.monotonic() - kept_at > 2:
                # mid-scratch, the hand still down: keep the scritch face up rather than let it time out
                react = reaction_for({"kind": "scritch"}, head)
            if react and react == reaction_for({"kind": "scritch"}, head):
                kept_at = time.monotonic()
            # earned, never by accident: not with a call open, not in his work hours, not if it's switched off
            wh = self.settings["work_hours"]
            tipped = False
            if tip and o["enabled"] and (head or {}).get("scritch") and self.device is None \
                    and not work_hours(time.localtime(), (wh["start"], wh["end"]), wh["days"]):
                asyncio.create_task(self.overwhelm(base, headers))
                react = None
                tipped = True
            self.overwhelm_state = {"running": overwhelm.running, "progress": round(overwhelm.progress(), 3)}
            for e in events:
                self.feed_seq += 1
                self.feed.append({"seq": self.feed_seq, "t": time.time(), **e,
                                  "showed": (react[0] if react else None) if e is events[-1] else None,
                                  "overwhelmed": tipped and e is events[-1]})
            self.feed = self.feed[-300:]
            if react:
                try:
                    async with self.http.post(f"{base}/react", headers=headers, json={"name": react[0], "ms": react[1]},
                                              timeout=ClientTimeout(total=4)) as r:
                        await r.read()
                except Exception:
                    pass
            for s in sessions:
                write({"session": s})
                log.info("touched: %s", s)

    async def watch_silence(self):
        """Hang up when nobody has said a word for a while, so a forgotten tap can't leave the mic open."""
        while True:
            await asyncio.sleep(1.0)
            d = self.device
            if d is None or d.speaking or self.saying or self.say_lock.locked():
                continue
            if time.monotonic() - d.last_words >= self.args.idle_close:
                log.info("no words for %ss: closing the channel", self.args.idle_close)
                await d.ws.close()

    async def on_start(self, app):
        self.http = ClientSession()
        self.touch_task = asyncio.create_task(self.watch_touch())
        self.silence_task = asyncio.create_task(self.watch_silence())
        self.night_task = asyncio.create_task(self.watch_night())

    async def on_stop(self, app):
        self.touch_task.cancel()
        self.silence_task.cancel()
        self.night_task.cancel()
        await self.http.close()


def make_app(link: "Link", viewer_html: str = "") -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.on_startup.append(link.on_start)
    app.on_cleanup.append(link.on_stop)
    app.router.add_route("*", "/ota/", link.ota)
    app.router.add_get("/voice", link.voice)
    app.router.add_post("/say", link.say_route)
    app.router.add_post("/clip", link.clip_route)
    app.router.add_get("/status", link.status)
    app.router.add_get("/settings", link.settings_get)
    app.router.add_post("/settings", link.settings_post)
    app.router.add_get("/touch", link.touch_feed)
    app.router.add_get("/health", link.health)
    app.router.add_get("/api/about", link.about)
    app.router.add_get("/api/logs", link.logs)
    if viewer_html:
        async def viewer(request):
            return web.Response(text=viewer_html, content_type="text/html",
                                headers={"Cache-Control": "no-store, must-revalidate"})

        async def root(request):
            raise web.HTTPFound("/viewer")
        app.router.add_get("/viewer", viewer)
        app.router.add_get("/", root)
    return app
