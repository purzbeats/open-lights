#!/usr/bin/env python3
"""Local Govee controller: LAN API (UDP) first, Govee cloud API for lights without LAN Control.
No dependencies.

    py server.py                 # http://127.0.0.1:8420
    py server.py --host 0.0.0.0  # reachable from your phone on the home network

Cloud: GOVEE_API_KEY is read from the environment, ./.env, or ~/.env.
Set GOVEE_IFACE=192.168.x.y to force which local IP discovery goes out on.
"""
import argparse
import ipaddress
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402

import effects as effects_mod  # noqa: E402
from effects import EFFECT_SCENES, EFFECTS  # noqa: E402
from modes import Modes  # noqa: E402
from sync import ZONE_NAMES, ScreenSync  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "devices.json"
MCAST_GROUP, SCAN_PORT, CMD_PORT, LISTEN_PORT = "239.255.255.250", 4001, 4003, 4002
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
CLOUD_API = "https://openapi.api.govee.com/router/api/v1"

SCAN_EVERY, STATUS_EVERY, OFFLINE_AFTER = 30, 8, 90
CLOUD_STATUS_EVERY, CLOUD_LIST_EVERY = 120, 600   # cloud API is rate limited per day; keep it gentle

lock = threading.RLock()
devices = {}  # id -> {id, sku, ip, via: lan|cloud, name, auto_name, kelvin, status, online, last_seen}
SAVED = ("id", "sku", "ip", "via", "name", "auto_name", "kelvin", "room", "segments", "govee_modes", "bri_restore", "screen_zone", "pos")
# default screen-sync zone by model (everything else: whole screen, ambient)
DEFAULT_ZONES = {"H6046": "sides"}
# Lights that accept the fast "razer" stream, and how many segments they have. Others get plain color commands.
STREAM_SEGMENTS = {"H60B2": 3, "H6046": 10}


# ---------- persistence ----------

def write_json(path, data):
    """Write via a temp file + atomic rename, so a crash mid-save can never leave a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path, default):
    """Read a settings file; if it's unreadable, set it aside (never delete) and start from the default."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        aside = path.with_name(f"{path.stem}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
        try:
            path.replace(aside)
        except OSError:
            pass
        print(f"WARNING: {path.name} was unreadable ({e}); moved to {aside.name}, starting fresh", flush=True)
        return default


def load():
    if STORE.exists():
        for d in read_json(STORE, {}).values():
            d.setdefault("via", "lan")
            if d.get("auto_name") is None:
                d["auto_name"] = d.get("name") == f"{d.get('sku')} · {d['id'][-5:].replace(':', '')}"
            devices[d["id"]] = {**d, "online": False, "status": None, "last_seen": 0}


def save():
    with lock:
        data = {k: {f: v.get(f) for f in SAVED} for k, v in devices.items()}
    write_json(STORE, data)


def load_key():
    if os.environ.get("GOVEE_API_KEY"):
        return os.environ["GOVEE_API_KEY"].strip()
    # this folder, then ~/.env
    for env in (ROOT / ".env", Path.home() / ".env"):
        if not env.exists():
            continue
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip().removeprefix("export ").strip() == "GOVEE_API_KEY" and v.strip():
                return v.strip().strip("\"'")
    return None


API_KEY = load_key()


# ---------- LAN ----------

def lan_ips():
    """Private IPv4s on this machine, skipping Tailscale (it swallows the multicast scan)."""
    if os.environ.get("GOVEE_IFACE"):
        return [os.environ["GOVEE_IFACE"]]
    ips = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:                       # no network yet (e.g. right after login)
        return []
    candidates = [info[4][0] for info in infos]
    try:                                  # the address used for outgoing traffic (nothing is actually sent)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        candidates.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    for c in candidates:
        ip = ipaddress.ip_address(c)
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and ip not in TAILSCALE:
            ips.add(str(ip))
    return sorted(ips)


rx = None    # UDP socket the lights reply to; opened in main() once we know we're the only copy running


def open_lan_socket():
    global rx
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", LISTEN_PORT))
    for ip in lan_ips():
        try:
            rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                          socket.inet_aton(MCAST_GROUP) + socket.inet_aton(ip))
        except OSError:
            pass


def scan():
    msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    for ip in lan_ips():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, 0))
            s.sendto(msg, (MCAST_GROUP, SCAN_PORT))
            s.sendto(msg, ("255.255.255.255", SCAN_PORT))
            s.close()
        except OSError as e:
            print(f"scan via {ip} failed: {e}", flush=True)


def lan_send(dev_id, cmd, data):
    with lock:
        ip = devices.get(dev_id, {}).get("ip")
    if ip:
        try:
            rx.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (ip, CMD_PORT))
        except OSError:          # network down / asleep: drop it, the next poll retries
            pass


def listen():
    while True:
        try:
            data, (src, _) = rx.recvfrom(65535)
            msg = json.loads(data)["msg"]
        except (OSError, ValueError, KeyError):
            continue
        cmd, d = msg.get("cmd"), msg.get("data") or {}
        now = time.time()
        with lock:
            if cmd == "scan" and d.get("device"):
                dev = devices.setdefault(d["device"], {"id": d["device"], "status": None})
                ip = d.get("ip") or src
                changed = dev.get("ip") != ip or dev.get("via") != "lan" or not dev.get("name")
                dev.update(sku=d.get("sku"), ip=ip, via="lan", online=True, last_seen=now)
                if not dev.get("name"):
                    dev["name"] = f"{dev['sku']} · {dev['id'][-5:].replace(':', '')}"
                    dev["auto_name"] = True
                if changed:
                    print(f"LAN: found {dev['sku']} at {ip}", flush=True)
                    save()
                    threading.Thread(target=lan_send, args=(dev["id"], "devStatus", {}), daemon=True).start()
            elif cmd == "devStatus":
                for dev in devices.values():
                    if dev.get("ip") == src:
                        dev.update(status=d, online=True, last_seen=now, fresh=True)
                        pending = dev.get("unconfirmed")
                        if isinstance(pending, list) and lan_matches(d, *pending):
                            dev["unconfirmed"] = False   # slow, not stuck: it took after all


# ---------- cloud ----------

def cloud(method, path, body=None):
    req = urllib.request.Request(
        CLOUD_API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Govee-API-Key": API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def cloud_discover():
    try:
        res = cloud("GET", "/user/devices")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"cloud: device list failed: {e}", flush=True)
        return
    if res.get("code") not in (None, 200):
        print(f"cloud: device list -> {res.get('code')} {res.get('msg')}", flush=True)
        return
    found = 0
    listed = {d.get("device") for d in res.get("data") or []}
    with lock:
        for gone in [i for i, d in devices.items() if d.get("via") == "cloud" and i not in listed]:
            print(f"cloud: {devices[gone].get('name')} is no longer on your Govee account, removing", flush=True)
            del devices[gone]
        for d in res.get("data") or []:
            if "light" not in (d.get("type") or ""):
                continue
            dev = devices.get(d["device"])
            app_name = d.get("deviceName")
            if dev and dev.get("via") == "lan":
                # Already controlled locally; just borrow the name from the Govee app.
                if dev.get("auto_name") and app_name:
                    dev.update(name=app_name, auto_name=False)
                continue
            dev = devices.setdefault(d["device"], {"id": d["device"], "status": None, "last_seen": 0})
            dev.update(sku=d["sku"], via="cloud", ip=None)
            if not dev.get("name") or dev.get("auto_name"):
                dev.update(name=app_name or f"{d['sku']} · {d['device'][-5:].replace(':', '')}", auto_name=False)
            for cap in d.get("capabilities") or []:
                if cap.get("instance") == "colorTemperatureK":
                    rng = (cap.get("parameters") or {}).get("range") or {}
                    if "min" in rng and "max" in rng:
                        dev["kelvin"] = [rng["min"], rng["max"]]
            found += 1
    print(f"cloud: {found} light(s) without LAN control", flush=True)
    save()


def cloud_status(dev_id):
    with lock:
        sku = devices.get(dev_id, {}).get("sku")
    try:
        res = cloud("POST", "/device/state", {"requestId": str(uuid.uuid4()),
                                               "payload": {"sku": sku, "device": dev_id}})
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"cloud: state for {dev_id} failed: {e}", flush=True)
        return
    if res.get("code") != 200:
        with lock:
            if dev_id in devices:
                devices[dev_id]["online"] = False
        print(f"cloud: state for {dev_id} -> {res.get('code')} {res.get('msg')}", flush=True)
        return
    caps = {c.get("instance"): (c.get("state") or {}).get("value")
            for c in (res.get("payload") or {}).get("capabilities") or []}
    rgb = caps.get("colorRgb") or 0
    kelvin = caps.get("colorTemperatureK") or 0
    status = {
        "onOff": 1 if caps.get("powerSwitch") else 0,
        "brightness": caps.get("brightness") or 100,
        "color": {"r": (rgb >> 16) & 255, "g": (rgb >> 8) & 255, "b": rgb & 255},
        "colorTemInKelvin": kelvin if (kelvin and not rgb) else 0,
    }
    with lock:
        dev = devices.get(dev_id)
        if dev:
            dev.update(status=status, online=caps.get("online") in (True, "true"), last_seen=time.time())


cloud_pending = {}            # (id, kind) -> capability; latest value wins so sliders don't flood the API
cloud_cv = threading.Condition()


def cloud_worker():
    while True:
        with cloud_cv:
            while not cloud_pending:
                cloud_cv.wait()
            key = next(iter(cloud_pending))
            cap = cloud_pending.pop(key)
        dev_id = key[0]
        with lock:
            sku = devices.get(dev_id, {}).get("sku")
        try:
            r = cloud("POST", "/device/control", {"requestId": str(uuid.uuid4()),
                                                   "payload": {"sku": sku, "device": dev_id, "capability": cap}})
            if r.get("code") != 200:
                print(f"cloud: control {cap['instance']} -> {r.get('code')} {r.get('msg')}", flush=True)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"cloud: control failed: {e}", flush=True)
        time.sleep(0.3)


def cloud_send(dev_id, kind, cap):
    with cloud_cv:
        cloud_pending[(dev_id, kind)] = cap
        cloud_cv.notify()


# ---------- background ----------

def background():
    last_scan = last_list = 0
    last_cloud_status = {}
    while True:
        try:
            last_scan, last_list = background_tick(last_scan, last_list, last_cloud_status)
        except Exception as e:                       # keep scanning/polling no matter what
            print(f"background: error {e}", flush=True)
        time.sleep(STATUS_EVERY)


def background_tick(last_scan, last_list, last_cloud_status):
    if True:
        now = time.time()
        if now - last_scan > SCAN_EVERY:
            scan()
            last_scan = now
        if API_KEY and now - last_list > CLOUD_LIST_EVERY:
            cloud_discover()
            last_list = now
        with lock:
            lan_ids = [i for i, d in devices.items() if d.get("via") == "lan"]
            cloud_ids = [i for i, d in devices.items() if d.get("via") == "cloud"]
            for i in lan_ids:
                if now - devices[i].get("last_seen", 0) > OFFLINE_AFTER and not streaming(i):
                    devices[i]["online"] = False
        for i in lan_ids:
            lan_send(i, "devStatus", {})
        if API_KEY:
            for i in cloud_ids:
                if now - last_cloud_status.get(i, 0) > CLOUD_STATUS_EVERY:
                    last_cloud_status[i] = now
                    threading.Thread(target=cloud_status, args=(i,), daemon=True).start()
    return last_scan, last_list



# ---------- effects engine ----------

def razer_packet(body):
    b = bytes(body)
    x = 0
    for v in b:
        x ^= v
    return base64.b64encode(b + bytes([x])).decode()


RAZER_ON = razer_packet([0xBB, 0x00, 0x01, 0xB1, 0x01])
RAZER_OFF = razer_packet([0xBB, 0x00, 0x01, 0xB1, 0x00])
FPS, BASIC_EVERY = 50, 5          # 50fps looked smoothest on the tree (tested 30-200); plain-color lights get 10/s


def segments_of(dev):
    return dev.get("segments") or STREAM_SEGMENTS.get(dev.get("sku"))


def ease(k):
    k = max(0.0, min(1.0, k))
    return k * k * (3 - 2 * k)


def k2rgb(k):
    k /= 100
    if k <= 66:
        r, g = 255, 99.47 * math.log(k) - 161.12
        b = 0 if k <= 19 else 138.52 * math.log(k - 10) - 305.04
    else:
        r, g, b = 329.7 * (k - 60) ** -0.1332, 288.12 * (k - 60) ** -0.0755, 255
    return tuple(max(0, min(255, int(round(v)))) for v in (r, g, b))


def to_rgb(col):
    """col is ("k", kelvin) or ("rgb", (r, g, b))."""
    return k2rgb(col[1]) if col[0] == "k" else tuple(col[1])


def status_color(st):
    if st.get("colorTemInKelvin"):
        return ("k", st["colorTemInKelvin"])
    c = st.get("color") or {}
    return ("rgb", (c.get("r", 255), c.get("g", 255), c.get("b", 255)))


def mixc(a, b, k):
    return tuple(int(round(a[j] + (b[j] - a[j]) * k)) for j in range(3))


def send_color(dev_id, col):
    if col[0] == "k":
        lan_send(dev_id, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": int(col[1])})
    else:
        lan_send(dev_id, "colorwc", {"color": dict(zip("rgb", col[1])), "colorTemInKelvin": 0})


def col_kind(col):
    return ("kelvin", int(col[1])) if col[0] == "k" else ("color", dict(zip("rgb", col[1])))


def confirm_later(dev_id, kind, value):
    with lock:
        seq = cmd_seq[(dev_id, kind)] = cmd_seq.get((dev_id, kind), 0) + 1
    threading.Thread(target=lan_confirm, args=(dev_id, kind, value, seq), daemon=True).start()


class Fader:
    """Software fades for LAN lights (the protocol has no transition time): brightness ramps and color/white
    crossfades, eased, ~20 steps a second, ending on the exact target value."""
    HZ = 20

    def __init__(self):
        self.jobs = {}            # (id, "bri"|"col") -> {frm, to, start, dur, done, last}
        self.lock = threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    @staticmethod
    def value(job, k):
        frm, to = job["frm"], job["to"]
        if isinstance(frm, (int, float)):
            return int(round(frm + (to - frm) * k))
        if frm[0] == "k" and to[0] == "k":
            return ("k", int(round(frm[1] + (to[1] - frm[1]) * k)))
        return ("rgb", mixc(to_rgb(frm), to_rgb(to), k))

    def current(self, dev_id, ch):
        with self.lock:
            job = self.jobs.get((dev_id, ch))
        return self.value(job, ease((time.time() - job["start"]) / job["dur"])) if job else None

    def start(self, dev_id, ch, frm, to, dur, done=None, tag=None):
        with self.lock:
            self.jobs[(dev_id, ch)] = dict(frm=frm, to=to, start=time.time(), dur=max(dur, 0.05), done=done, last=None, tag=tag)

    def info(self, dev_id):
        """A long power fade in progress ('on' / 'off'), for the UI: {tag, left (seconds)}."""
        with self.lock:
            job = self.jobs.get((dev_id, "bri"))
        if not job or not job.get("tag") or job["dur"] < 10:
            return None
        return {"tag": job["tag"], "left": max(0, int(job["start"] + job["dur"] - time.time()))}

    def cancel(self, dev_id, ch):
        with self.lock:
            self.jobs.pop((dev_id, ch), None)

    def send(self, dev_id, ch, v):
        if ch == "bri":
            lan_send(dev_id, "brightness", {"value": max(1, min(100, v))})
        else:
            send_color(dev_id, v)

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:                   # a fade glitch must not stop all future fades
                print(f"fader: error {e}", flush=True)
            time.sleep(1 / self.HZ)

    def tick(self):
        if True:
            now = time.time()
            with self.lock:
                items = list(self.jobs.items())
            for key, job in items:
                k = (now - job["start"]) / job["dur"]
                if k >= 1:
                    with self.lock:
                        if self.jobs.get(key) is not job:
                            continue
                        del self.jobs[key]
                    self.send(key[0], key[1], job["to"])
                    if job["done"]:
                        threading.Thread(target=job["done"], daemon=True).start()
                    continue
                v = self.value(job, ease(k))
                if v != job["last"]:
                    job["last"] = v
                    self.send(key[0], key[1], v)


fader = None


class Engine:
    def __init__(self):
        self.runs = {}            # id -> run dict (see start)
        self.lock = threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    def start(self, ids, fx, speed=1.0, fade=0.0):
        """Run one effect across these lights, laid out in order as one long strip. With fade, each light
        blends in from what it was showing (or from the previous effect)."""
        if fx not in EFFECTS and fx != "screen":
            return False
        with lock:
            devs = [devices[i] for i in ids if i in devices and devices[i].get("via") == "lan"
                    and not (len(ids) > 1 and devices[i].get("govee_modes") and fx != "screen")]
        self.quiet_stop([i for i in ids if i not in {d["id"] for d in devs}])
        layout, total = [], 0
        for d in devs:
            n = segments_of(d) or 1
            layout.append((d, n, total))
            total += n
        now = time.time()
        with self.lock:
            for d, n, off in layout:
                i = d["id"]
                old = self.runs.get(i)
                st = dict(d.get("status") or {})
                if old and old.get("last_cols") and len(old["last_cols"]) == n:
                    frm = old["last_cols"]
                else:
                    frm = [to_rgb(fader.current(i, "col") or status_color(st))] * n
                fader.cancel(i, "col")
                self.runs[i] = dict(
                    fx=fx, speed=speed, offset=off, total=max(total, 1), start=now, n=n, stream=bool(segments_of(d)),
                    restore=old["restore"] if old else st, sent_on=old["sent_on"] if old else 0,
                    last=None, last_cols=None, fade_in=(now, fade, frm) if fade > 0 else None, fade_out=None)
        return bool(layout)

    def _finish(self, dev_id, run, col):
        """Hand a light from the effect to a plain color: set the color while still streaming, then end the
        stream (tested on the tree: no flash)."""
        if col:
            send_color(dev_id, col)
            confirm_later(dev_id, *col_kind(col))
        if run["stream"]:
            time.sleep(0.15)
            for _ in range(2):
                lan_send(dev_id, "razer", {"pt": RAZER_OFF})
                time.sleep(0.05)

    def release(self, dev_id, col, fade=0.0):
        """Leave the effect for color `col`, fading over `fade` seconds. False if no effect was running."""
        with self.lock:
            run = self.runs.get(dev_id)
            if not run:
                return False
            if fade > 0:
                run["fade_out"] = (time.time(), fade, to_rgb(col), col)
                return True
            del self.runs[dev_id]
        threading.Thread(target=self._finish, args=(dev_id, run, col), daemon=True).start()
        return True

    def restore(self, ids, fade=0.0):
        """Stop effects and return each light to the color it had before the effect."""
        for i in ids:
            with self.lock:
                run = self.runs.get(i)
            if run:
                if run["restore"]:
                    self.release(i, status_color(run["restore"]), fade)
                else:
                    self.quiet_stop([i])

    def quiet_stop(self, ids):
        """The light is being turned off: drop the effect without restoring anything, end streaming once dark."""
        with self.lock:
            stopped = [(i, self.runs.pop(i)) for i in ids if i in self.runs]
        for i, run in stopped:
            if run["stream"]:
                for delay in (0.4, 0.6):
                    threading.Timer(delay, lan_send, args=(i, "razer", {"pt": RAZER_OFF})).start()

    def active(self, dev_id):
        with self.lock:
            r = self.runs.get(dev_id)
            return r["fx"] if r else None

    def reload_effects(self):
        """Pick up edits to effects.py while running, so effects can be tuned live."""
        global EFFECTS, EFFECT_SCENES
        try:
            mtime = os.path.getmtime(effects_mod.__file__)
            if mtime != getattr(self, "fx_mtime", mtime):
                importlib.reload(effects_mod)
                EFFECTS, EFFECT_SCENES = effects_mod.EFFECTS, effects_mod.EFFECT_SCENES
                print("effects.py reloaded", flush=True)
            self.fx_mtime = mtime
        except Exception as e:                    # a typo in effects.py shouldn't take the lights down
            print(f"effects.py reload failed, keeping the old version: {e}", flush=True)
            self.fx_mtime = mtime

    def loop(self):
        tick = 0
        next_t = time.perf_counter()
        while True:
            next_t += 1 / FPS
            tick += 1
            if tick % FPS == 0:
                self.reload_effects()
            now = time.time()
            with self.lock:
                runs = list(self.runs.items())
            if not runs:                                    # nothing playing: idle instead of ticking at 50/s
                time.sleep(0.1)
                next_t = time.perf_counter()
                continue
            for dev_id, r in runs:
                try:
                    if r["fx"] == "screen":
                        sync.touch()
                        dev = devices.get(dev_id) or {}
                        zone = dev.get("screen_zone") or DEFAULT_ZONES.get(dev.get("sku"), "all")
                        cols = sync.colors(dev_id, r["n"], zone)
                    else:
                        fn = EFFECTS[r["fx"]][1]
                        t = now - r["start"]
                        cols = [fn(t, (r["offset"] + k) / r["total"], r["offset"] + k, r["speed"]) for k in range(r["n"])]
                    if r["fade_in"]:
                        t0, dur, frm = r["fade_in"]
                        k = (now - t0) / dur
                        if k >= 1:
                            r["fade_in"] = None
                        else:
                            cols = [mixc(frm[j], c, ease(k)) for j, c in enumerate(cols)]
                    if r["fade_out"]:
                        t0, dur, to, col = r["fade_out"]
                        k = (now - t0) / dur
                        if k >= 1:
                            with self.lock:
                                if self.runs.get(dev_id) is r:
                                    del self.runs[dev_id]
                            threading.Thread(target=self._finish, args=(dev_id, r, col), daemon=True).start()
                            continue
                        cols = [mixc(c, to, ease(k)) for c in cols]
                    r["last_cols"] = cols
                    if r["stream"]:
                        if not r["sent_on"]:                    # enable once: re-sending it blanks the light for a frame
                            lan_send(dev_id, "razer", {"pt": RAZER_ON})
                            r["sent_on"] = now
                        body = [0xBB, 0x00, 3 * len(cols) + 2, 0xB0, 0, len(cols)]
                        for c in cols:
                            body += list(c)
                        lan_send(dev_id, "razer", {"pt": razer_packet(body)})
                    elif tick % BASIC_EVERY == 0 and cols[0] != r["last"]:
                        r["last"] = cols[0]
                        lan_send(dev_id, "colorwc", {"color": dict(zip("rgb", cols[0])), "colorTemInKelvin": 0})
                except Exception as e:                          # never let one light kill the loop
                    print(f"effect error on {dev_id}: {e}", flush=True)
            time.sleep(max(0, next_t - time.perf_counter()))
            if time.perf_counter() - next_t > 0.5:            # fell way behind (sleep/suspend): resync
                next_t = time.perf_counter()


engine = None


# ---------- built-in Govee scenes (cloud) ----------

scene_cache = {}   # sku -> {name: (type, instance, value)}


def scenes_for(dev_id):
    with lock:
        dev = devices.get(dev_id)
        sku = dev.get("sku") if dev else None
    if not sku or not API_KEY:
        return {}
    if sku not in scene_cache:
        try:
            res = cloud("POST", "/device/scenes", {"requestId": str(uuid.uuid4()),
                                                    "payload": {"sku": sku, "device": dev_id}})
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"cloud: scenes for {sku} failed: {e}", flush=True)
            return {}
        table = {}
        for cap in (res.get("payload") or {}).get("capabilities") or []:
            for o in (cap.get("parameters") or {}).get("options") or []:
                table[o["name"].replace("\u00a0", " ")] = (cap["type"], cap["instance"], o["value"])
        scene_cache[sku] = table
    return scene_cache[sku]


def scene_for_effect(dev_id, fx):
    table = {k.lower(): k for k in scenes_for(dev_id)}
    for want in EFFECT_SCENES.get(fx, []):
        if want.lower() in table:
            return table[want.lower()]
    return None


def govee_mode_effect(ids, fx):
    """Govee-modes lights in a group effect run the closest built-in scene."""
    for i in ids:
        with lock:
            dev = devices.get(i)
            if not dev or not dev.get("govee_modes"):
                continue
            if "scene_restore" not in dev:
                dev["scene_restore"] = dict(dev.get("status") or {})
        name = scene_for_effect(i, fx)
        if name:
            apply(i, {"action": "scene", "name": name})
        else:
            with lock:
                dev.pop("scene_restore", None)


def govee_mode_stop(ids):
    for i in ids:
        with lock:
            dev = devices.get(i)
            st = dev.pop("scene_restore", None) if dev else None
            if st is not None:
                dev["scene"] = None
        if st:
            if st.get("colorTemInKelvin"):
                lan_command(i, "kelvin", st["colorTemInKelvin"])
            elif st.get("color"):
                lan_command(i, "color", st["color"])


# ---------- actions ----------

def lan_command(dev_id, kind, value):
    if kind == "power":
        lan_send(dev_id, "turn", {"value": value})
    elif kind == "brightness":
        lan_send(dev_id, "brightness", {"value": value})
    # Lights ignore a color/white equal to the one they already hold, and if a Govee scene is running
    # they then stay in the scene. So send a barely-different value first, then the real one.
    elif kind == "color":
        nudge = dict(value, r=value["r"] + 1 if value["r"] < 255 else 254)
        lan_send(dev_id, "colorwc", {"color": nudge, "colorTemInKelvin": 0})
        threading.Timer(0.15, lan_send, args=(dev_id, "colorwc", {"color": value, "colorTemInKelvin": 0})).start()
    elif kind == "kelvin":
        nudge = value - 100 if value - 100 >= 2000 else value + 100
        lan_send(dev_id, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": nudge})
        threading.Timer(0.15, lan_send,
                        args=(dev_id, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": value})).start()


def lan_matches(st, kind, value):
    if kind == "power":
        return st.get("onOff") == value
    if kind == "brightness":
        return abs((st.get("brightness") or 0) - value) <= 1
    if kind == "color":
        c = st.get("color") or {}
        return not st.get("colorTemInKelvin") and all(abs((c.get(k) or 0) - value[k]) <= 2 for k in "rgb")
    if kind == "kelvin":
        return abs((st.get("colorTemInKelvin") or 0) - value) <= 100
    return True


cmd_seq = {}   # (id, kind) -> latest sequence number, so a newer command cancels an older one's retries


def streaming(dev_id):
    """Some lights (the H6046 bars) stop answering status requests while streaming, so they can't be checked."""
    return bool(engine and engine.active(dev_id) and segments_of(devices.get(dev_id, {})))


def lan_confirm(dev_id, kind, value, seq, tries=3):
    for attempt in range(1, tries + 1):
        time.sleep(1.0 if attempt == 1 else 0.6)          # bulbs settle a little after a change
        with lock:
            dev = devices.get(dev_id)
            if not dev or cmd_seq.get((dev_id, kind)) != seq:
                return                                  # superseded by a newer command
        if streaming(dev_id):
            with lock:
                dev["unconfirmed"] = False
            return
            dev["fresh"] = False
        lan_send(dev_id, "devStatus", {})
        deadline = time.time() + 0.8
        while time.time() < deadline:
            time.sleep(0.05)
            with lock:
                if dev.get("fresh"):
                    break
        with lock:
            if cmd_seq.get((dev_id, kind)) != seq:
                return
            if dev.get("fresh") and lan_matches(dev.get("status") or {}, kind, value):
                if attempt > 1:
                    print(f"LAN: {dev.get('name')} {kind} confirmed after {attempt} tries", flush=True)
                dev["unconfirmed"] = False
                return
            name = dev.get("name")
        if attempt < tries:
            print(f"LAN: {name} didn't take {kind}={value}, resending", flush=True)
            lan_command(dev_id, kind, value)
    with lock:
        dev["unconfirmed"] = [kind, value]       # cleared later if a status report shows it did take
    print(f"LAN: {name} never confirmed {kind}={value}", flush=True)


def command(dev_id, via, kind, value):
    if via == "lan":
        lan_command(dev_id, kind, value)
        with lock:
            seq = cmd_seq[(dev_id, kind)] = cmd_seq.get((dev_id, kind), 0) + 1
        threading.Thread(target=lan_confirm, args=(dev_id, kind, value, seq), daemon=True).start()
    elif via == "cloud" and API_KEY:
        if kind == "power":
            cap = {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": value}
        elif kind == "brightness":
            cap = {"type": "devices.capabilities.range", "instance": "brightness", "value": value}
        elif kind == "color":
            cap = {"type": "devices.capabilities.color_setting", "instance": "colorRgb",
                   "value": (value["r"] << 16) | (value["g"] << 8) | value["b"]}
            kind = "colorish"   # color and kelvin share a slot: only the latest one matters
        else:
            cap = {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK", "value": value}
            kind = "colorish"
        cloud_send(dev_id, kind, cap)


def apply(dev_id, body):
    a = body.get("action")
    fade = max(0.0, min(10.0, float(body.get("fade") or 0)))
    if a == "scene":
        scene = scenes_for(dev_id).get(body.get("name"))
        if not scene:
            return False
        engine.quiet_stop([dev_id])
        fader.cancel(dev_id, "col")
        cloud_send(dev_id, "colorish", {"type": scene[0], "instance": scene[1], "value": scene[2]})
        with lock:
            devices[dev_id]["scene"] = body.get("name")
        return True
    with lock:
        dev = devices.get(dev_id)
        if not dev:
            return False
        if a == "rename":
            dev["name"] = str(body.get("name", "")).strip()[:60] or dev["name"]
            dev["auto_name"] = False
            save()
            return True
        if a == "govee_modes":
            dev["govee_modes"] = bool(body.get("on"))
            save()
            if dev["govee_modes"]:
                threading.Thread(target=engine.restore, args=([dev_id],), daemon=True).start()
            return True
        if a == "screen_zone":
            dev["screen_zone"] = body.get("zone") if body.get("zone") in ZONE_NAMES else None
            save()
            return True
        if a == "position":                              # where the light sits on its room's map (0..1, 0..1)
            try:
                dev["pos"] = [round(min(1.0, max(0.0, float(body["x"]))), 4), round(min(1.0, max(0.0, float(body["y"]))), 4)]
            except (KeyError, TypeError, ValueError):
                dev["pos"] = None
            save()
            return True
        if a == "room":
            dev["room"] = str(body.get("room", "")).strip()[:40] or None
            save()
            return True
        if a not in ("power", "brightness", "color", "kelvin"):
            return False
        via = dev.get("via", "lan")
        st = dev.get("status") or {}
        dev["status"] = st
        prev = dict(st)
        if a != "brightness":
            dev["scene"] = None
            dev.pop("scene_restore", None)
        if a == "power":
            on = bool(body.get("on"))
            was_on = bool(prev.get("onOff", 1))
            st["onOff"] = int(on)
            if not on and was_on:      # remember the brightness to come back to (the fade-out ends at 1%)
                dev["bri_restore"] = int(fader.current(dev_id, "bri") or prev.get("brightness") or 100)
            restore_bri = dev.pop("bri_restore", None) if on else None
            save()
        elif a == "brightness":
            value = max(1, min(100, int(body["value"])))
            st["brightness"] = value
            dev.pop("bri_restore", None)
        elif a == "color":
            value = {k: max(0, min(255, int(body[k]))) for k in ("r", "g", "b")}
            col = ("rgb", (value["r"], value["g"], value["b"]))
            st.update(color=value, colorTemInKelvin=0, onOff=1)
        else:
            lo, hi = dev.get("kelvin") or [2000, 9000]
            value = max(lo, min(hi, int(body["value"])))
            col = ("k", value)
            st.update(color={"r": 0, "g": 0, "b": 0}, colorTemInKelvin=value, onOff=1)

    if via != "lan":                    # cloud lights: no fades, no effects
        kind = "power" if a == "power" else a
        command(dev_id, via, kind, (1 if on else 0) if a == "power" else value)
        return True

    if a == "power":
        if on:
            fader.cancel(dev_id, "bri")                  # e.g. cancel a fade-to-off still in progress
            target = restore_bri or prev.get("brightness") or 100
            if fade and not was_on:
                lan_send(dev_id, "brightness", {"value": 1})
                lan_send(dev_id, "turn", {"value": 1})
                fader.start(dev_id, "bri", 1, target, fade, tag="on",
                            done=lambda: (confirm_later(dev_id, "power", 1), confirm_later(dev_id, "brightness", target)))
                with lock:
                    st["brightness"] = target
            else:
                command(dev_id, "lan", "power", 1)
                if restore_bri:
                    command(dev_id, "lan", "brightness", restore_bri)
                    with lock:
                        st["brightness"] = restore_bri
        else:
            def off():
                command(dev_id, "lan", "power", 0)
                engine.quiet_stop([dev_id])
            if fade and was_on:
                cur = fader.current(dev_id, "bri") or prev.get("brightness") or 100
                fader.start(dev_id, "bri", cur, 1, fade, done=off, tag="off")
            else:
                fader.cancel(dev_id, "bri")
                off()
        return True

    if a == "brightness":              # brightness never interrupts an effect
        if fade:
            cur = fader.current(dev_id, "bri") or prev.get("brightness") or value
            fader.start(dev_id, "bri", cur, value, fade, done=lambda: confirm_later(dev_id, "brightness", value))
        else:
            fader.cancel(dev_id, "bri")
            command(dev_id, "lan", "brightness", value)
        return True

    # color / white
    if engine.release(dev_id, col, fade):              # leaving an effect: blend out of it into the color
        return True
    frm = fader.current(dev_id, "col") or status_color(prev)
    if fade and frm != col and prev.get("onOff", 1):
        fader.start(dev_id, "col", frm, col, fade, done=lambda: confirm_later(dev_id, *col_kind(col)))
    else:
        fader.cancel(dev_id, "col")
        command(dev_id, "lan", a, value)                # includes the nudge for same-value / scene exits
    return True


def snapshot():
    with lock:
        out = []
        for d in devices.values():
            st = dict(d.get("status") or {})
            bri = fader.current(d["id"], "bri") if fader else None
            if bri is not None:                          # mid-fade: report where the brightness is now
                st["brightness"] = max(1, min(100, int(bri)))
            out.append({**d, "status": st, "effect": engine.active(d["id"]), "stream": bool(segments_of(d)),
                        "zone": d.get("screen_zone") or DEFAULT_ZONES.get(d.get("sku"), "all"),
                        "fading": fader.info(d["id"]) if fader else None})
        return sorted(out, key=lambda d: (not d.get("online"), (d.get("name") or "").lower()))


# ---------- http ----------

def resolve(target):
    """'all', 'room:<name>' or a device id -> list of device ids (lights in a group sorted by name)."""
    if target == "all" or target.startswith("room:"):
        room = target[5:] if target != "all" else None
        with lock:
            return sorted((i for i, d in devices.items()
                           if d.get("online") and (room is None or (d.get("room") or "") == room)),
                          key=lambda i: (devices[i].get("name") or "").lower())
    return [target]


def run_action(ids, body):
    a = body.get("action")
    if a == "effect":
        fx = body.get("effect")
        ok = engine.start(ids, fx, float(body.get("speed") or 1), float(body.get("fade") or 0))
        if len(ids) > 1 and fx in EFFECTS:
            threading.Thread(target=govee_mode_effect, args=(ids, fx), daemon=True).start()
            ok = True
        return ok
    if a == "stop":
        engine.restore(ids, float(body.get("fade") or 0))
        govee_mode_stop(ids)
        return True
    return all([apply(i, body) for i in ids]) if ids else False


def run_step(step, fade=None):
    """One mode step. fade: the mode's / schedule entry's fade (seconds); a step's own fade wins."""
    with lock:
        ids = [i for i in step["ids"] if i in devices] if "ids" in step else None
    body = dict(step["body"])
    body.setdefault("fade", fade or MODE_FADE)          # modes and the schedule always crossfade
    run_action(ids if ids is not None else resolve(step.get("target", "all")), body)


def capture(scope):
    """Snapshot what the lights in scope are doing right now as mode steps."""
    with lock:
        ids = [i for i in resolve(scope)]
        snap = {i: dict(devices[i]) for i in ids if i in devices}
    steps, by_fx = [], {}
    for i, d in snap.items():
        st = d.get("status") or {}
        if not st:
            continue
        if not st.get("onOff"):
            steps.append({"ids": [i], "body": {"action": "power", "on": False}})
            continue
        steps.append({"ids": [i], "body": {"action": "power", "on": True}})
        steps.append({"ids": [i], "body": {"action": "brightness", "value": st.get("brightness") or 100}})
        fx = engine.active(i)
        if fx:
            by_fx.setdefault(fx, []).append(i)
        elif d.get("scene"):
            steps.append({"ids": [i], "body": {"action": "scene", "name": d["scene"]}})
        elif st.get("colorTemInKelvin"):
            steps.append({"ids": [i], "body": {"action": "kelvin", "value": st["colorTemInKelvin"]}})
        elif st.get("color"):
            steps.append({"ids": [i], "body": {"action": "color", **st["color"]}})
    for fx, group in by_fx.items():                      # effects last, as one group so they spread across lights
        steps.append({"ids": group, "body": {"action": "effect", "effect": fx}})
    return steps


modes = None
MODE_FADE = 1.5
# On the PC, sample the screen directly; anywhere else (the Pi), take frames from the PC's screen_agent.py
sync = ScreenSync(monitor=0, remote=os.name != "nt" or bool(os.environ.get("GOVEE_REMOTE_SCREEN")))


VOLATILE = ("last_seen", "fresh")     # change on every poll; left out of pushed state so it only goes out on real changes


def live_state():
    return {"devices": [{k: v for k, v in d.items() if k not in VOLATILE} for d in snapshot()],
            "modes": modes.status()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _events(self):
        """Server-sent events: the whole state whenever it changes, so every open page is live without polling."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last, beat = None, 0.0
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while True:
                data = json.dumps(live_state(), separators=(",", ":"))
                now = time.time()
                if data != last:
                    self.wfile.write(b"data: " + data.encode() + b"\n\n")
                    last, beat = data, now
                elif now - beat > 15:                       # lets the page notice a dead connection
                    self.wfile.write(b"event: ping\ndata: 1\n\n")
                    beat = now
                self.wfile.flush()
                time.sleep(0.2)
        except (OSError, ValueError):                       # page closed
            return

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if url.path == "/api/events":
            return self._events()
        if url.path == "/api/state":
            return self._json(live_state())
        if url.path == "/api/modes":
            return self._json(modes.status())
        if url.path == "/api/sync/want":                    # the PC's screen agent asks: should I send frames?
            return self._json({"want": sync.wanted()})
        # Stream Deck / one-click URLs (GET so any "open URL in background" button can use them)
        if url.path == "/api/mode/cycle":
            order = q.get("modes", [""])[0].split(",") if q.get("modes") else None
            nxt = modes.cycle(order)
            return self._json({"mode": nxt, **modes.status()})
        if url.path.startswith("/api/mode/toggle/"):
            back = modes.toggle(unquote(url.path[len("/api/mode/toggle/"):]))
            return self._json({"now": modes.status()["active"], **modes.status()})
        if url.path == "/api/mode/resume":
            modes.resume()
            return self._json(modes.status())
        if url.path.startswith("/api/mode/"):
            ok = modes.activate(unquote(url.path[len("/api/mode/"):]))
            return self._json(modes.status(), 200 if ok else 404)
        if url.path in PHONE_FILES:                         # phone app (installable web app)
            name, ctype = PHONE_FILES[url.path]
            body = (ROOT / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/devices":
            self._json(snapshot())
        elif self.path == "/api/effects":
            self._json([{"id": k, "name": v[0]} for k, v in EFFECTS.items()] + [{"id": "screen", "name": "Screen sync"}])
        elif self.path == "/api/zones":
            self._json([{"id": k, "name": v} for k, v in ZONE_NAMES.items()])
        elif self.path.startswith("/api/scenes/"):
            self._json(sorted(scenes_for(unquote(self.path[len("/api/scenes/"):]))))
        elif self.path == "/api/info":
            self._json({"cloud": bool(API_KEY), "interfaces": lan_ips()})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/api/sync/frame"):          # raw BGRX grid from the PC's screen agent
            q = parse_qs(urlsplit(self.path).query)
            w, h = int(q.get("w", ["0"])[0]), int(q.get("h", ["0"])[0])
            sw, sh = int(q.get("sw", ["0"])[0]), int(q.get("sh", ["0"])[0])
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if w > 0 and h > 0 and len(raw) == w * h * 4 and w * h <= 4096:
                sync.feed([[(raw[(y * w + x) * 4 + 2], raw[(y * w + x) * 4 + 1], raw[(y * w + x) * 4])
                            for x in range(w)] for y in range(h)], sw, sh)
            return self._json({"want": sync.wanted()})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/scan":
            scan()
            if API_KEY:
                threading.Thread(target=cloud_discover, daemon=True).start()
            time.sleep(1.5)
            return self._json(snapshot())
        if self.path.startswith("/api/devices/"):
            ok = run_action(resolve(unquote(self.path[len("/api/devices/"):])), body)
            return self._json({"ok": ok}, 200 if ok else 400)
        if self.path == "/api/schedule":
            modes.set_schedule(body.get("enabled"), body.get("entries"))
            return self._json(modes.status())
        if self.path == "/api/modes":                       # new mode from the current lights
            mid = modes.create(body.get("name", ""), body.get("scope") or "all")
            return self._json({"id": mid, **modes.status()}, 200 if mid else 400)
        if self.path.startswith("/api/modes/"):
            mid, _, op = unquote(self.path[len("/api/modes/"):]).partition("/")
            if op == "activate":
                ok = modes.activate(mid)
            elif op == "capture":
                ok = modes.capture_into(mid, body.get("scope") or "all")
            elif op == "delete":
                modes.delete(mid)
                ok = True
            elif op == "fade":
                ok = modes.set_fade(mid, body.get("fade"))
            else:
                ok = False
            return self._json(modes.status(), 200 if ok else 400)
        self._json({"error": "not found"}, 404)


PHONE_FILES = {      # url -> (file, content type); an explicit list, so nothing else in the folder is served
    "/m": ("index.html", "text/html; charset=utf-8"),      # same app; /m is the installed app's start URL
    "/manifest.webmanifest": ("static/manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("static/sw.js", "text/javascript"),
    "/static/icon-192.png": ("static/icon-192.png", "image/png"),
    "/static/icon-512.png": ("static/icon-512.png", "image/png"),
    "/static/icon-maskable-512.png": ("static/icon-maskable-512.png", "image/png"),
}


class Server(ThreadingHTTPServer):
    """HTTP server that refuses to share its port.
    Windows: SO_REUSEADDR would let a second copy bind the same port, so use SO_EXCLUSIVEADDRUSE instead.
    Linux: SO_REUSEADDR never allows two listeners, and it's needed to restart while old connections sit in
    TIME_WAIT (without it a quick restart fails with "address already in use")."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class RotatingLog:
    """Line-buffered log file that rolls over to .old.log at ~1 MB, checked as it writes."""
    LIMIT = 1_000_000

    def __init__(self, path):
        self.path = Path(path)
        self.f = None
        self.open()

    def open(self):
        if self.f:
            self.f.close()
        if self.path.exists() and self.path.stat().st_size > self.LIMIT:
            self.path.replace(self.path.with_suffix(".old.log"))
        self.f = open(self.path, "a", buffering=1, encoding="utf-8")

    def write(self, text):
        self.f.write(text)
        if self.f.tell() > self.LIMIT:
            self.open()
        return len(text)

    def flush(self):
        self.f.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--log", help="write output to this file (for running hidden with pythonw)")
    args = p.parse_args()
    if args.log:
        sys.stdout = sys.stderr = RotatingLog(args.log)
        print(f"===== started {time.strftime('%Y-%m-%d %H:%M:%S')} =====", flush=True)
    try:                                   # claim the port first: if another copy runs, leave it alone
        httpd = Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"another copy is already running on port {args.port} ({e}); exiting", flush=True)
        sys.exit(3)
    open_lan_socket()
    global engine, modes, fader
    load()
    fader = Fader()
    engine = Engine()
    modes = Modes(ROOT / "modes.json", run_step, capture, read_json, write_json)
    threading.Thread(target=listen, daemon=True).start()
    threading.Thread(target=cloud_worker, daemon=True).start()
    threading.Thread(target=background, daemon=True).start()
    # effects only exist while the server runs: if the active mode has one, pick it back up once lights answer
    threading.Timer(5, modes.resume_effects).start()
    print(f"LAN scan via {', '.join(lan_ips()) or 'no LAN interface found'}", flush=True)
    print(f"cloud API: {'on' if API_KEY else 'off (add GOVEE_API_KEY to .env)'}", flush=True)
    print(f"open http://localhost:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
