"""Firmware for the robot, from here: fetched from a release (or handed over), checked, and offered to
the robot once on its next check-in.

The robot already knows how to take it. Its check-in (POST /ota/) can answer with
``{"firmware": {"version", "url", "force": 1}}``, and the firmware downloads that URL into its other
app slot, checks the image, and reboots into it. If the new image can't check in, the bootloader
rolls back on the next reboot: only a check-in that succeeds marks an image good.

So the steps are:

1. stage: put an image in <state>/firmware, from a GitHub release (``fetch_release``, the
   ``-update.zip``, checked against the release's SHA256SUMS) or uploaded (``stage_bytes``, for a
   build of your own).
2. arm: say which one. Nothing happens yet.
3. the robot reboots and checks in, is offered the armed image once, and the offer is disarmed as
   it's made, so a robot that can't take it isn't offered it again and again.

Only the app is replaced. The assets partition (the stock face and icons) stays as it was, which is
fine while releases don't change it; a release that does says so, and needs the full image by USB.

The image is served only to the robot's address, like the check-in: a build of your own can hold
your token.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# The firmware's app slots (partitions.csv: ota_0 and ota_1 are 0x4f0000 each).
MAX_APP_BYTES = 0x4F0000
# An ESP-IDF app image starts with this byte (esp_image_header_t.magic).
IMAGE_MAGIC = 0xE9
# The release asset names firmware-release.yml makes.
UPDATE_ZIP = "soma-{v}-update.zip"
APP_IN_ZIP = "stack-chan.bin"

VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")


class FirmwareError(ValueError):
    """Something about an image or a release that someone can fix, said so they can."""


@dataclass
class Staged:
    version: str
    file: str            # under <state>/firmware
    size: int
    sha256: str
    source: str          # "release fw-v0.1.1" or "uploaded"
    staged_at: float


def firmware_dir(state: Path) -> Path:
    d = Path(state) / "firmware"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def check_version(version: str) -> str:
    v = (version or "").strip()
    if v.startswith("fw-v"):
        v = v[4:]
    if not VERSION_RE.match(v):
        raise FirmwareError(f"{version!r} isn't a version like 0.1.1")
    return v


def check_image(data: bytes) -> None:
    """An app image, small enough for a slot. Not proof it boots (the bootloader is that), but enough
    to refuse a zip, a merged full image, or half a download before the robot spends a minute on it."""
    if len(data) < 1024:
        raise FirmwareError("that's too small to be a firmware image")
    if data[0] != IMAGE_MAGIC:
        raise FirmwareError("that isn't an ESP32 app image (stack-chan.bin from the update zip, not the -full.bin)")
    if len(data) > MAX_APP_BYTES:
        raise FirmwareError(f"that's {len(data)} bytes; an app slot holds {MAX_APP_BYTES} "
                            "(the -full.bin goes on by USB, the robot takes stack-chan.bin)")


def _index(state: Path) -> Path:
    return firmware_dir(state) / "staged.json"


def staged(state: Path) -> list[Staged]:
    """What's staged, newest first. Entries whose file has gone are dropped."""
    try:
        rows = json.loads(_index(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    d = firmware_dir(state)
    out = [Staged(**r) for r in rows if isinstance(r, dict) and (d / str(r.get("file", ""))).is_file()]
    return sorted(out, key=lambda s: s.staged_at, reverse=True)


def find(state: Path, version: str) -> Optional[Staged]:
    v = check_version(version)
    return next((s for s in staged(state) if s.version == v), None)


def stage_bytes(state: Path, version: str, data: bytes, source: str, keep: int = 3) -> Staged:
    """Keep an image under <state>/firmware as soma-<version>.bin, replacing one of that version,
    and keep only the newest few."""
    v = check_version(version)
    check_image(data)
    d = firmware_dir(state)
    name = f"soma-{v}.bin"
    tmp = d / (name + ".new")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)     # a build of your own can hold your token
    except OSError:
        pass
    os.replace(tmp, d / name)
    entry = Staged(version=v, file=name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                   source=source, staged_at=time.time())
    rows = [s for s in staged(state) if s.version != v]
    rows.insert(0, entry)
    for old in rows[keep:]:
        try:
            (d / old.file).unlink()
        except OSError:
            pass
    rows = rows[:keep]
    _index(state).write_text(json.dumps([asdict(s) for s in rows], indent=1), encoding="utf-8")
    return entry


def app_from_update_zip(zip_bytes: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == APP_IN_ZIP]
            if not names:
                raise FirmwareError(f"the update zip has no {APP_IN_ZIP}")
            return z.read(names[0])
    except zipfile.BadZipFile:
        raise FirmwareError("that download isn't a zip")


def sums_for(text: str) -> dict[str, str]:
    """SHA256SUMS ("<hex>  <name>" lines) as name -> hex."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


def pick_release(releases: list[dict], version: str) -> dict:
    """The fw-v* release asked for ("latest" is the newest that isn't a draft or prerelease)."""
    fw = [r for r in releases if isinstance(r, dict) and str(r.get("tag_name", "")).startswith("fw-v")
          and not r.get("draft")]
    if version in ("", "latest"):
        fw = [r for r in fw if not r.get("prerelease")]
        if not fw:
            raise FirmwareError("no firmware release found (tags fw-v*)")
        return max(fw, key=lambda r: r.get("published_at") or "")
    v = check_version(version)
    for r in fw:
        if r["tag_name"] == f"fw-v{v}":
            return r
    raise FirmwareError(f"no release fw-v{v}")


def asset_sources(release: dict, name: str) -> list[tuple[str, dict]]:
    """Where to download an asset, best first: the API's own asset URL (asked for as octet-stream),
    then the web link. They're different front doors: on 8 Nov 2026 the web one answered 504 for
    every asset, from every network, while the API one served the 12 MB image in 1.5 s."""
    for a in release.get("assets", []):
        if a.get("name") == name:
            out = []
            if a.get("url"):
                out.append((a["url"], {"User-Agent": "seren-soma", "Accept": "application/octet-stream"}))
            if a.get("browser_download_url"):
                out.append((a["browser_download_url"], {"User-Agent": "seren-soma"}))
            if out:
                return out
    raise FirmwareError(f"release {release.get('tag_name')} has no {name}")


def verified_app(release: dict, zip_bytes: bytes, sums_text: str) -> bytes:
    """The app image from a release's update zip, after the zip matches the release's SHA256SUMS."""
    v = release["tag_name"][4:]
    name = UPDATE_ZIP.format(v=v)
    want = sums_for(sums_text).get(name)
    if not want:
        raise FirmwareError(f"SHA256SUMS in {release['tag_name']} doesn't list {name}")
    got = hashlib.sha256(zip_bytes).hexdigest()
    if got != want:
        raise FirmwareError(f"{name} doesn't match its SHA256SUMS line: not installing it")
    app = app_from_update_zip(zip_bytes)
    check_image(app)
    return app
