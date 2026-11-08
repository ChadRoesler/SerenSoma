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

Copy [`seren-soma.yaml.sample`](seren-soma.yaml.sample) to `~/seren-soma/seren-soma.yaml`.
Everything it keeps is in the state folder, `~/seren-soma/state/` by default: its two tokens, the
robot's control token, what was heard, the touch log, the settings and its own log.

**Its page** (`/viewer`) shows whether it's alive and its log. "Copy for help" replaces everything
that was heard with a word count, so the log can go to someone helping without what was said.

It binds the LAN, because the robot dials in, so it won't start without its API token; with none
configured it makes one in the state folder. The robot gets a separate token for its voice
connection, and the check-in answers only the robot's own address.

AGPL-3.0-or-later.
