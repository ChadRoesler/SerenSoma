"""SerenSoma's identity card. STDLIB ONLY - nothing imported, nothing read: the moment you most want
a thing to say its own name is when its install is broken."""
from __future__ import annotations

NAME = "seren-soma"
DISPLAY = "Seren Soma"
DESCRIPTION = ("The body's nervous system: a Stack-chan robot's voice, ears and touch, its faces' "
               "reactions, night mode and work hours.")

# 7480: outside the family's 74xx band on purpose, where the robot's firmware has always found it.
PORT = 7480

GROUP = "auxiliary"

# Sage: the sweater the face wears.
ACCENT = "#8fbf88"

# Lodestar does the hearing and speaking; without it the body still reacts to touch and keeps night.
REQUIRES: tuple = ()
EXTRAS: tuple = ()

DESCRIBE = {
    "schema_version": 1,
    "name": NAME,
    "display": DISPLAY,
    "description": DESCRIPTION,
    "port": PORT,
    "group": GROUP,
    "accent": ACCENT,
    "package": "seren-soma",
    "requires": list(REQUIRES),
    "extras": list(EXTRAS),
}
