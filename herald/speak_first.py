"""The right to speak first, with its limits in code, not in the prompt.

Off until enabled. A proactive message may go out only if: no STOP file, not in quiet hours, under the daily
cap, past the minimum gap, and the owner answered the last messages. The model may always choose silence:
an answer of `[SILENT]` sends nothing. State lives in one JSON file, written atomically; if it cannot be
read, herald stays quiet (fail closed).
"""
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass
class Limits:
    enabled: bool = False
    per_day: int = 3
    min_gap_min: int = 180
    quiet: tuple = ("22:00", "09:00")   # local time; may cross midnight; None = no quiet hours
    max_unanswered: int = 2

    def __post_init__(self):
        if self.per_day < 1 or self.min_gap_min < 0 or self.max_unanswered < 1:
            raise ValueError("per_day and max_unanswered must be >= 1, min_gap_min >= 0")
        if self.quiet and not all(HHMM.match(q) for q in self.quiet):
            raise ValueError("quiet hours are two HH:MM times")


@dataclass
class Job:
    name: str
    prompt: str
    at: str = None          # daily, local HH:MM
    every_min: int = None   # or every N minutes

    def __post_init__(self):
        if (self.at is None) == (self.every_min is None):
            raise ValueError(f"job {self.name}: give exactly one of at= or every_min=")
        if self.at is not None and not HHMM.match(self.at):
            raise ValueError(f"job {self.name}: at= is HH:MM")
        if self.every_min is not None and self.every_min < 1:
            raise ValueError(f"job {self.name}: every_min >= 1")


def what_to_send(answer):
    """None when the model chose silence."""
    text = (answer or "").strip()
    return None if not text or text.upper() == "[SILENT]" else text


class Gate:
    def __init__(self, limits, state_path, stop_path, now=datetime.now):
        self.limits, self.state_path, self.stop_path, self.now = limits, state_path, stop_path, now

    # ---------- state ----------
    def _load(self):
        if not os.path.exists(self.state_path):
            return {"sent": [], "unanswered": 0, "jobs": {}}
        with open(self.state_path, encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s.get("sent"), list) or not isinstance(s.get("unanswered"), int):
            raise ValueError("unexpected shape")
        s.setdefault("jobs", {})
        return s

    def _save(self, s):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, self.state_path)

    # ---------- the gate ----------
    def may_speak(self):
        L, now = self.limits, self.now()
        if not L.enabled:
            return False, "speaking first is disabled"
        if os.path.exists(self.stop_path):
            return False, "STOP file present"
        try:
            s = self._load()
        except (OSError, ValueError) as e:
            return False, f"state unreadable, staying quiet: {e}"
        if L.quiet:
            q0, q1, m = _minutes(L.quiet[0]), _minutes(L.quiet[1]), now.hour * 60 + now.minute
            if (q0 <= m < q1) if q0 < q1 else (m >= q0 or m < q1):
                return False, "quiet hours"
        sent = [datetime.fromisoformat(t) for t in s["sent"]]
        if sum(1 for t in sent if t.date() == now.date()) >= L.per_day:
            return False, f"{L.per_day} per day already sent"
        if sent and now - max(sent) < timedelta(minutes=L.min_gap_min):
            return False, "too soon after the last one"
        if s["unanswered"] >= L.max_unanswered:
            return False, f"{s['unanswered']} unanswered, waiting for the owner"
        return True, "ok"

    def record_sent(self):
        s = self._load()
        now = self.now()
        s["sent"] = [t for t in s["sent"] if datetime.fromisoformat(t) > now - timedelta(days=2)] + [now.isoformat()]
        s["unanswered"] += 1
        self._save(s)

    def record_owner_reply(self):
        s = self._load()
        s["unanswered"] = 0
        self._save(s)

    def take_due(self, jobs):
        """Jobs whose time has come; each is marked as run. A long downtime gives one run, not a burst."""
        s, now, due = self._load(), self.now(), []
        for j in jobs:
            last = s["jobs"].get(j.name)
            last = datetime.fromisoformat(last) if last else None
            if j.every_min is not None:
                ready = last is None or now - last >= timedelta(minutes=j.every_min)
            else:
                at = now.replace(hour=_minutes(j.at) // 60, minute=_minutes(j.at) % 60, second=0, microsecond=0)
                ready = now >= at and (last is None or last < at)
            if ready:
                due.append(j)
                s["jobs"][j.name] = now.isoformat()
        if due:
            self._save(s)
        return due
