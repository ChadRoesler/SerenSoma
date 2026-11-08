"""SerenSoma's config: dataclasses on the shared Meninges blocks.

Read from, first found wins:

    --config  ->  $SEREN_SOMA_CONFIG  ->  ~/seren-soma/seren-soma.yaml  ->  defaults

It binds 0.0.0.0 by default: the robot dials in over the LAN, so loopback would be a body with no
nervous system. That makes the API token mandatory (Meninges' enforce_server), and when no
server.bearer_token_* pointer is set it's the one in <state>/token, made the first time.

The robot's control token and Lodestar's are POINTERS too: `*_env` (an env var's name), `*_keyring`
("service/username"), `*_file` (a file holding only the token), or inline as the escape hatch. With no
Lodestar given, it's read from the Symposium config on this box, as it always was.
"""
from __future__ import annotations

import os
import socket
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from seren_meninges import ServerConfig, resolve_token

from ._describe import PORT as DEFAULT_PORT
from ._diag import diag

try:
    import yaml
    _HAS_YAML = True
except ImportError:  # pragma: no cover - pyyaml is a dependency
    _HAS_YAML = False

DEFAULT_HOST = "0.0.0.0"


@dataclass
class Pointer:
    """Where a token is: a file, an env var's name, a keychain ref, or inline."""
    token: str = field(default="", repr=False)
    token_env: str = ""
    token_keyring: str = ""
    token_file: str = ""

    def resolve(self, fallback_file: Optional[Path] = None) -> str:
        path = self.token_file or (str(fallback_file) if fallback_file and not (self.token or self.token_env or self.token_keyring) else "")
        if path:
            try:
                found = Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
                if found:
                    return found
            except OSError:
                if self.token_file:
                    diag(f"[seren-soma] can't read the token file {path}")
        return resolve_token(inline=self.token or None, keyring_ref=self.token_keyring or None,
                             env_var=self.token_env or None)

    @classmethod
    def from_dict(cls, d: Any) -> "Pointer":
        d = d if isinstance(d, dict) else {}
        return cls(token=str(d.get("token") or ""), token_env=str(d.get("token_env") or ""),
                   token_keyring=str(d.get("token_keyring") or ""), token_file=str(d.get("token_file") or ""))


@dataclass
class RobotConfig:
    url: str = ""        # the robot's control API, e.g. http://192.0.2.21:8080 (its screen shows the address)
    auth: Pointer = field(default_factory=Pointer)       # its control token; default <state>/control_token
    # Who /ota/ answers. Empty: the robot's own address, looked up from url at startup.
    device_from: List[str] = field(default_factory=list)


@dataclass
class LodestarConfig:
    url: str = ""                                        # blank: from symposium_yaml
    auth: Pointer = field(default_factory=Pointer)
    symposium_yaml: str = ""                             # a Symposium config to read Lodestar's url and bearer from


@dataclass
class SomaConfig:
    server: ServerConfig = field(default_factory=lambda: ServerConfig(host=DEFAULT_HOST, port=DEFAULT_PORT))
    state_dir: str = "~/seren-soma/state"
    advertise: str = ""        # this box as the robot reaches it; blank: worked out at startup
    voice: str = ""            # the voice Lodestar speaks in; blank: Lodestar's own default
    idle_close_seconds: float = 60.0
    updates: bool = True       # "is there a newer seren-soma", checked against PyPI
    firmware_repo: str = "ChadRoesler/SerenSoma"   # where fw-v* releases come from (GitHub owner/repo)
    robot: RobotConfig = field(default_factory=RobotConfig)
    lodestar: LodestarConfig = field(default_factory=LodestarConfig)

    @property
    def state(self) -> Path:
        return Path(os.path.expanduser(self.state_dir)).resolve()

    def runtime(self) -> Namespace:
        """Everything the server needs, resolved: tokens read, addresses worked out. Raises SystemExit
        with the reason when something it can't run without is missing."""
        from .voice import lodestar_from_symposium, token_file, use_state
        use_state(self.state)
        api = self.server.resolve_bearer()
        if not api:
            # the default: a token of its own in the state folder, made the first time
            api = token_file("token")
            self.server.bearer_token = api
        if not self.robot.url:
            raise SystemExit("[seren-soma] robot.url isn't set: the robot's control API, like "
                             "http://192.0.2.21:8080 (the robot's screen shows its address)")
        robot_host = urlparse(self.robot.url).hostname or ""
        device_from = set(self.robot.device_from)
        if not device_from and robot_host:
            try:
                device_from = {socket.gethostbyname(robot_host)}
            except OSError:
                diag(f"[seren-soma] can't look up the robot's address {robot_host}; the check-in will answer anyone")
        if self.lodestar.url:
            lodestar_url, lodestar_token = self.lodestar.url, self.lodestar.auth.resolve()
        elif not self.lodestar.symposium_yaml:
            raise SystemExit("[seren-soma] no Lodestar: set lodestar.url (and its token), or lodestar.symposium_yaml "
                             "to read them from a Symposium config")
        else:
            lodestar_url, lodestar_token = lodestar_from_symposium(Path(os.path.expanduser(self.lodestar.symposium_yaml)))
        return Namespace(
            host=self.server.host, port=self.server.port, advertise=self.advertise or _this_box(robot_host),
            voice=self.voice, device=robot_host, updates=self.updates, robot_url=self.robot.url, idle_close=self.idle_close_seconds,
            api_token=api, device_token=token_file("device_token"),
            robot_token=self.robot.auth.resolve(self.state / "control_token"), device_from=device_from,
            robot_token_file=_robot_token_file_for(self.robot.auth, self.state),
            state=self.state, firmware_repo=self.firmware_repo,
            lodestar_url=lodestar_url.rstrip("/"), lodestar_token=lodestar_token)


def _robot_token_file_for(auth: "Pointer", state: Path) -> str:
    """Where the robot's token is written when it's made or rotated here: its token_file, else the
    state folder's control_token when nothing else names it. "" when it lives somewhere a write
    can't reach (an env var, a keychain, the config itself)."""
    if auth.token_file:
        return os.path.expanduser(auth.token_file)
    if auth.token or auth.token_env or auth.token_keyring:
        return ""
    return str(state / "control_token")


def _this_box(toward: str) -> str:
    """The address this box uses to reach the robot, which is the one the robot can reach it on."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((toward or "10.255.255.255", 9))   # no packet is sent: this only picks a route
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _candidate_paths(explicit: Optional[str]) -> List[Path]:
    out: List[Path] = []
    if explicit:
        out.append(Path(explicit))
    env = os.environ.get("SEREN_SOMA_CONFIG")
    if env:
        out.append(Path(env))
    out.append(Path.home() / "seren-soma" / "seren-soma.yaml")
    return out


def _read_yaml(path: Optional[str]) -> Dict[str, Any]:
    for candidate in _candidate_paths(path):
        if not candidate.is_file():
            continue
        if not _HAS_YAML:
            diag(f"[seren-soma] pyyaml missing; ignoring {candidate}")
            return {}
        try:
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("top level of the config is not a mapping")
            diag(f"[seren-soma] config: {candidate}")
            return loaded
        except Exception as exc:  # noqa: BLE001 - lenient for a malformed file, and it says so
            diag(f"[seren-soma] {candidate} is malformed ({exc}); falling back to defaults")
            return {}
    return {}


def load_config(path: Optional[str] = None) -> SomaConfig:
    d = _read_yaml(path)
    raw = d.get("server")
    cfg = SomaConfig(server=ServerConfig.from_dict(raw if isinstance(raw, dict) else {}, default_port=DEFAULT_PORT,
                                                   default_host=DEFAULT_HOST))
    cfg.state_dir = str(d.get("state_dir") or cfg.state_dir)
    cfg.advertise = str(d.get("advertise") or "")
    cfg.voice = str(d.get("voice") or cfg.voice)
    cfg.firmware_repo = str(d.get("firmware_repo") or cfg.firmware_repo)
    up = d.get("updates")
    cfg.updates = bool(up.get("enabled", True)) if isinstance(up, dict) else (bool(up) if up is not None else True)
    try:
        cfg.idle_close_seconds = float(d.get("idle_close_seconds", cfg.idle_close_seconds))
    except (TypeError, ValueError):
        pass
    r = d.get("robot") if isinstance(d.get("robot"), dict) else {}
    cfg.robot = RobotConfig(url=str(r.get("url") or RobotConfig.url), auth=Pointer.from_dict(r),
                            device_from=[str(x) for x in (r.get("device_from") or [])])
    lo = d.get("lodestar") if isinstance(d.get("lodestar"), dict) else {}
    cfg.lodestar = LodestarConfig(url=str(lo.get("url") or ""), auth=Pointer.from_dict(lo),
                                  symposium_yaml=str(lo.get("symposium_yaml") or LodestarConfig.symposium_yaml))
    if v := os.environ.get("SEREN_SOMA_ADVERTISE"):
        cfg.advertise = v
    if v := os.environ.get("SEREN_SOMA_STATE_DIR"):
        cfg.state_dir = v
    return cfg
