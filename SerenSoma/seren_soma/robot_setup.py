"""Setting up a robot from here: the soma.yaml keys, with the blanks this box can fill already filled.

Two ways in, same keys (the firmware's soma_config.h has the rules):

- A robot already on the network: POST them to its /api/config with the token it has now, and it
  saves them and reboots. ``push``.
- A robot that isn't yet (a new one, or one whose Wi-Fi changed): a soma.yaml to put on its card,
  which it reads once at boot and deletes. ``card_text``.

What this box fills in: ``voice`` is its own check-in address, and the token is the one it already
holds for the robot (made and saved here if it has none). Wi-Fi is whatever the person types and is
never kept here. A tailnet key the same.

Rotating the token is the one step that can lock us out, so the order is fixed: the robot takes the
new one first, then this box writes it down, then the robot reboots onto it. If the robot says no,
nothing here changes.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Optional

# What the firmware will take (soma_config.cpp's caps).
CAPS = {"wifi.ssid": 32, "wifi.password": 64, "voice": 128, "token": 95, "tailnet.key": 128, "tailnet.name": 63}


class SetupError(ValueError):
    """Something a person asked for that can't be done, said so they can fix it."""


def check_in_url(advertise: str, port: int) -> str:
    """This box's check-in, as the robot reaches it."""
    return f"http://{advertise}:{port}/ota/"


def new_token() -> str:
    """43 URL-safe characters: well under the firmware's 95, and nothing a YAML quote can trip on."""
    return secrets.token_urlsafe(32)


def write_token(path: Path, token: str) -> None:
    """Replace a token file without a moment where it's half written, keeping the old one beside it
    as <name>.previous so a lockout has a way back. Owner-only, like every token file here."""
    path = Path(os.path.expanduser(str(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        prev = path.with_name(path.name + ".previous")
        prev.write_bytes(path.read_bytes())
        _private(prev)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(token, encoding="utf-8")
    _private(tmp)
    os.replace(tmp, path)


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows: ACLs, not modes
        pass


def keys_from(body: dict[str, Any], *, advertise: str, port: int) -> dict[str, Any]:
    """The soma.yaml keys a request asked for, checked against what the firmware takes.

    body: {"wifi": {"ssid", "password"}, "voice": "auto" | "<address>", "tailnet": {"key", "name"}}.
    "auto" (or leaving voice out) is this box. The token is never taken from the request: it's this
    box's to decide (``push`` and ``card_text`` add it).
    """
    if not isinstance(body, dict):
        raise SetupError("expected a JSON object")
    keys: dict[str, Any] = {}

    voice = body.get("voice", "auto")
    if voice is not None:
        if not isinstance(voice, str):
            raise SetupError("voice is an address, or \"auto\" for this box")
        keys["voice"] = check_in_url(advertise, port) if voice.strip() in ("", "auto") else voice.strip()
        if "\n" in keys["voice"] or " " in keys["voice"]:
            raise SetupError("voice is one address, no spaces")

    for section, fields in (("wifi", ("ssid", "password")), ("tailnet", ("key", "name"))):
        given = body.get(section)
        if given is None:
            continue
        if not isinstance(given, dict):
            raise SetupError(f"{section} is an object: {{{', '.join(fields)}}}")
        part = {f: given[f] for f in fields if given.get(f) is not None}
        for f, v in part.items():
            if not isinstance(v, str):
                raise SetupError(f"{section}.{f} is text")
            if "\n" in v or "\r" in v:
                raise SetupError(f"{section}.{f} can't have a line break in it")
        if section == "wifi" and part and not part.get("ssid", "").strip():
            raise SetupError("wifi needs an ssid")
        if part:
            keys[section] = part

    for name, cap in CAPS.items():
        value = _get(keys, name)
        if value is not None and len(value) > cap:
            raise SetupError(f"{name} is longer than the robot takes ({cap} characters)")
    return keys


def _get(keys: dict[str, Any], dotted: str) -> Optional[str]:
    part: Any = keys
    for step in dotted.split("."):
        if not isinstance(part, dict) or step not in part:
            return None
        part = part[step]
    return part


def _quote(value: str) -> str:
    """A value the robot's parser takes whole. It has no escapes: a quoted value runs to the next
    matching quote, so pick the quote the value doesn't contain."""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise SetupError("a value with both kinds of quote can't go in soma.yaml; use wifi.txt for that password")


def card_text(keys: dict[str, Any], token: str) -> str:
    """soma.yaml for the card: these keys plus the token, in the shape the robot reads."""
    lines = [
        "# soma.yaml - made by seren-soma. Copy to the root of the robot's microSD card and boot it.",
        "# The robot reads this once, saves it, and deletes it from the card.",
        "",
    ]
    wifi = keys.get("wifi")
    if wifi:
        lines += ["wifi:", f"  ssid: {_quote(wifi['ssid'])}", f"  password: {_quote(wifi.get('password', ''))}", ""]
    else:
        lines += ["# wifi:            # fill in to change the robot's network", "#   ssid: MyNetwork",
                  '#   password: "..."', ""]
    if keys.get("voice"):
        lines.append(f"voice: {_quote(keys['voice'])}")
    lines.append(f"token: {_quote(token)}")
    tailnet = keys.get("tailnet")
    if tailnet:
        lines += ["", "tailnet:"] + [f"  {k}: {_quote(v)}" for k, v in tailnet.items()]
    return "\n".join(lines) + "\n"
