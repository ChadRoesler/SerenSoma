#!/usr/bin/env python3
"""
face_studio.py -- a live, editable preview of a StackChan sprite face.

    python firmware/tools/face_studio.py              # opens the studio in your browser
    python firmware/tools/face_studio.py --port 8765 --no-browser

Opens a page that draws the face the way the firmware's custom skin does: the
socket clip, the upper lid sliding by eyeOpen x its own height, mouth frames
picked by weight, brows tilted in tenths of a degree, pulsing decorators. Blink,
lip sync, breathing and idle glances run on the firmware's own timings, so what
moves here is what will move on the device.

Everything is editable: drag parts on the canvas, change numbers, pose every
emotion, add mouth shapes and decorators. Save writes face.yaml (the old one is
kept as face.yaml.bak-<time>). Layers can come straight out of a PixlPunkt .pxp:
full-canvas layers are cropped and placed for their part automatically.

Listens on 127.0.0.1 only, and answers only pages from this machine. Reads and
writes only inside the open folder (Open folder... in the page, or --root); the
choice is remembered in ~/.face_studio.json. Needs PyYAML (pip install pyyaml). Building for the device is still face.py's job.

"""
import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent          # the open folder; Open folder... or --root changes it
SETTINGS = Path.home() / ".face_studio.json"
PAGE = TOOLS / "face_studio.html"
HEAD_TOP = TOOLS / "stackChanTop.png"
sys.path.insert(0, str(TOOLS))
import face as facetool  # noqa: E402  (face.py: DEFAULT_EMOTIONS, screen size, name limit)

try:
    import yaml
except ImportError:
    sys.exit("face_studio needs PyYAML:  pip install pyyaml")

SKIP_DIRS = {"node_modules", "xiaozhi-esp32", "build", "managed_components", "__pycache__"}


def settings():
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def save_settings(**changes):
    SETTINGS.write_text(json.dumps({**settings(), **changes}, indent=1), encoding="utf-8")


def remember_root():
    save_settings(root=str(ROOT))


def remembered_root():
    try:
        p = Path(settings()["root"])
    except (KeyError, TypeError):
        return None
    return p if p.is_dir() else None


def set_root(path):
    """Open another folder. Only a real folder: nothing is created here."""
    global ROOT
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError(f"no such folder: {path}")
    ROOT = p.resolve()
    remember_root()


PICK_FOLDER = """
import sys, tkinter, tkinter.filedialog as d
w = tkinter.Tk(); w.withdraw(); w.attributes("-topmost", True)
print(d.askdirectory(title="Choose the folder your faces live in", initialdir=sys.argv[1]) or "")
"""


def pick_folder():
    """The operating system's own folder chooser, in its own process so tkinter keeps out of the
    server's threads. None if this Python has no tkinter; the page then asks for a typed path."""
    try:
        r = subprocess.run([sys.executable, "-c", PICK_FOLDER, str(ROOT)], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def inside(rel):
    """Resolve a path the page sent, refusing anything outside the open folder."""
    p = (ROOT / unquote(rel or "")).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise PermissionError(rel)
    return p


def walk(base, depth=4):
    if depth < 0:
        return
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for e in entries:
        if e.is_dir():
            if e.name not in SKIP_DIRS and not e.name.startswith("."):
                yield from walk(e, depth - 1)
        else:
            yield e


# ------------------------------------------------------------------ face.yaml

def emit_yaml(doc, header):
    """face.yaml in the hand-written shape: blocks for sections, one flow line
    per decorator and emotion, flow lists for positions and frames. Checked by
    reading it back; on any mismatch the plain PyYAML dump is written instead."""
    plain = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-/ ]*$")
    special = {"true", "false", "yes", "no", "on", "off", "null", "y", "n"}

    def scalar(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if v is None:
            return "null"
        if isinstance(v, (int, float)):
            return repr(v)
        s = str(v)
        if plain.match(s) and s.lower() not in special and not s.endswith(" "):
            return s
        return json.dumps(s)

    def flow(v):
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k}: {flow(x)}" for k, x in v.items()) + "}"
        if isinstance(v, list):
            return "[" + ", ".join(flow(x) for x in v) + "]"
        return scalar(v)

    lines = []

    def block(d, indent, flow_children=False):
        pad = "  " * indent
        for k, v in d.items():
            if isinstance(v, dict) and v and not flow_children:
                lines.append(f"{pad}{k}:")
                block(v, indent + 1, flow_children=k in ("decorators", "emotions"))
            else:
                lines.append(f"{pad}{k}: {flow(v)}")

    for top in ("face", "parts", "emotions"):
        if top in doc:
            if lines:
                lines.append("")
            if isinstance(doc[top], dict) and doc[top]:
                lines.append(f"{top}:")
                block(doc[top], 1, flow_children=top == "emotions")
            else:
                lines.append(f"{top}: {flow(doc[top] or {})}")
    for k, v in doc.items():
        if k not in ("face", "parts", "emotions"):
            lines.append(yaml.safe_dump({k: v}, sort_keys=False).rstrip())
    text = (header + "\n" if header else "") + "\n".join(lines) + "\n"
    if yaml.safe_load(text) != doc:
        text = (header + "\n" if header else "") + yaml.safe_dump(doc, sort_keys=False)
    return text


def header_of(text):
    out = []
    for line in text.splitlines():
        if line.startswith("#") or (out and not line.strip()):
            out.append(line)
        else:
            break
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


# ------------------------------------------------------------------ PixlPunkt .pxp

def pxp_layers(path):
    """Every raster layer in a .pxp: name, visible, pixels (BGRA, straight alpha).

    Finds layers by their pixel block (width, height, byte count = w*h*4) and
    reads the fields PixlPunkt writes just before it: name, visible, locked,
    blend, opacity. Folder nesting is not needed for picking parts."""
    b = path.read_bytes()
    if struct.unpack_from("<i", b, 0)[0] != 0x31505850:
        raise ValueError("not a PixlPunkt document")
    p, n, s = 8, 0, 0
    while True:
        c = b[p]
        p += 1
        n |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            break
    p += n
    w, h = struct.unpack_from("<ii", b, p)
    pat = struct.pack("<iii", w, h, w * h * 4)
    out, off = [], b.find(pat)
    while off != -1:
        end, name = off - 7, f"layer {len(out)}"
        for length in range(0, 128):
            s0 = end - length - 1
            if s0 >= 0 and b[s0] == length:
                try:
                    cand = b[s0 + 1:end].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if cand.isprintable():
                    name = cand
                    break
        out.append({"name": name, "visible": bool(b[off - 7]), "w": w, "h": h,
                    "pixels": b[off + 12: off + 12 + w * h * 4]})
        off = b.find(pat, off + 12 + w * h * 4)
    return out


def bbox(px, w, h):
    a = px[3::4]
    rows = [any(a[y * w:(y + 1) * w]) for y in range(h)]
    if not any(rows):
        return None
    y0, y1 = rows.index(True), h - rows[::-1].index(True)
    cols = [any(a[y * w + x] for y in range(y0, y1)) for x in range(w)]
    x0, x1 = cols.index(True), w - cols[::-1].index(True)
    return [x0, y0, x1, y1]


def png_rgba(w, h, bgra):
    rgba = bytearray(bgra)
    rgba[0::4], rgba[2::4] = bgra[2::4], bgra[0::4]
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


_pxp_cache = {}


def cached_layers(path):
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _pxp_cache:
        _pxp_cache.clear()
        layers = pxp_layers(path)
        for L in layers:
            L["bbox"] = bbox(L["pixels"], L["w"], L["h"])
        _pxp_cache[key] = layers
    return _pxp_cache[key]


# ------------------------------------------------------------------ the robot

MAX_BUNDLE = 2 * 1024 * 1024         # what the firmware will load from the card (hal_sdcard.cpp)
JOB = {"running": False, "phase": "", "ok": None}


def bot():
    b = settings().get("bot") or {}
    return (b.get("url") or "").rstrip("/"), b.get("token") or ""   # set in the page (Robot...)


def bot_call(path, body=None, headers=None, timeout=8, method=None):
    url, token = bot()
    h = {"X-StackChan-Token": token} if token else {}
    h.update(headers or {})
    req = urllib.request.Request(url + path, data=body, headers=h, method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def bot_error(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return "the robot refused the token"
        try:
            return json.loads(e.read()).get("error") or f"the robot said {e.code}"
        except ValueError:
            return f"the robot said {e.code}"
    return f"can't reach the robot at {bot()[0]}"


def bundle_of(folder):
    """Every built file: [u8 name length][name][u32 size][bytes], the shape face_push.cpp unpacks."""
    out = bytearray()
    for f in sorted(p for p in Path(folder).iterdir() if p.is_file()):
        name, data = f.name.encode(), f.read_bytes()
        out += struct.pack("<B", len(name)) + name + struct.pack("<I", len(data)) + data
    return bytes(out)


def wait_for_reboot(was_up, seconds=90):
    """The robot's answer once it's back: uptime has gone backwards. None if it never returned."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(1.5)
        try:
            s = bot_call("/api/status", timeout=3)
        except (OSError, ValueError):
            continue
        if s.get("uptimeMs", 0) < was_up:
            return s
    return None


def job_step(phase):
    JOB["phase"] = phase


def run_send(face_dir):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            job_step("Building the face…")
            r = subprocess.run([sys.executable, str(TOOLS / "face.py"), "build", str(face_dir), "--out", tmp],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError("the build failed: " + (r.stderr or r.stdout).strip()[-300:])
            data = bundle_of(tmp)
        if len(data) > MAX_BUNDLE:
            raise RuntimeError(f"the face is {len(data) // 1024} KB, and the robot holds {MAX_BUNDLE // 1024} KB")
        was_up = bot_call("/api/status").get("uptimeMs", 0)
        job_step(f"Sending {len(data) // 1024} KB to the robot…")
        bot_call("/api/face", data, {"X-Face-Crc32": f"{zlib.crc32(data):08x}", "Content-Type": "application/octet-stream"}, timeout=240)
        job_step("Rebooting the robot…")
        bot_call("/api/reboot", b"{}")
        s = wait_for_reboot(was_up)
        if s is None:
            raise RuntimeError("the robot took the face but didn't come back. Check that it's on.")
        result = s.get("facePush") or "the robot came back without saying what happened"
        JOB.update(phase=result[0].upper() + result[1:] + ".", ok=result.startswith("installed"))
    except (OSError, ValueError) as e:
        JOB.update(phase=bot_error(e) + ".", ok=False)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        JOB.update(phase=str(e), ok=False)
    finally:
        JOB["running"] = False


def run_restore():
    try:
        was_up = bot_call("/api/status").get("uptimeMs", 0)
        job_step("Asking the robot for its previous face…")
        bot_call("/api/face-restore", b"{}")
        bot_call("/api/reboot", b"{}")
        s = wait_for_reboot(was_up)
        if s is None:
            raise RuntimeError("the robot didn't come back. Check that it's on.")
        result = s.get("facePush") or "the robot came back without saying what happened"
        JOB.update(phase=result[0].upper() + result[1:] + ".", ok=result.startswith("previous"))
    except (OSError, ValueError) as e:
        JOB.update(phase=bot_error(e) + ".", ok=False)
    except RuntimeError as e:
        JOB.update(phase=str(e), ok=False)
    finally:
        JOB["running"] = False


def start_job(target, *args):
    if JOB["running"]:
        raise ValueError("the robot is busy with the last request")
    JOB.update(running=True, phase="Starting…", ok=None)
    threading.Thread(target=target, args=args, daemon=True).start()


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def from_this_machine(self):
        """Saves write files, so a web page elsewhere must not reach this server through the browser
        (a cross-site POST, or DNS rebinding a name onto 127.0.0.1)."""
        local = ("127.0.0.1", "localhost")
        host = urlparse("//" + (self.headers.get("Host") or "")).hostname
        origin = self.headers.get("Origin")
        return host in local and (not origin or urlparse(origin).hostname in local)

    def do_GET(self):
        if not self.from_this_machine():
            return self.send(403, {"error": "Face Studio only answers pages from this machine"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/stackChanTop.png":
                return self.send(200, HEAD_TOP.read_bytes(), "image/png")
            if u.path == "/api/home":
                faces, pxps = [], []
                for f in walk(ROOT):
                    if f.name in ("face.yaml", "face.yml"):
                        faces.append(f.parent.relative_to(ROOT).as_posix())
                    elif f.suffix.lower() == ".pxp":
                        pxps.append(f.relative_to(ROOT).as_posix())
                # a folder holding a .pxp but no face.yaml yet is a face waiting to be started
                waiting = sorted({str(Path(p).parent.as_posix()) for p in pxps} - set(faces))
                return self.send(200, {"root": str(ROOT), "faces": faces, "waiting": waiting, "pxps": pxps,
                                       "defaults": facetool.DEFAULT_EMOTIONS,
                                       "screen": [facetool.SCREEN_W, facetool.SCREEN_H],
                                       "max_asset_name": facetool.MAX_ASSET_NAME})
            if u.path == "/api/bot":
                url, token = bot()
                return self.send(200, {"url": url, "hasToken": bool(token), "job": JOB})
            if u.path == "/api/face":
                d = inside(q["dir"])
                y = next((d / n for n in ("face.yaml", "face.yml") if (d / n).exists()), None)
                doc = (yaml.safe_load(y.read_text(encoding="utf-8")) or {}) if y else {}
                files = sorted(p.name for p in d.iterdir() if p.suffix.lower() == ".png") if d.is_dir() else []
                return self.send(200, {"doc": doc, "files": files, "stamp": str(y.stat().st_mtime_ns) if y else ""})
            if u.path.startswith("/file/"):
                return self.send(200, inside(u.path[len("/file/"):]).read_bytes(), "image/png")
            if u.path == "/api/pxp":
                layers = cached_layers(inside(q["path"]))
                return self.send(200, [{"i": i, "name": L["name"], "visible": L["visible"], "bbox": L["bbox"]}
                                       for i, L in enumerate(layers)])
            if u.path == "/api/pxp-layer":
                L = cached_layers(inside(q["path"]))[int(q["i"])]
                return self.send(200, png_rgba(L["w"], L["h"], L["pixels"]), "image/png")
            return self.send(404, {"error": "no such page"})
        except PermissionError:
            return self.send(403, {"error": "outside the open folder"})
        except (KeyError, FileNotFoundError, ValueError, IndexError) as e:
            return self.send(400, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self.from_this_machine():
            return self.send(403, {"error": "Face Studio only answers pages from this machine"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            if u.path == "/api/bot":
                b = json.loads(body)
                now = settings().get("bot") or {}
                if "url" in b:
                    now["url"] = str(b["url"]).strip()
                if "token" in b:
                    now["token"] = str(b["token"]).strip()
                save_settings(bot=now)
                return self.send(200, {"url": bot()[0], "hasToken": bool(bot()[1])})
            if u.path == "/api/bot/send":
                start_job(run_send, inside(q["dir"]))
                return self.send(200, {"ok": True})
            if u.path == "/api/bot/restore":
                start_job(run_restore)
                return self.send(200, {"ok": True})
            if u.path == "/api/bot/try":
                b = json.loads(body)
                try:
                    # the color being worked on, not whatever the face on the robot has for this pose
                    lit = facetool.lights_rgb(b.get("lights")) or [0, 0, 0]
                    bot_call("/api/emotion", json.dumps({"name": b["name"], "lights": lit, "lightsPulse": facetool.lights_pulse(b.get("lights")),
                                                         "look": facetool.sided_pair(b.get("look")), "brow": facetool.sided_pair(b.get("brow")),
                                                         "mouthCycle": int(b.get("mouthCycle") or 0), "roll": int(b.get("roll") or 0)}).encode(),
                             {"Content-Type": "application/json"})
                    tw = b.get("twitch")
                    bot_call("/api/twitch", json.dumps(tw or {"off": True}).encode(), {"Content-Type": "application/json"})
                except (OSError, ValueError) as e:
                    return self.send(502, {"error": bot_error(e)})
                return self.send(200, {"ok": True})
            if u.path == "/api/pick-folder":
                path = pick_folder()
                if path is None:
                    return self.send(200, {"unavailable": True})
                if path:
                    set_root(path)
                return self.send(200, {"root": str(ROOT), "changed": bool(path)})
            if u.path == "/api/root":
                set_root(json.loads(body)["path"])
                return self.send(200, {"root": str(ROOT)})
            if u.path == "/api/save":
                d = inside(q["dir"])
                y = d / "face.yaml"
                if q.get("new") and y.exists():
                    return self.send(409, {"error": f"{q['dir']} already has a face. Pick another name."})
                # something else wrote it after this page opened it, and this page doesn't have that
                if q.get("since") and not q.get("force") and y.exists() and str(y.stat().st_mtime_ns) != q["since"]:
                    return self.send(409, {"changed": True, "error": "face.yaml changed on disk since this page opened it"})
                d.mkdir(parents=True, exist_ok=True)
                doc = json.loads(body)
                header = "# StackChan face -- see firmware/docs/custom-faces.md"
                if y.exists():
                    header = header_of(y.read_text(encoding="utf-8")) or header
                    shutil.copy2(y, d / f"face.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                y.write_text(emit_yaml(doc, header), encoding="utf-8")
                return self.send(200, {"ok": True, "stamp": str(y.stat().st_mtime_ns)})
            if u.path == "/api/file":
                d = inside(q["dir"])
                d.mkdir(parents=True, exist_ok=True)
                name = Path(q["name"]).name
                if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.png", name):
                    raise ValueError("file names are letters, digits, _ . - and end in .png")
                target = d / name
                if target.exists():
                    keep = d / ".studio-backup"       # the version this import replaced
                    keep.mkdir(exist_ok=True)
                    shutil.copy2(target, keep / name)
                target.write_bytes(body)
                return self.send(200, {"ok": True, "name": name})
            return self.send(404, {"error": "no such action"})
        except PermissionError:
            return self.send(403, {"error": "outside the open folder"})
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return self.send(400, {"error": f"{type(e).__name__}: {e}"})


def main():
    ap = argparse.ArgumentParser(description="Live, editable preview of a StackChan sprite face.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--root", help="the folder to look for faces in (default: the last one opened)")
    args = ap.parse_args()
    global ROOT
    if args.root:
        set_root(args.root)
    else:
        ROOT = remembered_root() or ROOT
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Face Studio on {url}  (folder: {ROOT})  Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
