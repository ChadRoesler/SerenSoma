"""The body's nervous system: stands where Xiaozhi's cloud stood.

The robot checks in at POST /ota/, is told to use our websocket, and speaks Xiaozhi's protocol to
/voice (see firmware/docs/voice-link.md). What it hears goes to Lodestar's hear route and lands in
<state>/heard.jsonl. What it says comes from Lodestar's speak route, by POST /say. It reads the head
sensor into touches and faces, and it owns night mode and work hours.

There is no mind in here: whoever answers reads heard.jsonl and POSTs /say.

TWO TOKENS. The API token (<state>/token, or server.bearer_token_*) guards /say, /heard, /settings,
/touch, /clip, /status and /api/logs, and never leaves this box. The device token (<state>/device_token) is
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
from aiohttp import ClientError, ClientSession, ClientTimeout, FormData, WSMsgType, web

from .bench import Bench
from .timing import VoiceClock
from .touch_read import HEAD_KINDS, TouchReader, in_window, label_scratching, reaction_for, scritching

log = logging.getLogger("seren-soma")
# Where the body keeps what it keeps. use_state() moves all of it; the default is ~/seren-soma/state.
STATE = Path.home() / "seren-soma" / "state"
HEARD_DIR = STATE / "heard"
HEARD_LOG = STATE / "heard.jsonl"
TOUCH_LOG = STATE / "touch.jsonl"
CLIPS = STATE / "clips"     # saved sounds the voice server plays through the voice channel (POST /clip)
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
    # When the person's working: a fact about the house. Nothing here acts on it; SerenPsycheion reads it
    # and keeps a face's build-ups out of those hours.
    "work_hours": {"days": [0, 1, 2, 3, 4], "start": 8, "end": 17},
    # The dead-air timer: an open channel with no words this long hangs up. Live: the next silence
    # uses it. idle_close_seconds in seren-soma.yaml is only where it starts on a fresh install.
    # end_silence_ms: how much quiet after talking counts as "finished". Every turn waits this long
    # before anything else can start, so it is the first thing to shorten for quick back and forth,
    # and the first thing to lengthen if it cuts people off mid-thought.
    "channel": {"dead_air_seconds": 60, "end_silence_ms": 900},
}


DEAD_AIR_RANGE = (10, 600)
END_SILENCE_RANGE = (300, 2500)
CHANNEL_RANGES = {"dead_air_seconds": ("seconds", DEAD_AIR_RANGE), "end_silence_ms": ("milliseconds", END_SILENCE_RANGE)}


def load_settings(dead_air_default=None):
    """The saved settings over the defaults. dead_air_default: the yaml's idle_close_seconds, the
    starting value until one is saved."""
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    if dead_air_default is not None:
        lo, hi = DEAD_AIR_RANGE
        s["channel"]["dead_air_seconds"] = max(lo, min(hi, int(round(float(dead_air_default)))))
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
                if section == "channel":
                    unit, (lo, hi) = CHANNEL_RANGES[k]
                    if not lo <= v <= hi:
                        raise ValueError(f"{section}.{k} is {unit}, {lo} to {hi}")
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
REPLY_WINDOW_S = 10   # how long a call back stays open for an answer
SPEECH_RMS = 500      # 16-bit RMS above this is speech; tune against a real room
END_SILENCE_MS = 900  # this much quiet after speech ends an utterance, until the live setting says otherwise
MIN_SPEECH_MS = 300   # shorter than this was a noise
MAX_UTTERANCE_S = 20
# The speech service puts a model away when nobody has used it for a while, and the next line then
# waits for it to load: 40 s for the voice on 12 Nov 2026, against about a second once it's loaded.
# So when the robot opens its channel and the service has sat idle this long, a throwaway line is
# rendered and a moment of quiet transcribed, and both are loaded by the time anyone speaks.
WARM_AFTER_S = 90
WARM_LINE = "Okay."

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


# A sentence: up to its closing punctuation (and any quote or bracket after it), when a space or
# the end follows. "3.5" and "e.g" stay whole; "Mr. Smith" splits, and that's fine for speech.
SENTENCE = re.compile(r'''.+?(?:[.!?…]+["')\]]*(?=\s|$)|$)''', re.S)
SHORT_PIECE = 12


def split_sentences(text: str) -> list[str]:
    """The pieces a line is rendered and spoken in. The first piece is what the room waits for, so
    it's kept as short as it comes, a lone "Ha!" included. After it, a piece too short to carry its
    own tone joins the one before."""
    parts = [m.group(0).strip() for m in SENTENCE.finditer(text.strip())]
    parts = [x for x in parts if x]
    out: list[str] = []
    for part in parts:
        if len(out) > 1 and len(part) < SHORT_PIECE:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out or ([text.strip()] if text.strip() else [])


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
        self.ended_quiet_ms = 0
        self.reset()

    def reset(self):
        self.pcm = []
        self.speech_ms = 0
        self.quiet_ms = 0
        self.in_speech = False

    def feed(self, packet: bytes, end_silence_ms: int = END_SILENCE_MS):
        """Decode one frame. Returns a finished utterance's samples, or None. ended_quiet_ms is then
        how much quiet it waited through, so whoever times the turn knows when the talking stopped."""
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
        if self.in_speech and (self.quiet_ms >= end_silence_ms or total_ms >= MAX_UTTERANCE_S * 1000):
            done = np.concatenate(self.pcm)
            enough = self.speech_ms >= MIN_SPEECH_MS
            self.ended_quiet_ms = self.quiet_ms
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
        # "is there a newer seren-soma": cosmetic, never fatal, the family's checker
        try:
            from seren_meninges.updates import UpdateChecker

            from . import __version__
            self.updates = UpdateChecker("seren-soma", enabled=getattr(args, "updates", True),
                                         fallback_version=__version__)
        except Exception as exc:  # noqa: BLE001
            self.updates = None
            log.info("update checking unavailable (%s)", exc)
        self.device: Device | None = None
        self.http: ClientSession | None = None
        self.say_lock = asyncio.Lock()
        self.saying = 0      # replies being rendered or spoken: the channel stays up for them
        self.last_heard = 0.0  # when someone last said real words: within LIVE_S, we're mid-conversation
        self.waking = False
        self.settings = load_settings(getattr(args, "idle_close", None))
        self.feed = []              # recent touches for the console: what the hand did, what it was read as, what showed
        self.feed_seq = 0
        self.hand = {"down": False}  # the head touch the hand hasn't lifted from yet, on the robot's clock
        self.night_now = None       # what was last sent: True, False, or None before the first look
        self.firmware_armed = None  # {"version", "file"}: offered on the next check-in, once
        self.clock = VoiceClock()   # how long each leg of a turn took (timing.py); no words in it
        self.heard_recent = []      # what was heard since this server started, for GET /heard
        self.heard_event = asyncio.Event()  # set, and replaced, each time something is heard
        self.speech_used = 0.0      # when the speech service last rendered or transcribed for us (monotonic)
        HEARD_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def dead_air(self) -> int:
        """Seconds of no words before an open channel hangs up: the live setting, read every time."""
        return int(self.settings["channel"]["dead_air_seconds"])

    @property
    def end_silence_ms(self) -> int:
        """How much quiet ends an utterance: the live setting, read on every frame."""
        return int(self.settings["channel"]["end_silence_ms"])

    def lodestar_headers(self):
        return {"Authorization": f"Bearer {self.lodestar_token}"}

    async def warm(self):
        """Load the voice and the ears before they're needed (see WARM_AFTER_S). Never raises."""
        if time.monotonic() - self.speech_used < WARM_AFTER_S:
            return
        self.speech_used = time.monotonic()    # now, so two openings in a row warm once
        started = time.monotonic()
        try:
            form = FormData()
            form.add_field("file", wav_bytes(np.zeros(IN_RATE // 2, dtype=np.int16), IN_RATE), filename="quiet.wav",
                           content_type="audio/wav")
            form.add_field("language", "en")
            form.add_field("holder", "seren-voice")

            async def ears():
                async with self.http.post(f"{self.lodestar_url}/api/v1/hear", headers=self.lodestar_headers(), data=form,
                                          timeout=ClientTimeout(total=120)) as r:
                    await r.read()

            await asyncio.gather(self.speak_wav(WARM_LINE, self.args.voice), ears())
            log.info("warmed the voice and the ears in %.1fs", time.monotonic() - started)
        except Exception as e:  # noqa: BLE001 - a warm-up that fails costs nothing but the wait it would have saved
            log.info("warm-up didn't finish: %s", e)

    async def speak_wav(self, text: str, voice: str) -> bytes:
        self.speech_used = time.monotonic()
        async with self.http.post(f"{self.lodestar_url}/api/v1/speak", headers=self.lodestar_headers(),
                                  json={"text": text, "holder": "seren-voice", **({"voice": voice} if voice else {})},
                                  timeout=ClientTimeout(total=240)) as r:
            if r.status != 200:
                raise web.HTTPBadGateway(text=f"speak: {r.status} {(await r.text())[:200]}")
            return await r.read()

    async def hear(self, pcm: np.ndarray, detected: float | None = None, quiet_ms: float = 0):
        """detected: when the utterance was called finished (time.monotonic()). quiet_ms: how much quiet
        that took, so the talking stopped at detected - quiet_ms."""
        detected = time.monotonic() if detected is None else detected
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
        self.speech_used = time.monotonic()
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
        wordless = transcribed and not has_words(text)
        self.clock.heard(speech_end=detected - quiet_ms / 1000, detected=detected, done=time.monotonic(),
                         audio_s=len(pcm) / IN_RATE, words=0 if wordless else len(text.split()), transcribed=transcribed)
        if wordless:
            path.unlink(missing_ok=True)
            log.info("dropped a clip with no words (%.1fs)", len(pcm) / IN_RATE)
            return
        entry = {"t": time.time(), "wav": str(path), "seconds": round(len(pcm) / IN_RATE, 1), "text": text}
        with HEARD_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info("heard (%ss): %s", entry["seconds"], text)
        self.last_heard = time.monotonic()
        if text:
            # whoever answers is waiting on GET /heard: wake them now rather than at their next look
            self.heard_recent = (self.heard_recent + [{"t": entry["t"], "seconds": entry["seconds"], "text": text}])[-100:]
            waiting, self.heard_event = self.heard_event, asyncio.Event()
            waiting.set()
        d = self.device
        if d is not None and text:
            d.last_words = time.monotonic()
            await self.send(d, {"type": "stt", "text": text})

    async def send(self, d: Device, message: dict):
        await d.ws.send_str(json.dumps({"session_id": d.session_id, **message}))

    async def wake_device(self):
        """Open the channel from this end. It stays deaf, and hangs up after the line: I can get
        someone's attention without the mic listening to a room nobody opened it in."""
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

    async def say(self, text: str, voice: str, emotion: str | None, wake: bool = False, wav: bytes | None = None,
                  whole: bool = False):
        self.saying += 1
        try:
            return await self._say(text, voice, emotion, wake, wav, whole)
        finally:
            self.saying -= 1

    async def _say(self, text: str, voice: str, emotion: str | None, wake: bool, wav: bytes | None = None,
                   whole: bool = False):
        """A line goes out a sentence at a time: the first is rendered and starts playing while the
        rest are still being rendered behind it, so the room waits for one sentence, not the whole
        line. whole: render it in one piece (one breath, the tone carried right through), and wait
        for all of it. A saved clip is already rendered and is one piece."""
        asked = time.monotonic()
        pieces = [text] if (wav is not None or whole) else split_sentences(text)
        ready: asyncio.Queue = asyncio.Queue()    # each piece's packets in order, then None; or what went wrong

        async def render():
            try:
                for piece in pieces:
                    data = wav if wav is not None else await self.speak_wav(piece, voice)
                    await ready.put(await asyncio.to_thread(wav_to_opus, data))
                await ready.put(None)
            except Exception as e:  # noqa: BLE001 - handed to the one speaking, who knows what's been said so far
                await ready.put(e)

        renderer = asyncio.create_task(render())
        try:
            # the first words first: rendering is the slow part, so the channel is open for as short a time as can be
            packets = await ready.get()
            if isinstance(packets, Exception):
                raise packets
            rendered_first = time.monotonic()
            woke = False
            if self.device is None and wake:
                await self.wake_device()
                woke = True
            sent, first_audio, failed = 0, None, None
            async with self.say_lock:
                d = self.device
                if d is None:
                    raise web.HTTPConflict(text="the device has no audio channel open: wake it first")
                d.speaking = True
                try:
                    await self.send(d, {"type": "tts", "state": "start"})
                    if emotion:
                        await self.send(d, {"type": "llm", "emotion": emotion, "text": ""})
                    # no text: spoken out loud, the words need no bubble, and the face gets the screen
                    await self.send(d, {"type": "tts", "state": "sentence_start"})
                    next_at = time.monotonic()    # when the packet about to be sent is due to play
                    while packets is not None and d.speaking:
                        if time.monotonic() > next_at:
                            next_at = time.monotonic()    # this piece wasn't ready in time: a pause, then on from now
                        for packet in packets:
                            if not d.speaking:
                                break
                            await d.ws.send_bytes(packet)
                            if first_audio is None:
                                first_audio = time.monotonic()
                            sent += 1
                            next_at += FRAME_MS / 1000
                            # a little ahead of real time, so the device's queue never runs dry
                            lead = next_at - 0.2 - time.monotonic()
                            if lead > 0:
                                await asyncio.sleep(lead)
                        packets = await ready.get()
                        if isinstance(packets, Exception):
                            failed, packets = packets, None
                    remaining = next_at - time.monotonic()
                    if remaining > 0 and d.speaking:
                        await asyncio.sleep(remaining)
                    await self.send(d, {"type": "tts", "state": "stop"})
                finally:
                    d.speaking = False
                    d.last_words = time.monotonic()
                    d.reset()
                if woke or d.deaf:
                    if time.monotonic() - self.last_heard < LIVE_S:
                        # he's talking to me: stay for an answer, then the usual hang-up applies
                        d.deaf = False
                        d.last_words = time.monotonic() - max(0, self.dead_air - REPLY_WINDOW_S)
                    else:
                        await asyncio.sleep(0.5)    # the tail of the audio, still in the device's queue
                        await d.ws.close()
            if first_audio is not None:
                turn = self.clock.said(asked=asked, rendered_first=rendered_first, first_audio=first_audio,
                                       done=time.monotonic(), pieces=len(pieces), chars=len(text),
                                       audio_s=sent * FRAME_MS / 1000, woke=woke, clip=wav is not None)
                log.info("said %s piece(s), %.1fs: first sound %d ms after the words arrived%s", len(pieces), turn["audio_s"],
                         turn["to_first_sound_ms"],
                         f", {turn['last_word_to_first_sound_ms']} ms after he stopped talking" if "answers" in turn else "")
            if failed is not None:
                raise failed    # part of the line was said; the rest couldn't be rendered
            return sent * FRAME_MS / 1000
        finally:
            renderer.cancel()

    # -- routes ---------------------------------------------------------------------------

    def robot_headers(self) -> dict:
        """One dict for the life of the server, refilled on every call: the loops that took it once at
        start (touch, night) see a rotated token without being restarted."""
        h = self.__dict__.setdefault("_robot_headers", {})
        h.clear()
        if self.robot_token:
            h["X-StackChan-Token"] = self.robot_token
        return h

    # -- setting the robot up (robot_setup.py) ----------------------------------------------------

    def robot_token_path(self) -> Path | None:
        """The file the robot's token lives in here, if it's a file: where a new one gets written."""
        p = getattr(self.args, "robot_token_file", "")
        return Path(p) if p else None

    async def _setup_body(self, request):
        from . import robot_setup as rs
        self.authorized(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="expected a JSON object")
        try:
            keys = rs.keys_from(body, advertise=self.args.advertise, port=self.args.port)
        except rs.SetupError as e:
            raise web.HTTPBadRequest(text=str(e))
        return rs, body, keys

    def _token_nowhere(self):
        return web.HTTPConflict(text="the robot's token here comes from an env var, a keychain or the config itself, not "
                                     "a file, so a new one has nowhere to be written down. Point robot.token_file at a "
                                     "file, or leave the robot's token unset to use <state>/control_token.")

    async def robot_config(self, request):
        """POST /robot/config: set up a robot that's on the network, through its /api/config.
        Body: soma.yaml's keys (voice defaults to this box), plus "rotate_token": true for a new token
        and "reboot" (default true; always, with a new token). Answers with the names of what the robot
        saved, never values. Wi-Fi passwords pass through and are not kept or logged."""
        rs, body, keys = await self._setup_body(request)
        new = None
        path = self.robot_token_path()
        if body.get("rotate_token"):
            if path is None:
                raise self._token_nowhere()
            new = rs.new_token()
            keys["token"] = new
        reboot = bool(body.get("reboot", True)) or new is not None
        url = f"{self.args.robot_url.rstrip('/')}/api/config"
        try:
            async with self.http.post(url, headers=dict(self.robot_headers()), json={**keys, "reboot": reboot},
                                      timeout=ClientTimeout(total=8)) as r:
                text = await r.text()
                if r.status == 404:
                    raise web.HTTPBadGateway(text="this robot's firmware has no /api/config yet: update it, or use a "
                                                  "soma.yaml card")
                if r.status != 200:
                    raise web.HTTPBadGateway(text=f"the robot said {r.status}: {text[:200]}")
                answer = json.loads(text)
        except web.HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise web.HTTPBadGateway(text=f"couldn't reach the robot at {self.args.robot_url}: {e}")
        changed = [str(c) for c in answer.get("changed", [])]
        rebooting = bool(answer.get("rebooting"))
        if new is not None:
            if "token" not in changed:
                raise web.HTTPBadGateway(text="the robot didn't save the new token, so nothing changed here")
            # The robot has it (in NVS, used from its next boot), so this box writes it down now.
            rs.write_token(path, new)
            self.robot_token = new
            self.robot_headers()
            log.info("robot token rotated; the old one is kept as %s.previous", path.name)
            if not rebooting:
                log.warning("the robot saved a new token but didn't reboot onto it: reboot it by hand")
        log.info("robot set up over the network: %s%s", ", ".join(changed) or "nothing changed",
                 ", rebooting" if rebooting else "")
        return web.json_response({"ok": True, "changed": changed, "rebooting": rebooting,
                                  "token_rotated": new is not None,
                                  "also": ("anything else holding the old robot token (a Workbench, a Psycheion) "
                                           "needs it again" if new is not None else None)})

    # -- firmware for the robot (firmware.py) ----------------------------------------------------

    def _fw_state(self) -> Path:
        return Path(getattr(self.args, "state", STATE))

    async def _robot_firmware(self) -> str | None:
        try:
            async with self.http.get(f"{self.args.robot_url.rstrip('/')}/api/status", headers=dict(self.robot_headers()),
                                     timeout=ClientTimeout(total=4)) as r:
                return (await r.json(content_type=None)).get("firmware") if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def firmware_get(self, request):
        """GET /firmware: what the robot runs, what's staged here, what's armed."""
        from . import firmware as fw
        self.authorized(request)
        return web.json_response({
            "robot": await self._robot_firmware(),
            "staged": [vars(s) for s in fw.staged(self._fw_state())],
            "armed": self.firmware_armed,
            "repo": getattr(self.args, "firmware_repo", ""),
        })

    # GitHub's release CDN answers 504 (and crawls) for a while after a release is published:
    # 8 Nov 2026, fw-v0.5.0's 174-byte SHA256SUMS was a 504 twice, then 0.1 s three times.
    GITHUB_RETRY_S = (3.0, 8.0)          # waits between tries: three tries in all
    GITHUB_TRANSIENT = {429, 500, 502, 503, 504}

    async def _from_github(self, url: str, headers: dict, timeout_s: float, what: str) -> bytes:
        """GET it, trying again on a status or a failure that passes; any other answer is final."""
        from . import firmware as fw
        last = ""
        for attempt, wait in enumerate((0.0, *self.GITHUB_RETRY_S)):
            if wait:
                log.info("%s: %s, trying again in %.0fs", what, last, wait)
                await asyncio.sleep(wait)
            try:
                async with self.http.get(url, headers=headers, timeout=ClientTimeout(total=timeout_s)) as r:
                    if r.status == 200:
                        return await r.read()
                    if r.status not in self.GITHUB_TRANSIENT:
                        raise fw.FirmwareError(f"{what} answered {r.status}")
                    last = f"answered {r.status}"
            except fw.FirmwareError:
                raise
            except (asyncio.TimeoutError, ClientError) as e:
                last = f"{type(e).__name__} {e}".strip()
        raise fw.FirmwareError(f"{what} {last}, {1 + len(self.GITHUB_RETRY_S)} tries (GitHub can be slow for a few "
                               "minutes after a release; try again shortly)")

    async def _asset(self, sources: list, what: str) -> bytes:
        """The first of an asset's sources that gives it up, each tried with its retries."""
        from . import firmware as fw
        errors = []
        for url, headers in sources:
            try:
                return await self._from_github(url, headers, 60, what)
            except fw.FirmwareError as e:
                errors.append(str(e))
        raise fw.FirmwareError("; ".join(errors))

    async def firmware_fetch(self, request):
        """POST /firmware/fetch {"version": "latest" | "0.1.1"}: the release's update zip, checked
        against its SHA256SUMS, its app image staged here."""
        from . import firmware as fw
        self.authorized(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        want = str((body or {}).get("version") or "latest")
        repo = getattr(self.args, "firmware_repo", "")
        gh = {"Accept": "application/vnd.github+json", "User-Agent": "seren-soma"}
        try:
            listing = await self._from_github(f"https://api.github.com/repos/{repo}/releases?per_page=30", gh, 20,
                                              f"{repo}'s releases")
            release = fw.pick_release(json.loads(listing), want)
            fetched = {}
            for name in (fw.UPDATE_ZIP.format(v=release["tag_name"][4:]), "SHA256SUMS"):
                fetched[name] = await self._asset(fw.asset_sources(release, name), f"downloading {name}")
            zip_name = fw.UPDATE_ZIP.format(v=release["tag_name"][4:])
            app = fw.verified_app(release, fetched[zip_name], fetched["SHA256SUMS"].decode("utf-8", "replace"))
            entry = fw.stage_bytes(self._fw_state(), release["tag_name"], app, f"release {release['tag_name']}")
        except fw.FirmwareError as e:
            raise web.HTTPBadGateway(text=str(e))
        except web.HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise web.HTTPBadGateway(text=f"couldn't fetch the release: {e}")
        log.info("staged firmware %s from %s (%d bytes, checked)", entry.version, entry.source, entry.size)
        return web.json_response(vars(entry))

    async def firmware_upload(self, request):
        """PUT /firmware/upload?version=0.2.0-mine: an app image of your own (stack-chan.bin from a build)."""
        from . import firmware as fw
        self.authorized(request)
        version = request.query.get("version", "")
        data = await request.read()
        try:
            entry = fw.stage_bytes(self._fw_state(), version, data, "uploaded")
        except fw.FirmwareError as e:
            raise web.HTTPBadRequest(text=str(e))
        log.info("staged firmware %s, uploaded (%d bytes)", entry.version, entry.size)
        return web.json_response(vars(entry))

    async def firmware_install(self, request):
        """POST /firmware/install {"version", "reboot": true}: arm a staged image for the next check-in,
        and reboot the robot so that's now. The robot shows its progress and reboots into it; if it
        can't check in from the new one, the bootloader goes back to this one."""
        from . import firmware as fw
        self.authorized(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="expected {\"version\": ...}")
        try:
            entry = fw.find(self._fw_state(), str(body.get("version") or ""))
        except fw.FirmwareError as e:
            raise web.HTTPBadRequest(text=str(e))
        if entry is None:
            raise web.HTTPNotFound(text="that version isn't staged here; fetch or upload it first")
        self.firmware_armed = {"version": entry.version, "file": entry.file}
        rebooting = False
        if body.get("reboot", True):
            try:
                async with self.http.post(f"{self.args.robot_url.rstrip('/')}/api/reboot", headers=dict(self.robot_headers()),
                                          json={}, timeout=ClientTimeout(total=6)) as r:
                    rebooting = r.status == 200
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't reboot the robot for the firmware: %s", e)
        log.info("firmware %s armed%s", entry.version, ", robot rebooting to take it" if rebooting else "")
        return web.json_response({"ok": True, "armed": self.firmware_armed, "rebooting": rebooting})

    async def firmware_disarm(self, request):
        self.authorized(request)
        self.firmware_armed = None
        return web.json_response({"ok": True, "armed": None})

    async def firmware_image(self, request):
        """GET /firmware/image/<file>: the image itself, to the robot only (like the check-in)."""
        from . import firmware as fw
        if self.args.device_from and request.remote not in self.args.device_from:
            log.warning("firmware download from %s refused: only %s get it", request.remote,
                        ", ".join(sorted(self.args.device_from)))
            raise web.HTTPForbidden(text="not this body's robot")
        name = request.match_info["file"]
        entry = next((s for s in fw.staged(self._fw_state()) if s.file == name), None)
        if entry is None:
            raise web.HTTPNotFound(text="no such image")
        log.info("robot downloading firmware %s", entry.version)
        return web.FileResponse(fw.firmware_dir(self._fw_state()) / entry.file,
                                headers={"Content-Type": "application/octet-stream"})

    async def robot_card(self, request):
        """POST /robot/soma.yaml: the card file for a robot that isn't on the network yet, blanks
        filled: this box as its voice server and the token this box holds for it (made and kept
        here if there's none). Same body as /robot/config, without rotate_token or reboot."""
        rs, body, keys = await self._setup_body(request)
        token = self.robot_token
        if not token:
            path = self.robot_token_path()
            if path is None:
                raise self._token_nowhere()
            token = rs.new_token()
            rs.write_token(path, token)
            self.robot_token = token
            self.robot_headers()
            log.info("made a control token for the robot, kept in %s", path.name)
        try:
            text = rs.card_text(keys, token)
        except rs.SetupError as e:
            raise web.HTTPBadRequest(text=str(e))
        log.info("made a soma.yaml for the card (%s)", ", ".join(sorted(keys) + ["token"]))
        return web.Response(text=text, content_type="text/yaml",
                            headers={"Content-Disposition": 'attachment; filename="soma.yaml"',
                                     "Cache-Control": "no-store"})

    async def ota(self, request):
        """The check-in. No mqtt, no activation, no firmware: the websocket is all it gets, and only
        the robot gets it. Anyone else asking is told no and logged."""
        if self.args.device_from and request.remote not in self.args.device_from:
            log.warning("check-in from %s refused: only %s get the voice channel", request.remote,
                        ", ".join(sorted(self.args.device_from)))
            raise web.HTTPForbidden(text="not this body's robot")
        host = self.args.advertise
        answer = {
            "websocket": {"url": f"ws://{host}:{self.args.port}/voice", "token": self.device_token, "version": 1},
            "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": -time.timezone // 60},
        }
        offer, self.firmware_armed = self.firmware_armed, None    # offered once, then disarmed
        if offer is not None:
            answer["firmware"] = {"version": offer["version"], "force": 1,
                                  "url": f"http://{host}:{self.args.port}/firmware/image/{offer['file']}"}
            log.info("offered firmware %s on the check-in", offer["version"])
        return web.json_response(answer)

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
        asyncio.create_task(self.warm())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if d.speaking or d.deaf:
                        continue
                    done = d.feed(msg.data, self.end_silence_ms)
                    if done is not None:
                        asyncio.create_task(self.hear(done, time.monotonic(), d.ended_quiet_ms))
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
        seconds = await self.say(text, body.get("voice") or self.args.voice, body.get("emotion"), bool(body.get("wake")),
                                 whole=bool(body.get("whole")))
        return web.json_response({"ok": True, "seconds": seconds})

    async def heard_route(self, request):
        """GET /heard?after=<t>&wait=<seconds>: what was heard after time t (an entry's own "t"), for
        whoever answers. With wait, an empty answer is held until something is heard or the wait runs
        out (30 s at most), so the words arrive the moment they're transcribed and nobody has to keep
        looking. after=0 answers at once with what's been heard since this server started, to begin from."""
        self.authorized(request)
        try:
            after = float(request.query.get("after", "0") or 0)
            wait = max(0.0, min(30.0, float(request.query.get("wait", "0") or 0)))
        except ValueError:
            raise web.HTTPBadRequest(text="after is a time and wait is seconds, both numbers")

        def new():
            return [e for e in self.heard_recent if e["t"] > after]

        entries = new()
        if not entries and wait > 0 and after > 0:
            try:
                await asyncio.wait_for(self.heard_event.wait(), wait)
            except asyncio.TimeoutError:
                pass
            entries = new()
        return web.json_response({"entries": entries, "now": time.time(),
                                  "last": self.heard_recent[-1]["t"] if self.heard_recent else after})

    async def voice_timing(self, request):
        """GET /api/voice-timing: how long each leg of the recent turns took (timing.py). No words."""
        self.authorized(request)
        return web.json_response({"turns": self.clock.recent(), "end_silence_ms": self.end_silence_ms,
                                  "end_silence_range": list(END_SILENCE_RANGE)})

    def clip(self, name: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""):
            raise web.HTTPBadRequest(text="clip names are letters, digits, _ and -")
        p = CLIPS / f"{name}.wav"
        if not p.exists():
            raise web.HTTPNotFound(text=f"no clip {name}")
        return p.read_bytes()

    async def clip_route(self, request):
        """POST /clip {"name": "hello", "emotion": "happy"}: play a saved sound, waking the robot for it."""
        self.authorized(request)
        body = await request.json()
        seconds = await self.say("", self.args.voice, body.get("emotion"), True, self.clip(body.get("name", "")))
        return web.json_response({"ok": True, "seconds": seconds})

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
        """GET /touch?after=<seq>: touches since then, each head one labelled "scratching", and the hand
        that's down right now (what a build-up needs to keep counting through one long scratch)."""
        self.authorized(request)
        after = int(request.query.get("after", "0") or 0)
        return web.json_response({"events": [e for e in self.feed if e["seq"] > after], "last": self.feed_seq,
                                  "hand": self.hand})

    async def root(self, request):
        """The family's info route: who this is, where its page is, and the updates block, which every
        service carries with the same seven keys whatever state its checker is in (CI pins them)."""
        from . import __version__
        try:
            from seren_meninges.updates import updates_payload
            updates = await updates_payload(self.updates, distribution="seren-soma", installed=__version__)
        except Exception as exc:  # noqa: BLE001 - an info route never fails over a cosmetic check
            updates = {"status": "unavailable", "distribution": "seren-soma", "installed": __version__, "latest": None,
                       "update_available": False, "checked_at": None,
                       "detail": f"update checking could not be constructed ({exc}): pip install -U seren-soma"}
        return web.json_response({"service": "seren-soma", "version": __version__, "viewer": "/viewer",
                                  "updates": updates})

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
            "night_now": self.night_now, "hand": self.hand, "state": str(STATE),
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
        kept_at = 0.0       # when the scritch face was last renewed under a hand that hasn't lifted
        head = None         # what the face says its head touches show, None if it doesn't
        uptime = None       # the robot's clock, and when it was read
        read_at = 0.0

        def write(entry):
            with TOUCH_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), **entry}) + "\n")

        while True:
            await asyncio.sleep(1.0)
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
            recent = label_scratching(events, recent)
            held = reader.open_touch()
            self.hand = {"down": True, "from_ms": held[0], "to_ms": held[1]} if held else {"down": False}
            # only the last one: a burst shouldn't queue up a parade of faces
            react = None
            if events:
                last = events[-1]
                scritch = last["kind"] in HEAD_KINDS and (last["kind"] == "scritch" or scritching(recent))
                react = reaction_for({"kind": "scritch"} if scritch else last, head)
            elif held and scritching(recent) and time.monotonic() - kept_at > 2:
                # mid-scratch, the hand still down: keep the scritch face up rather than let it time out
                react = reaction_for({"kind": "scritch"}, head)
            if react and react == reaction_for({"kind": "scritch"}, head):
                kept_at = time.monotonic()
            for e in events:
                self.feed_seq += 1
                self.feed.append({"seq": self.feed_seq, "t": time.time(), **e,
                                  "showed": (react[0] if react else None) if e is events[-1] else None})
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
            if time.monotonic() - d.last_words >= self.dead_air:
                log.info("no words for %ss: closing the channel", self.dead_air)
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
    app.router.add_post("/robot/config", link.robot_config)
    app.router.add_post("/robot/soma.yaml", link.robot_card)
    app.router.add_get("/firmware", link.firmware_get)
    app.router.add_post("/firmware/fetch", link.firmware_fetch)
    app.router.add_put("/firmware/upload", link.firmware_upload)
    app.router.add_post("/firmware/install", link.firmware_install)
    app.router.add_post("/firmware/disarm", link.firmware_disarm)
    app.router.add_get("/firmware/image/{file}", link.firmware_image)
    app.router.add_get("/health", link.health)
    app.router.add_get("/", link.root)
    app.router.add_get("/api/about", link.about)
    app.router.add_get("/api/logs", link.logs)
    app.router.add_get("/heard", link.heard_route)
    app.router.add_get("/api/voice-timing", link.voice_timing)
    Bench(link, STATE).add_routes(app)
    if viewer_html:
        async def viewer(request):
            return web.Response(text=viewer_html, content_type="text/html",
                                headers={"Cache-Control": "no-store, must-revalidate"})

        app.router.add_get("/viewer", viewer)
    return app
