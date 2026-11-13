"""How long a turn of talk takes, and where the time goes.

Talk through the robot is a relay: the room goes quiet, the clip is transcribed, whoever answers
thinks, the answer is rendered into a voice, and the first of it reaches the speaker. Each leg is
somebody else's to make faster, so each is timed separately, in milliseconds on this server's own
clock. Nothing that was said is kept here: a turn holds how many words, never which.

A "heard" turn is one thing somebody said. A "said" turn is one thing the robot said; when it comes
soon after a heard one it is that one's answer, and carries the whole gap from the person's last
word to the robot's first sound, which is the number a conversation actually feels.
"""
from __future__ import annotations

import time
from collections import deque

ANSWER_WITHIN_S = 120     # a reply later than this after the words isn't an answer to them


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


class VoiceClock:
    def __init__(self, keep: int = 60):
        self.turns: deque = deque(maxlen=keep)
        self.seq = 0
        self._open = None      # the last thing heard that nothing has answered yet: (seq, words ready, talking stopped)

    def heard(self, *, speech_end: float, detected: float, done: float, audio_s: float, words: int, transcribed: bool) -> dict:
        """speech_end: when the talking stopped. detected: when enough quiet had passed to call it
        over. done: when the words came back. All time.monotonic()."""
        self.seq += 1
        turn = {"seq": self.seq, "kind": "heard", "t": time.time(), "audio_s": round(audio_s, 1), "words": words,
                "transcribed": transcribed,
                "wait_for_quiet_ms": _ms(detected - speech_end), "transcribe_ms": _ms(done - detected)}
        self.turns.append(turn)
        if words:
            self._open = (self.seq, done, speech_end)
        return turn

    def said(self, *, asked: float, rendered_first: float, first_audio: float, done: float, pieces: int,
             chars: int, audio_s: float, woke: bool = False, clip: bool = False) -> dict:
        """asked: when the words arrived to be said. rendered_first: when the first piece was ready
        to send. first_audio: when the first sound went to the robot. done: when the last did."""
        self.seq += 1
        turn = {"seq": self.seq, "kind": "said", "t": time.time(), "pieces": pieces, "chars": chars,
                "audio_s": round(audio_s, 1), "woke": woke, "clip": clip,
                "render_first_ms": _ms(rendered_first - asked), "open_channel_ms": _ms(first_audio - rendered_first),
                "to_first_sound_ms": _ms(first_audio - asked), "speaking_ms": _ms(done - first_audio)}
        if self._open is not None and asked - self._open[1] <= ANSWER_WITHIN_S:
            seq, words_ready, speech_end = self._open
            turn["answers"] = seq
            turn["mind_ms"] = _ms(asked - words_ready)
            turn["last_word_to_first_sound_ms"] = _ms(first_audio - speech_end)
        self._open = None      # the first thing said after it is its answer; a second line isn't
        self.turns.append(turn)
        return turn

    def recent(self) -> list:
        return list(self.turns)
