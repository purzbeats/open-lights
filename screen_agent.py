"""Screen agent for the PC: feeds Watching mode on the Pi.

Sits idle, asking the lights server once a second whether any light is screen-syncing. Only while it is, samples
the screen (64x36 pixels, ~30 times a second) and sends each frame to the server, which does all the color work.

    pythonw screen_agent.py --server https://your-pi.your-tailnet.ts.net --log screen_agent.log
"""
import argparse
import http.client
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))   # -I doesn't add the script's folder
from screen import Sampler  # noqa: E402

GRID, HZ = (64, 36), 30


class Link:
    """One kept-alive HTTP(S) connection to the server, reopened on any error."""

    def __init__(self, base):
        u = urlsplit(base)
        self.https, self.host, self.port = u.scheme == "https", u.hostname, u.port
        self.conn = None

    def request(self, method, path, body=None, timeout=3):
        for attempt in (1, 2):
            try:
                if self.conn is None:
                    cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
                    self.conn = cls(self.host, self.port, timeout=timeout)
                self.conn.request(method, path, body=body,
                                  headers={"Content-Type": "application/octet-stream"} if body else {})
                r = self.conn.getresponse()
                return json.loads(r.read() or b"{}")
            except (OSError, http.client.HTTPException, ValueError):
                self.close()
                if attempt == 2:
                    raise

    def close(self):
        if self.conn:
            self.conn.close()
        self.conn = None


def stream(link, monitor):
    """Send frames until the server stops wanting them."""
    sampler = Sampler(monitor, *GRID, average=False)
    print(f"{time.strftime('%H:%M:%S')} streaming {sampler.w}x{sampler.h} -> {GRID[0]}x{GRID[1]} at {HZ}/s", flush=True)
    frames, t_start = 0, time.perf_counter()
    try:
        while True:
            t0 = time.perf_counter()
            r = link.request("POST", f"/api/sync/frame?w={GRID[0]}&h={GRID[1]}&sw={sampler.w}&sh={sampler.h}", sampler.grab_raw())
            frames += 1
            if not r.get("want"):
                break
            time.sleep(max(0.0, 1 / HZ - (time.perf_counter() - t0)))
    finally:
        sampler.close()
        secs = time.perf_counter() - t_start
        print(f"{time.strftime('%H:%M:%S')} stopped after {frames} frames ({frames / max(secs, 0.001):.0f}/s)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("GOVEE_SERVER", "http://127.0.0.1:8420"))
    ap.add_argument("--monitor", type=int, default=0, help="0 = primary monitor")
    ap.add_argument("--log", help="write output to this file (when running hidden with pythonw)")
    args = ap.parse_args()
    if args.log:
        log = Path(args.log)
        if log.exists() and log.stat().st_size > 500_000:
            log.replace(log.with_suffix(".old.log"))
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
    print(f"===== screen agent started {time.strftime('%Y-%m-%d %H:%M:%S')}, server {args.server} =====", flush=True)
    link, down_since = Link(args.server), None
    while True:
        try:
            if link.request("GET", "/api/sync/want").get("want"):
                stream(link, args.monitor)
            if down_since:
                print(f"{time.strftime('%H:%M:%S')} server reachable again", flush=True)
                down_since = None
            time.sleep(1)
        except Exception as e:                       # server down, PC offline, display asleep...: wait and retry
            if not down_since:
                print(f"{time.strftime('%H:%M:%S')} can't reach server ({e}); retrying quietly", flush=True)
                down_since = time.time()
            time.sleep(5)


if __name__ == "__main__":
    main()
