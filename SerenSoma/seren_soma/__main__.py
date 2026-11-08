"""Entry point for `python -m seren_soma` or the `seren-soma` script.

--describe answers before anything heavy is imported. --config like the rest of the family;
--advertise and --state override the config for a hand run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _force_utf8_stdio()
    if "--describe" in sys.argv[1:]:
        from ._describe import DESCRIBE
        print(json.dumps(DESCRIBE))
        raise SystemExit(0)

    parser = argparse.ArgumentParser(prog="seren_soma", description="SerenSoma - the body's nervous system.")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to seren-soma.yaml (default: $SEREN_SOMA_CONFIG, then ~/seren-soma/seren-soma.yaml).")
    parser.add_argument("--advertise", default=None, help="This box's address as the robot reaches it.")
    parser.add_argument("--state", default=None, help="The state folder (tokens, what was heard, the log).")
    args = parser.parse_args()

    from aiohttp import web
    from seren_meninges.exposure import enforce_server

    from . import __version__, voice
    from ._describe import ACCENT
    from ._diag import diag
    from .config import load_config

    cfg = load_config(args.config)
    if args.advertise:
        cfg.advertise = args.advertise
    if args.state:
        cfg.state_dir = args.state
    cfg.state.mkdir(parents=True, exist_ok=True)

    # the log goes to stderr (journalctl, NSSM) and to a file the viewer can show
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(), RotatingFileHandler(cfg.state / "seren-soma.log", maxBytes=1_000_000,
                                                             backupCount=3, encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    rt = cfg.runtime()
    # LAN by nature, so the API token is required; runtime() made one if none was configured
    enforce_server(cfg.server, service="seren-soma", env_prefix="SEREN_SOMA", log=diag)

    html = ""
    try:
        from seren_meninges.viewer import render_from_dir
        html = render_from_dir(Path(__file__).parent / "viewer" / "ui", title="SerenSoma", brand="Seren<b>Soma</b>",
                               subtitle=f"v{__version__} · the body's nervous system", accent=ACCENT)
    except Exception as exc:  # noqa: BLE001 - the body runs without its page
        diag(f"[seren-soma] no viewer ({exc}); the body runs without its page")

    link = voice.Link(rt)
    diag(f"[seren-soma] on {rt.host}:{rt.port}, the robot at {rt.robot_url}, telling it to call {rt.advertise}")
    diag(f"[seren-soma] viewer at http://{rt.advertise}:{rt.port}/viewer; state in {cfg.state}")
    web.run_app(voice.make_app(link, html), host=rt.host, port=rt.port, print=None)


if __name__ == "__main__":
    main()
