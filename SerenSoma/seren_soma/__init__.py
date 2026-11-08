"""seren_soma - the body's nervous system.

A Stack-chan robot running SerenSoma's firmware checks in here and speaks over a websocket: what it
hears goes to Lodestar to be transcribed, what the mind says comes back through Lodestar in its voice.
This server also reads the robot's head sensor into strokes, pokes, scritches and resting hands, picks
the face each one brings up, and owns night mode and work hours, so the body behaves itself with
nothing else running.
"""
from __future__ import annotations

try:  # the setuptools-scm build artifact (gitignored); present in installed wheels
    from ._version import version as __version__
except Exception:  # source checkout without a build -> metadata, then dev fallback
    try:
        from importlib.metadata import version as _v

        __version__ = _v("seren-soma")
    except Exception:
        __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
