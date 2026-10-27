"""Outcome ledger: the agent's memory of what happened to each problem it diagnosed.

It only RECORDS. It never changes a diagnosis, a severity or what the agent does; it is the first stage of a human-approved learning
loop (later stages aggregate these records on the server and propose rule changes for a person to approve).

One *episode* = one problem (device + finding code) from the first check that saw it until the first check that no longer does:

    first_seen / last_seen / checks      how long and how often it was observed
    confidence, severity                 as diagnosed
    actions                              what the agent or the operator actually did (e.g. started a service) and the result
    resolved_at / resolution             "after_fix" (a fix reported ok during the episode) or "self_resolved" (it just went away)
    recurred                             True if the same problem came back within RECUR_HOURS of closing

From these: time to resolve, whether an offered fix worked AND stuck, and false-alarm-like patterns (problems that open and close
on their own again and again).

Stored locally in a small JSON file next to the diagnostic history. Holds only codes, device names, timestamps and action results - no
evidence text, no customer data. Bounded in size and age. Every public method swallows its own errors: recording must never break
reporting.
"""
import json
import os
import statistics
import threading
from datetime import datetime, timedelta, timezone

RECUR_HOURS = 72
KEEP_DAYS = 90
MAX_CLOSED = 500
MAX_ACTIONS = 10
FIX_WINDOW_HOURS = 24          # a fix counts as the reason for a recovery only if it ran within this long before the problem cleared
SKIP_STATES = ("DIAGNOSIS_REQUIRED",)     # the agent could not diagnose: absence of a finding then proves nothing


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def default_path(history_path):
    return os.environ.get("CSP_AGENT_OUTCOMES_PATH") or os.path.join(os.path.dirname(os.path.abspath(history_path)), "diagnostic_outcomes.json")


class Ledger:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ storage
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("open"), dict) and isinstance(data.get("closed"), list):
                return data
        except Exception:                                           # noqa: BLE001
            pass
        return {"seq": 0, "open": {}, "closed": []}

    def _save(self, data):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except Exception:                                           # noqa: BLE001
            pass

    @staticmethod
    def _key(dev, code):
        return f"{dev}:{code}"

    # ------------------------------------------------------------------ recording
    def observe(self, diagnosis, now=None):
        """Call once per diagnosis. `diagnosis` is {device: {"health_state", "findings": [...]}}."""
        try:
            now = now or _now()
            with self._lock:
                data = self._load()
                for dev, result in (diagnosis or {}).items():
                    if (result or {}).get("health_state") in SKIP_STATES:
                        continue                                    # leave this device's episodes open; we could not look
                    seen = set()
                    for f in result.get("findings") or []:
                        code = f.get("code")
                        if not code:
                            continue
                        seen.add(self._key(dev, code))
                        self._open_or_update(data, dev, f, now)
                    for key in [k for k, e in data["open"].items() if e["device"] == dev and k not in seen]:
                        self._close(data, key, now)
                self._prune(data, now)
                self._save(data)
        except Exception:                                           # noqa: BLE001
            pass

    def _open_or_update(self, data, dev, f, now):
        key = self._key(dev, f["code"])
        ep = data["open"].get(key)
        if ep:
            ep["last_seen"], ep["checks"] = _iso(now), ep["checks"] + 1
            ep["confidence"], ep["severity"] = f.get("confidence"), f.get("severity") or ep.get("severity")
            return
        data["seq"] += 1
        recurrence_of = None
        for prev in reversed(data["closed"]):
            if prev["device"] == dev and prev["code"] == f["code"]:
                ended = _parse(prev.get("resolved_at"))
                if ended and now - ended <= timedelta(hours=RECUR_HOURS):
                    prev["recurred"] = True
                    recurrence_of = prev["id"]
                break
        data["open"][key] = {"id": data["seq"], "device": dev, "code": f["code"], "category": f.get("category"),
                             "confidence": f.get("confidence"), "severity": f.get("severity"), "blocks": bool(f.get("blocks")),
                             "offered_fix": (f.get("auto_fix") or {}).get("type"), "first_seen": _iso(now), "last_seen": _iso(now),
                             "checks": 1, "actions": [], "recurrence_of": recurrence_of}

    def _close(self, data, key, now):
        ep = data["open"].pop(key)
        ep["resolved_at"] = _iso(now)
        ep["duration_s"] = max(0, int((now - (_parse(ep["first_seen"]) or now)).total_seconds()))
        window = now - timedelta(hours=FIX_WINDOW_HOURS)
        fixed = any(a.get("result") == "ok" and (_parse(a.get("t")) or now) >= window for a in ep["actions"])
        ep["resolution"] = "after_fix" if fixed else "self_resolved"
        ep["recurred"] = False
        data["closed"].append(ep)

    def record_action(self, dev, code, action, target, result, now=None):
        """Attach an action (and its result) to the open episode it was meant to fix."""
        try:
            now = now or _now()
            with self._lock:
                data = self._load()
                ep = data["open"].get(self._key(dev, code))
                if ep:
                    ep["actions"] = (ep["actions"] + [{"t": _iso(now), "action": action, "target": target, "result": result}])[-MAX_ACTIONS:]
                    self._save(data)
        except Exception:                                           # noqa: BLE001

            pass

    @staticmethod
    def _prune(data, now):
        cutoff = now - timedelta(days=KEEP_DAYS)
        data["closed"] = [e for e in data["closed"] if (_parse(e.get("resolved_at")) or now) >= cutoff][-MAX_CLOSED:]

    # ------------------------------------------------------------------ reading
    def summary(self, now=None, days=30):
        """Per problem code: how often, how long, did offered fixes work and stick. Pure arithmetic over the local records."""
        now = now or _now()
        with self._lock:
            data = self._load()
        cutoff = now - timedelta(days=days)
        by = {}
        for e in data["closed"]:
            if (_parse(e.get("resolved_at")) or now) < cutoff:
                continue
            by.setdefault((e["device"], e["code"]), []).append(e)
        for e in data["open"].values():
            by.setdefault((e["device"], e["code"]), [])
        out = []
        for (dev, code), eps in sorted(by.items()):
            fixes = [e for e in eps if any(a.get("action") for a in e["actions"])]
            fixed_ok = [e for e in fixes if e["resolution"] == "after_fix"]
            durations = [e["duration_s"] for e in eps]
            out.append({"device": dev, "code": code, "closed_episodes": len(eps),
                        "open_now": self._key(dev, code) in data["open"],
                        "median_minutes_to_resolve": round(statistics.median(durations) / 60, 1) if durations else None,
                        "self_resolved": sum(1 for e in eps if e["resolution"] == "self_resolved"),
                        "fix_attempts": len(fixes), "fix_then_resolved": len(fixed_ok),
                        "fix_stuck": sum(1 for e in fixed_ok if not e["recurred"]),
                        "recurred": sum(1 for e in eps if e["recurred"])})
        return out


def for_history(history):
    """The ledger that sits next to a History file (tests give History a temp path, so they get a temp ledger)."""
    return Ledger(default_path(history.path))


if __name__ == "__main__":                       # python outcomes.py  -> print this PC's summary
    import diagnostics
    for row in for_history(diagnostics.History()).summary():
        print(row)
