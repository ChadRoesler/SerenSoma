"""Traces taken from Chad's hand on 5 Nov 2026."""
from seren_soma import touch_read as tr


def rows(patterns, start=1000, step=50):
    """'003 033 330 300' as readings, 50 ms apart, followed by the reading where the touch ends."""
    out = [(start + i * step, int(p[0]), int(p[1]), int(p[2])) for i, p in enumerate(patterns.split())]
    return out + [(out[-1][0] + step, 0, 0, 0)]


def one(patterns):
    events, _ = tr.TouchReader().feed(rows(patterns))
    assert len(events) == 1, events
    return events[0]


def test_touches():
    e = one("003 033 330 300")
    assert (e["kind"], e["way"]) == ("stroke", "front_to_back")
    e = one("300 320 330 333 332 132 033 013 003 002")
    assert (e["kind"], e["way"]) == ("stroke", "back_to_front")
    e = one("003 003 003 003 003 003")
    assert (e["kind"], e["where"]) == ("poke", "front")
    e = one("300 300 300 300")
    assert (e["kind"], e["where"]) == ("poke", "back")
    e = one("030 030 030 030")
    assert (e["kind"], e["where"]) == ("poke", "middle")
    assert one(" ".join(["033"] * 50))["kind"] == "finger_rest"
    assert one(" ".join(["333"] * 150))["kind"] == "hand"
    assert one("020 021 001 021 120 020 120 020")["kind"] == "scritch"
    assert one("030 030")["kind"] == "scritch"   # a blip too short for a finger to mean it


def session(*contacts, gap=1400):
    all_rows, at = [], 1000
    for c in contacts:
        r = rows(c, start=at)
        all_rows += r
        at = r[-1][0] + gap
    reader = tr.TouchReader()
    _, done = reader.feed(all_rows, now_ms=at + tr.SESSION_GAP_MS + 1000)
    assert len(done) == 1, done
    return done[0]


def test_sessions():
    s = session(*["003 033 330 300"] * 5)
    assert s["kind"] == "petting" and s["front_to_back"] == 5 and s["back_to_front"] == 0
    assert session("030 030", "002 002", "030 030")["kind"] == "scritching"
    assert session("003 003 003 003", "003 003 003 003", "003 003 003 003")["kind"] == "fiddling"
    assert session(" ".join(["333"] * 150))["kind"] == "hand"


def test_gap_splits_sessions():
    reader = tr.TouchReader()
    first = rows("003 033 330 300", start=1000)
    second = rows("003 033 330 300", start=20000)
    _, done = reader.feed(first + second)
    assert len(done) == 1 and done[0]["touches"] == 1


def test_a_touch_across_two_polls_is_one_touch():
    reader = tr.TouchReader()
    r = rows("003 033 330 300")
    a, _ = reader.feed(r[:2])
    b, _ = reader.feed(r[2:])
    assert a == [] and len(b) == 1 and b[0]["kind"] == "stroke"


def test_quiet_ends_a_touch_whose_end_was_never_seen():
    reader = tr.TouchReader()
    e, _ = reader.feed(rows("003 003 003 003")[:-1], now_ms=2000)
    assert len(e) == 1 and e[0]["kind"] == "poke"


def test_screen_taps():
    reader = tr.TouchReader()
    events, _ = reader.feed([(1000, 0, 0, 0, 10), (1500, 0, 0, 0, 11)])
    assert [e["kind"] for e in events] == ["nose_boop", "forehead_kiss"]


def test_a_boop_mid_stroke_doesnt_split_it():
    reader = tr.TouchReader()
    r = [x + (0,) for x in rows("003 033 330 300")]
    events, _ = reader.feed(r[:2] + [(r[1][0] + 1, 0, 0, 0, 10)] + r[2:])
    assert [e["kind"] for e in events] == ["nose_boop", "stroke"]


def test_the_face_decides_when_it_says():
    head = {"stroke_front_to_back": {"pose": "loving", "ms": 3000}, "poke_back": {"pose": "angry"}}
    assert tr.reaction_for({"kind": "stroke", "way": "front_to_back"}, head) == ("loving", 3000)
    assert tr.reaction_for({"kind": "stroke", "way": "back_to_front"}, head) is None
    assert tr.reaction_for({"kind": "poke", "where": "back"}, head) == ("angry", 2000)
    assert tr.reaction_for({"kind": "scritch"}, head) is None      # it said things, and this wasn't one
    assert tr.reaction_for({"kind": "scritch"}, {}) is None        # it said nothing happens
    assert tr.reaction_for({"kind": "scritch"})[0] == "laughing"   # it never said


def test_touch_spots():
    events, _ = tr.TouchReader().feed([(1000, 0, 0, 0, 102)])
    assert events[0]["kind"] == "touch_spot" and events[0]["index"] == 2
    assert tr.reaction_for(events[0]) is None    # the firmware already showed the face's own pose


def test_reactions():
    assert tr.reaction_for({"kind": "scritch"})[0] == "laughing"
    assert tr.reaction_for({"kind": "poke", "where": "middle"})[0] == "confused"
    assert tr.reaction_for({"kind": "stroke", "way": "front_to_back"}) is None   # the firmware's job
    assert tr.reaction_for({"kind": "nose_boop"}) is None                        # the firmware's job


def test_reboot_forgets():
    reader = tr.TouchReader()
    reader.feed(rows("003 003")[:-1])
    reader.reset()
    e, s = reader.feed([], now_ms=99999)
    assert e == [] and s == []


def test_a_long_moving_touch_is_a_scratch():
    # 4.6 s, travelling front to back and churning: read as a stroke on 7 Nov, and it's a scratch
    e = one(" ".join(["003", "033", "330", "300", "030", "303"] * 15) + " 300")
    assert e["kind"] == "scritch", e


def test_scritching_is_a_burst():
    # Chad's scritches on 7 Nov: quick strokes, pokes and blips, all inside two seconds
    burst = [{"kind": "stroke", "way": "back_to_front", "ms": 281, "t_ms": 1000},
             {"kind": "poke", "where": "middle", "ms": 171, "t_ms": 1400},
             {"kind": "stroke", "way": "back_to_front", "ms": 230, "t_ms": 1800},
             {"kind": "scritch", "ms": 100, "t_ms": 2300}]
    assert tr.scritching(burst)
    # his real pet: 450 ms strokes, one every 1.7 s
    pets = [{"kind": "stroke", "way": "front_to_back", "ms": 450, "t_ms": 1000 + i * 1700} for i in range(4)]
    assert not tr.scritching(pets)
    # a few pokes in a row are pokes
    pokes = [{"kind": "poke", "where": "back", "ms": 300, "t_ms": 1000 + i * 500} for i in range(4)]
    assert not tr.scritching(pokes)
    # slow strokes the other way are strokes
    slow = [{"kind": "stroke", "way": "back_to_front", "ms": 800, "t_ms": 1000 + i * 900} for i in range(3)]
    assert not tr.scritching(slow)


def test_overwhelm_is_earned():
    o = tr.Overwhelm()
    # quick scratches, one a second for 20 s: it tips over once, at 15
    fired = [t for t in range(21) if o.touch(t * 1000, t * 1000 + 200, True)]
    assert fired == [15], fired
    # straight back to it: resting, nothing
    assert not any(o.touch(t * 1000, t * 1000 + 200, True) for t in range(21, 60))
    # a pause longer than the gap starts the count again
    o = tr.Overwhelm()
    assert not any(o.touch(t * 1000, t * 1000 + 200, True) for t in list(range(0, 10)) + list(range(14, 26)))
    # one long unbroken scratch, still going: carries the run to 15 s
    o = tr.Overwhelm()
    assert not o.touch(0, 2000, True)
    assert not o.touch(2500, 9000, True, still_going=True)
    assert o.touch(2500, 15500, True, still_going=True)
    # a hand still on the head can't start a run by itself
    o = tr.Overwhelm()
    assert not o.touch(0, 30000, True, still_going=True) and not o.running
    # scratch, then pet: the pet ends it
    o = tr.Overwhelm()
    assert not o.touch(0, 8000, True)
    assert not o.touch(9000, 9700, False) and not o.running
    # Chad's second run on 7 Nov, on the sensor's clock: unbroken from 400755 to 419723, 19 s
    run = [(400755, 50, "scritch"), (402120, 110, "scritch"), (404837, 410, "scritch"), (405296, 4627, "scritch"),
           (409930, 4705, "scritch"), (415655, 651, "poke"), (416655, 400, "poke"), (417056, 1969, "scritch")]
    o = tr.Overwhelm()
    fired = [s for s, ms, k in run if o.touch(s, s + ms, tr.scritchy({"kind": k, "ms": ms}) or k == "poke")]
    assert fired, "19 s of scratching gets there at 15"
    import time
    assert tr.work_hours(time.strptime("2026-11-06 10:00", "%Y-%m-%d %H:%M"))       # a Friday morning
    assert not tr.work_hours(time.strptime("2026-11-06 19:00", "%Y-%m-%d %H:%M"))   # Friday night
    assert not tr.work_hours(time.strptime("2026-11-07 10:00", "%Y-%m-%d %H:%M"))   # Saturday


def test_night_window_crosses_midnight():
    import time
    at = lambda hhmm: time.strptime(f"2026-11-07 {hhmm}", "%Y-%m-%d %H:%M")
    assert tr.in_window(at("23:30"), "22:30", "07:00")
    assert tr.in_window(at("03:00"), "22:30", "07:00")
    assert not tr.in_window(at("07:00"), "22:30", "07:00")
    assert not tr.in_window(at("12:00"), "22:30", "07:00")
    assert tr.in_window(at("13:00"), "12:00", "14:00") and not tr.in_window(at("14:00"), "12:00", "14:00")


def test_overwhelm_takes_its_settings():
    o = tr.Overwhelm(after_ms=5000)
    assert [t for t in range(4) if o.touch(t * 1000, t * 1000 + 200, True)] == []
    assert 0.6 < o.progress() < 0.7          # 3.2 s of 5
    assert o.touch(5000, 5200, True)
    assert o.progress() == 0.0               # it fired: the run starts over


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
