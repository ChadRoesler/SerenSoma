# seren-soma

The nervous system of a [SerenSoma](https://github.com/ChadRoesler/SerenSoma) body: a Stack-chan
robot's voice, ears and touch.

```
pip install seren-soma
seren-soma                  # http://<this box>:7480/viewer
seren-soma --describe       # one line of JSON, for installers
```

- The robot checks in at `/ota/` and talks over a websocket. What it hears goes to Lodestar to be
  transcribed and lands in `heard.jsonl`; what the mind says comes back through Lodestar, in its voice.
- It reads the head sensor into strokes, pokes, scritches and resting hands, and shows the face the
  robot's face file names for each.
- It owns night mode (caps on the robot's lights, speaker and screen) and work hours, so the body
  behaves with nothing else running.

**Talking back and forth.** Whoever answers (a mind, a game, a script) does two things:

- `GET /heard?after=<t>&wait=25` returns what was heard after time `t`, and with `wait` holds an empty
  answer open until something is heard. So the words arrive the moment they are transcribed, with no
  file to keep reading. Start with `after=0` to get the last entry's `t`.
- `POST /say {"text": "...", "emotion": "happy", "wake": true}` says a line. It goes out a sentence
  at a time: the first sentence is rendered and starts playing while the rest render behind it, so
  the wait is for one sentence. `"whole": true` renders the line in one piece instead.

`GET /api/voice-timing` (and the page's **Voice** tab) times every leg of each turn: waiting for quiet,
transcribing, the answer being thought of, rendering the first sentence, opening the channel. It keeps
how many words, never which. The quiet that ends a turn is a live setting there
(`channel.end_silence_ms`, 900 by default). When the robot opens its channel after the speech service
has sat idle, a throwaway line is rendered so the voice is loaded before anyone needs it.

Copy [`seren-soma.yaml.sample`](seren-soma.yaml.sample) to `~/seren-soma/seren-soma.yaml`.
Everything it keeps is in the state folder, `~/seren-soma/state/` by default: its two tokens, the
robot's control token, what was heard, the touch log, the settings and its own log.

**Its page** (`/viewer`) shows whether it's alive and its log. "Copy for help" replaces everything
that was heard with a word count, so the log can go to someone helping without what was said.
Its **Touch** and **Servos** tabs are the bench: every channel of the touch chip live, a chart of
which pad answers where (kept as `touch_map.json` in the state folder), and the neck servos' own
readings with a sweep test. They need firmware with the bench routes; see
[`firmware/docs/bench.md`](../firmware/docs/bench.md).

It binds the LAN, because the robot dials in, so it won't start without its API token; with none
configured it makes one in the state folder. The robot gets a separate token for its voice
connection, and the check-in answers only the robot's own address.

AGPL-3.0-or-later.
