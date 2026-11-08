#!/usr/bin/env python3
"""Walk the device through every emotion, to check a face on the real screen.

    python emotion_cycle.py --host <robot address> [--seconds 4] [--face path/to/face.yaml]

With --face the names come from that face.yaml (PyYAML needed), neutral first;
without it, the stock list. Each name is shown in the speech bubble too, so what
is on screen can be matched to what was asked for. Ctrl+C stops it. Uses only
the control API: no flash, no firmware change.
"""
import argparse
import json
import time
import urllib.error
import urllib.request

STOCK = ["neutral", "happy", "laughing", "funny", "sad", "angry", "crying", "loving", "embarrassed", "surprised",
         "shocked", "thinking", "winking", "cool", "relaxed", "delicious", "kissy", "confident", "silly", "confused",
         "sleepy"]


def post(base, path, body, attempts=3):
    """Idle Wi-Fi power save can hold a request for seconds, so try again before giving up."""
    data = json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=8) as reply:
                return json.loads(reply.read())
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == attempts - 1:
                return {"ok": False, "error": str(e)}


def names_from(path):
    import yaml
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    emotions = doc.get("emotions") or doc.get("face", {}).get("emotions") or {}
    return ["neutral"] + [n for n in emotions if n != "neutral"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="the robot's address (its screen shows it)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--face", help="face.yaml to take the emotion names from")
    ap.add_argument("--once", action="store_true", help="one pass, then back to neutral")
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"
    names = names_from(args.face) if args.face else STOCK
    print(f"{len(names)} emotions, {args.seconds:g} s each, on {base}")
    try:
        while True:
            for name in names:
                reply = post(base, "/api/emotion", {"name": name})
                post(base, "/api/say", {"text": name})
                print(f"{name}: {'ok' if reply and reply.get('ok') else reply}")
                time.sleep(args.seconds)
            if args.once:
                break
    except KeyboardInterrupt:
        pass
    post(base, "/api/emotion", {"name": "neutral"})


if __name__ == "__main__":
    main()
