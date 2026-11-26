"""Modes (saved lighting setups), a weekly schedule, and manual overrides.

A mode is a list of steps; each step is {"target": "room:Office" | "all"} or {"ids": [...]} plus a "body"
that goes through the same action handler as the web UI.

Schedule entries are {"days": [0..6 Mon=0], "time": "HH:MM", "mode": id}. The latest entry at or before
now decides the scheduled mode. Activating a mode by hand overrides the schedule until its next change.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta

DEFAULT_MODES = [
    {"id": "office", "name": "Office Lighting", "steps": [
        {"target": "room:Office", "body": {"action": "power", "on": True}},
        {"target": "room:Office", "body": {"action": "brightness", "value": 100}},
        {"target": "room:Office", "body": {"action": "kelvin", "value": 4000}},
    ]},
    {"id": "chill", "name": "Chill Mode", "steps": [
        {"target": "room:Office", "body": {"action": "power", "on": True}},
        {"target": "room:Office", "body": {"action": "brightness", "value": 45}},
        {"target": "room:Office", "body": {"action": "effect", "effect": "fire"}},
    ]},
    {"id": "off", "name": "Off", "steps": [
        {"target": "room:Office", "body": {"action": "power", "on": False}},
    ]},
]
DEFAULT_CYCLE = ["office", "chill", "off"]


def _copy(x):
    return json.loads(json.dumps(x))


class Modes:
    def __init__(self, path, run_step, capture, read_json, write_json):
        self.path, self.run_step, self.capture = path, run_step, capture
        self._write = write_json
        self.lock = threading.RLock()
        self.data = read_json(path, {})        # crash-safe read (unreadable file is set aside)
        self.data.setdefault("modes", _copy(DEFAULT_MODES))
        self.data.setdefault("schedule", {"enabled": False, "entries": []})
        self.data.setdefault("state", {"active": None, "override": None, "last_key": None})
        self.save()
        threading.Thread(target=self.loop, daemon=True).start()

    # ---------- storage ----------

    def save(self):
        with self.lock:
            self._write(self.path, self.data)      # crash-safe: temp file + atomic rename

    def mode(self, mid):
        return next((m for m in self.data["modes"] if m["id"] == mid), None)

    # ---------- applying ----------

    def apply(self, mid, reason):
        m = self.mode(mid)
        if not m:
            return False
        print(f"mode: {m['name']} ({reason})", flush=True)
        with self.lock:
            self.data["state"]["active"] = mid
            self.save()
        for step in m["steps"]:
            try:
                self.run_step(step)
            except Exception as e:                       # one bad step shouldn't abort the rest
                print(f"mode: step {step} failed: {e}", flush=True)
        return True

    # ---------- schedule math ----------

    def _entries(self):
        out = []
        for e in self.data["schedule"]["entries"]:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(e.get("time", "")))
            if m and self.mode(e.get("mode")):
                out.append((e, int(m[1]), int(m[2])))
        return out

    def scheduled_at(self, now):
        """(datetime, entry) of the latest schedule entry at or before now, or None."""
        entries = self._entries()
        for back in range(8):
            day = now - timedelta(days=back)
            best = None
            for e, hh, mm in entries:
                if day.weekday() not in e.get("days", []):
                    continue
                at = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if at <= now and (best is None or at > best[0]):
                    best = (at, e)
            if best:
                return best
        return None

    def next_change(self, now):
        entries = self._entries()
        for ahead in range(8):
            day = now + timedelta(days=ahead)
            best = None
            for e, hh, mm in entries:
                if day.weekday() not in e.get("days", []):
                    continue
                at = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if at > now and (best is None or at < best[0]):
                    best = (at, e)
            if best:
                return best
        return None

    @staticmethod
    def _key(cur):
        return f"{cur[0].isoformat()}|{cur[1]['mode']}" if cur else None

    # ---------- the clock ----------

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"schedule: error {e}", flush=True)
            time.sleep(15)

    def tick(self):
        now = datetime.now()
        with self.lock:
            st, sch = self.data["state"], self.data["schedule"]
            ov = st.get("override")
            if ov and ov.get("until") and now >= datetime.fromisoformat(ov["until"]):
                print(f"schedule: override ({ov['mode']}) ended", flush=True)
                st["override"] = ov = None
                self.save()
            if not sch.get("enabled") or ov:
                return
            cur = self.scheduled_at(now)
            if not cur or self._key(cur) == st.get("last_key"):
                return
            st["last_key"] = self._key(cur)
            self.save()
        self.apply(cur[1]["mode"], f"schedule {cur[1]['time']}")

    # ---------- controls ----------

    def activate(self, mid):
        """Manual switch (UI / Stream Deck): overrides the schedule until its next change."""
        if not self.mode(mid):
            return False
        now = datetime.now()
        with self.lock:
            st = self.data["state"]
            cur = self.scheduled_at(now)
            st["last_key"] = self._key(cur)          # this block is handled; don't re-apply it
            if self.data["schedule"].get("enabled") and not (cur and cur[1]["mode"] == mid):
                nxt = self.next_change(now)
                st["override"] = {"mode": mid, "until": nxt[0].isoformat() if nxt else None}
            else:
                st["override"] = None                 # picking the scheduled mode = back on schedule
            self.save()
        threading.Thread(target=self.apply, args=(mid, "manual"), daemon=True).start()
        return True

    def cycle(self, order=None):
        order = [m for m in (order or DEFAULT_CYCLE) if self.mode(m)] or [m["id"] for m in self.data["modes"]]
        active = self.data["state"].get("active")
        nxt = order[(order.index(active) + 1) % len(order)] if active in order else order[0]
        self.activate(nxt)
        return nxt

    def toggle(self, mid):
        """One-button toggle (e.g. Watching): press to enter the mode, press again to go back to what was
        on before; if that was simply the schedule, resume it rather than holding an override."""
        if not self.mode(mid):
            return None
        with self.lock:
            st = self.data["state"]
            if st.get("active") != mid:
                st["before"] = {"mode": st.get("active"), "override": st.get("override")}
                self.save()
                entering = True
            else:
                before = st.pop("before", None) or {}
                self.save()
                entering = False
        if entering:
            self.activate(mid)
            return mid
        back = before.get("mode")
        if self.data["schedule"].get("enabled") and not before.get("override"):
            self.resume()                                   # we were on schedule before: go back to it
        elif back and self.mode(back):
            self.activate(back)
        else:
            self.resume()
        return back

    def resume_effects(self):
        """After a server restart: if the active mode runs an effect (screen sync, fire...), start it again.
        Plain modes are left alone, since the lights kept their state."""
        mid = self.data["state"].get("active")
        m = self.mode(mid)
        if m and any(step["body"].get("action") == "effect" for step in m["steps"]):
            self.apply(mid, "restarted")

    def resume(self):
        with self.lock:
            self.data["state"]["override"] = None
            self.data["state"]["last_key"] = None
            self.save()
        threading.Thread(target=self.tick, daemon=True).start()

    def capture_into(self, mid, scope):
        with self.lock:
            m = self.mode(mid)
            if not m:
                return False
            m["steps"], m["scope"] = self.capture(scope), scope
            self.save()
        return True

    def create(self, name, scope):
        name = str(name).strip()[:40]
        if not name:
            return None
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "mode"
        mid, n = base, 2
        while self.mode(mid):
            mid, n = f"{base}-{n}", n + 1
        with self.lock:
            self.data["modes"].append({"id": mid, "name": name, "scope": scope, "steps": self.capture(scope)})
            self.save()
        return mid

    def delete(self, mid):
        with self.lock:
            self.data["modes"] = [m for m in self.data["modes"] if m["id"] != mid]
            self.data["schedule"]["entries"] = [e for e in self.data["schedule"]["entries"] if e.get("mode") != mid]
            self.save()

    def set_schedule(self, enabled, entries):
        clean = []
        for e in entries or []:
            days = sorted({int(d) for d in e.get("days", []) if 0 <= int(d) <= 6})
            if days and re.fullmatch(r"\d{1,2}:\d{2}", str(e.get("time", ""))) and self.mode(e.get("mode")):
                clean.append({"days": days, "time": e["time"], "mode": e["mode"]})
        clean.sort(key=lambda e: tuple(int(x) for x in e["time"].split(":")))
        with self.lock:
            was = self.data["schedule"].get("enabled")
            self.data["schedule"] = {"enabled": bool(enabled), "entries": clean}
            if enabled and not was:
                self.data["state"]["last_key"] = None   # turning it on applies the current block
            self.save()
        threading.Thread(target=self.tick, daemon=True).start()

    def status(self):
        now = datetime.now()
        with self.lock:
            cur, nxt = self.scheduled_at(now), self.next_change(now)
            st = self.data["state"]
            return {
                "modes": [{"id": m["id"], "name": m["name"], "scope": m.get("scope")} for m in self.data["modes"]],
                "active": st.get("active"),
                "override": st.get("override"),
                "schedule": self.data["schedule"],
                "scheduled": cur[1]["mode"] if cur else None,
                "next": {"at": nxt[0].isoformat(), "mode": nxt[1]["mode"]} if nxt else None,
            }
