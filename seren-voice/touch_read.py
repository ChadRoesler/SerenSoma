"""What a hand on the robot's head is doing, from the sensor's raw readings.

A reading is (ms, zone0, zone1, zone2). Zone 2 is the front of the head, toward the face, and zone 0
the back. Thresholds come from Chad's hand on 5 Nov 2026, two runs with labeled sets:
- Strength isn't pressure. A barely-there brush reaches full strength, so nothing here reads it.
- His real pet is a stroke front to back, about 450 ms, one every 1.7 s, on and on.
- Scritches flicker between zones without going anywhere.
"""
from dataclasses import dataclass, field

CONTACT_GAP_MS = 120     # readings further apart than this are separate touches
SAMPLE_MS = 50
STROKE_TRAVEL = 60       # how far a touch has to move, on -100 (back) .. +100 (front)
HOLD_MS = 1000
STROKE_MAX_MS = 1500     # his strokes run 450-850 ms; one unbroken touch longer than this that moves is scratching
HAND_ZONES = 2.5         # average zones covered: a whole hand, not a finger
SCRITCH_MS = 300
SCRITCH_CHANGES = 3
SCRITCH_BLIP_MS = 120    # shorter than any poke he meant: fingernails barely register
SESSION_GAP_MS = 4000    # hands off this long ends a session
PETTING_STROKES = 3
SPOT_FIRST = 100         # touch_log.h: a face's touch spots, by their place in its list
# Scritching, from Chad's hand on 7 Nov 2026: nails going back and forth come through as a burst of
# quick bits, 230-310 ms "strokes", 170 ms pokes, 100 ms blips, that each look like something else.
# Three quick ones inside two seconds, with a stroke or a scritch among them, is scritching. His slowest
# pet is one stroke every ~1.7 s, so petting can't do that, and a few pokes in a row aren't scritches.
SCRITCH_BURST_MS = 2000
SCRITCH_BURST_TOUCHES = 3
SCRITCH_BURST_MAX_MS = 450
HEAD_KINDS = ("stroke", "poke", "scritch", "hand", "finger_rest")
SCREEN_TAPS = {10: "nose_boop", 11: "forehead_kiss"}   # touch_log.h: taps on the screen share the feed


def _position(r):
    total = r[1] + r[2] + r[3]
    return (r[3] - r[1]) * 100 / total if total else 0


def _touching(r):
    return bool(r[1] or r[2] or r[3])


def classify(contact):
    """One unbroken touch, as a dict: kind, ms, and where or which way."""
    ms = contact[-1][0] - contact[0][0] + SAMPLE_MS
    travel = _position(contact[-1]) - _position(contact[0])
    zones_seen = sum(1 for z in (1, 2, 3) if any(r[z] for r in contact))
    avg_zones = sum(sum(1 for z in (1, 2, 3) if r[z]) for r in contact) / len(contact)
    patterns = [(r[1], r[2], r[3]) for r in contact]
    changes = sum(1 for a, b in zip(patterns, patterns[1:]) if a != b)
    event = {"t_ms": contact[0][0], "ms": ms}

    if ms > STROKE_MAX_MS:
        # longer than any stroke: resting if it hardly moves, scratching if it churns
        # (7 Nov: a 4.6 s scratch had travelled far enough to pass for a stroke)
        if changes < 6:
            return {**event, "kind": "hand" if avg_zones >= HAND_ZONES else "finger_rest"}
        return {**event, "kind": "scritch"}
    if abs(travel) >= STROKE_TRAVEL and zones_seen >= 2:
        return {**event, "kind": "stroke", "way": "front_to_back" if travel < 0 else "back_to_front"}
    if ms >= HOLD_MS and changes < 6:
        return {**event, "kind": "hand" if avg_zones >= HAND_ZONES else "finger_rest"}
    if ms <= SCRITCH_BLIP_MS or (ms >= SCRITCH_MS and changes >= SCRITCH_CHANGES):
        return {**event, "kind": "scritch"}
    totals = [sum(r[z] for r in contact) for z in (1, 2, 3)]
    return {**event, "kind": "poke", "where": ("back", "middle", "front")[totals.index(max(totals))]}


# My faces for his hands (Wren's picks, 6 Nov 2026; he said to fool around with them). Strokes aren't
# here: the firmware already answers those with the happy face, heart and blush.
REACTIONS = {
    "scritch": ("laughing", 1800),
    "hand": ("relaxed", 3000),
    "finger_rest": ("thinking", 2000),
    "poke_front": ("surprised", 1200),
    "poke_middle": ("confused", 1500),
    "poke_back": ("annoyed", 1500),
}


# Overwhelm (Wren's design, Chad's go, 7 Nov 2026): scritches that don't stop for this long tip melty
# over into the ahegao face. Earned, never by accident: a gap longer than OVERWHELM_GAP_S starts the
# count again, and once it fires it rests for OVERWHELM_REST_S.
OVERWHELM_MS = 15000     # 20 s was a lot of arm: he got to 19 on 7 Nov
OVERWHELM_GAP_MS = 3000
OVERWHELM_REST_MS = 120000
WORK_HOURS = (8, 17)        # weekdays, local time: never then. Chad's to change.


def scritchy(event):
    """Part of scratching: a scritch, or one of the quick bits scratching breaks into."""
    return event["kind"] == "scritch" or (event["kind"] in ("stroke", "poke") and event["ms"] <= SCRITCH_BURST_MAX_MS)


@dataclass
class Overwhelm:
    """On the sensor's clock, so one long unbroken scratch counts all the way through."""
    since: float = None
    last: float = -1e12
    fired: float = -1e12
    after_ms: int = OVERWHELM_MS
    gap_ms: int = OVERWHELM_GAP_MS
    rest_ms: int = OVERWHELM_REST_MS

    def touch(self, start_ms, end_ms, scratching, still_going=False):
        """A touch from start_ms to end_ms. scratching: it's part of scratching. still_going: the hand
        hasn't lifted yet, so it can carry a run on but not start one. True when it's time to tip over."""
        if self.since is not None and (start_ms - self.last > self.gap_ms or not scratching):
            self.since = None              # a pause, or a real stroke: petting doesn't count
        if self.since is None:
            if not scratching or still_going:
                return False
            self.since = start_ms
        self.last = max(self.last, end_ms)
        if self.last - self.since >= self.after_ms and self.last - self.fired >= self.rest_ms:
            self.fired = self.last
            self.since = None
            return True
        return False

    @property
    def running(self):
        return self.since is not None

    def progress(self):
        """How far along a run is, 0..1: what the console's meter shows."""
        return min(1.0, (self.last - self.since) / self.after_ms) if self.running else 0.0


def work_hours(local, hours=WORK_HOURS, days=(0, 1, 2, 3, 4)):
    """local: a time.struct_time. True on one of days (Monday is 0) inside hours."""
    return local.tm_wday in days and hours[0] <= local.tm_hour < hours[1]


def in_window(local, start, end):
    """local: a time.struct_time. start, end: "HH:MM". A window that ends before it starts crosses midnight."""
    def minutes(hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        return h * 60 + m
    now, s, e = local.tm_hour * 60 + local.tm_min, minutes(start), minutes(end)
    return s <= now < e if s <= e else (now >= s or now < e)


def scritching(recent):
    """recent: the last few head touches, oldest first. True if they're a burst of scritching."""
    last = [e for e in recent if e["kind"] in HEAD_KINDS][-SCRITCH_BURST_TOUCHES:]
    if len(last) < SCRITCH_BURST_TOUCHES:
        return False
    quick = all(e["ms"] <= SCRITCH_BURST_MAX_MS or e["kind"] == "scritch" for e in last)
    moving = any(e["kind"] in ("stroke", "scritch") for e in last)
    return quick and moving and last[-1]["t_ms"] - last[0]["t_ms"] <= SCRITCH_BURST_MS


def reaction_for(event, head=None):
    """The pose and how long, for a touch that just ended; None if the face should leave it alone.
    head: what the face itself says its head touches show. When it says anything, it alone decides."""
    kind = event["kind"]
    if kind == "poke":
        kind = f"poke_{event['where']}"
    elif kind == "stroke":
        kind = f"stroke_{event['way']}"
    if head is not None:
        spec = head.get(kind)
        return (spec["pose"], spec.get("ms", 2000)) if spec else None
    return REACTIONS.get(kind)


def summarize(events):
    """A run of touches with no long gap: what was going on, for the log and for me."""
    if not events:
        return None
    strokes = [e for e in events if e["kind"] == "stroke"]
    start = events[0]["t_ms"]
    out = {"seconds": round((events[-1]["t_ms"] + events[-1]["ms"] - start) / 1000, 1), "touches": len(events),
           "counts": {k: sum(1 for e in events if e["kind"] == k) for k in sorted({e["kind"] for e in events})}}
    if strokes:
        out["front_to_back"] = sum(1 for e in strokes if e["way"] == "front_to_back")
        out["back_to_front"] = len(strokes) - out["front_to_back"]
        out["avg_stroke_ms"] = round(sum(e["ms"] for e in strokes) / len(strokes))
    if len(strokes) >= 2:
        out["stroke_every_ms"] = round((strokes[-1]["t_ms"] - strokes[0]["t_ms"]) / (len(strokes) - 1))
    # what the whole of it amounts to
    if len(strokes) >= PETTING_STROKES:
        out["kind"] = "petting"
    elif out["counts"].get("scritch", 0) >= 2:
        out["kind"] = "scritching"
    elif len(events) == 1:
        out["kind"] = events[0]["kind"]
    else:
        out["kind"] = "fiddling"
    return out


@dataclass
class TouchReader:
    """Feed it readings as they arrive; it hands back finished touches and finished sessions."""
    _contact: list = field(default_factory=list)
    _session: list = field(default_factory=list)

    def feed(self, rows, now_ms=None):
        """rows: (ms, z0, z1, z2, ...). now_ms: the sensor's clock, to end what has gone quiet.
        Returns (touch events, session summaries)."""
        events, sessions = [], []

        def end_contact():
            if self._contact:
                events.append(self._add(classify(self._contact), sessions))
                self._contact = []

        for r in rows:
            if len(r) > 4 and r[4] in SCREEN_TAPS:
                events.append(self._add({"t_ms": r[0], "ms": 0, "kind": SCREEN_TAPS[r[4]]}, sessions))
                continue
            if len(r) > 4 and r[4] >= SPOT_FIRST:
                events.append(self._add({"t_ms": r[0], "ms": 0, "kind": "touch_spot", "index": r[4] - SPOT_FIRST}, sessions))
                continue
            if _touching(r):
                if self._contact and r[0] - self._contact[-1][0] > CONTACT_GAP_MS:
                    end_contact()
                self._contact.append(r)
            else:
                end_contact()
        if now_ms is not None:
            if self._contact and now_ms - self._contact[-1][0] > CONTACT_GAP_MS * 3:
                end_contact()
            if self._session and now_ms - self._last_end() > SESSION_GAP_MS and not self._contact:
                sessions.append(summarize(self._session))
                self._session = []
        return events, sessions

    def open_touch(self):
        """(start_ms, last_ms) of a touch the hand hasn't lifted from yet, or None."""
        return (self._contact[0][0], self._contact[-1][0]) if self._contact else None

    def reset(self):
        """The robot rebooted: its clock starts over."""
        self._contact = []
        self._session = []

    def _last_end(self):
        return self._session[-1]["t_ms"] + self._session[-1]["ms"]

    def _add(self, event, sessions):
        if self._session and event["t_ms"] - self._last_end() > SESSION_GAP_MS:
            sessions.append(summarize(self._session))
            self._session = []
        self._session.append(event)
        return event
