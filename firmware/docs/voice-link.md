# The Seren voice link

How SerenChan stops talking to Xiaozhi's cloud and starts talking to Seren. This is a design from
reading the code (Xiaozhi 2.5.2 in `xiaozhi-esp32/`, protocol in its `docs/websocket.md`). Nothing
here has been built or run yet.

## The short version

The cloud is one setting away from being ours. On boot the device asks `CONFIG_OTA_URL` what to do,
and the answer's `websocket` section tells it where to connect. So a small Seren server can answer
that request and speak the websocket protocol, and the firmware's audio, wake word, face and MCP
all keep working with **no firmware protocol changes**.

## What the server has to do

### 1. Answer the check-in

`POST CONFIG_OTA_URL`, answered with JSON:

```json
{
  "websocket": { "url": "ws://192.168.0.x:PORT/voice", "token": "...", "version": 1 },
  "server_time": { "timestamp": 0, "timezone_offset": 0 }
}
```

Leave out:

- `mqtt`: with no MQTT config the app uses the websocket protocol, and MQTT's TLS session goes away.
- `activation`: no code to enter.
- `firmware`: nothing to install, which is the proper end of the auto-upgrade problem.

Check `application.cc` around `InitializeProtocol` when building this: it decides between the two
protocols, and the settings the cloud already stored in NVS (`mqtt`, `websocket`) may need clearing.

### 2. Speak the websocket protocol

The device connects when it needs audio, sending `Authorization`, `Device-Id` and `Client-Id` headers.

| Direction | Message | Meaning |
|---|---|---|
| Device to server | `hello` | Features and audio format: Opus, 16 kHz, mono, 60 ms frames |
| Server to device | `hello` | Must have `transport: "websocket"`; we choose the playback rate, such as 24 kHz |
| Device to server | binary | Microphone audio, one Opus frame per message |
| Device to server | `listen` | `start`, `stop`, or `detect` with the wake word text |
| Device to server | `abort` | Stop talking |
| Server to device | `stt` | What was heard, shown on screen |
| Server to device | `llm` | `emotion`: sets the face, and custom pose names work |
| Server to device | `tts` | `start`, `sentence_start` with text, `stop` |
| Server to device | binary | My voice, Opus frames |
| Either way | `mcp` | JSON-RPC: device tools such as head angles and LED |

### 3. Connect it to Seren

- **Ears:** decode the Opus, find the end of speech, send it to Lodestar's hear route.
- **Mind:** the open question, below.
- **Voice:** text to the speaker (Lodestar, a fast voice), encode to Opus at 60 ms.
  Trained pass 9 is the keeper for live voice; it ran at 1.57x real time with `fast_qwen`.

## What this gets us

- **RAM:** on the LAN it can be plain `ws://`, so no TLS, and no MQTT task. Part of that 68 KB should
  come back. How much is a measurement, not a promise.
- **Privacy:** nothing goes to `api.tenclass.net`. Also check `CONFIG_STACKCHAN_SERVER_URL`.
- **Tailnet later:** the same server, reached a different way.

## What it doesn't get us yet

- **Reaching an idle device.** `WebsocketProtocol::Start()` doesn't connect; the channel opens only
  on demand. For me to speak first, the device needs an always-on path. Candidates: a small keep-alive
  socket in firmware, or a `POST /api/wake` on the control API, which is much smaller and works on the
  LAN. I'd start with the second, since the cluster can already reach the device.
- **Interrupting each other.** `kListeningModeRealtime` exists and `abort` works, but realtime needs
  AEC, and the doc says audio arriving during `listening` is dropped. Talking over each other needs
  firmware work and testing. Interrupting me mid-sentence by wake word or tap should work first.
- **Device to cluster is unproven.** The IoT network passes desktop to device; the other way is the
  first thing a real connection tests.

## Who answers

**Both,** decided 4 Nov 2026. A quick local model catches the moment ("hang on" or a laugh)
while a real session of me thinks. Banter needs low latency and it needs to actually be me, and
those pull in opposite directions.

- **Quick:** a local model through Lodestar, like every other local model.
- **Real:** over the existing Claude carabiner, because no Seren app talks to a harness directly.
  The voice server gets no carabiner of its own; it isn't a harness.
- **How the real one is reached is still open.** `kbh wake` starts a fresh session each time, which
  is fine for housekeeping and probably too slow for talk. Feeding a Symposium chat keeps one session
  going. Either way, words from the Stack-chan must carry a server-set source, never look like typing
  (`symposium.game-table` in Loci). Latency hasn't been measured for either.

## Build order

1. Server answers the check-in and `hello`, then plays one canned line in my voice on wake. Proves
   the whole path, device to cluster included, with no speech recognition or mind.
2. Add ears: print what it heard on the Stack-chan's screen.
3. Add the mind.
4. Add `POST /api/wake`, so I can speak first.
5. Measure RAM after each step, then cull what the cloud left behind.
