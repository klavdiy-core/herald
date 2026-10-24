"""Decisions offered to the owner as buttons: each counts once, can be postponed, and expires.

Kept in control/decisions.json so a tap still works after a restart. A decision expires after its TTL whether it
was postponed or simply left; an expired one is reported once (one line, the buttons are not pushed again).
"""
import json
import os
import secrets


def _valid(d):
    return (isinstance(d, dict) and d.get("status") in ("open", "postponed", "done", "expired")
            and isinstance(d.get("options"), list) and isinstance(d.get("text"), str)
            and isinstance(d.get("expires"), (int, float)) and isinstance(d.get("meta", {}), dict))


class Decisions:
    def __init__(self, path, now, ttl_s=24 * 3600):
        self.path, self.now, self.ttl_s = path, now, ttl_s

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # a damaged entry is dropped, not trusted: a record without "status" once crashed every tick
                return {k: v for k, v in data.items() if _valid(v)}
        except (ValueError, OSError):
            pass
        os.replace(self.path, self.path + ".bad")
        return {}

    def _save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def offer(self, text, options, kind="turn", meta=None):
        data, did = self._load(), secrets.token_hex(4)
        data[did] = {"text": text, "options": list(options), "kind": kind, "meta": meta or {},
                     "created": self.now(), "expires": self.now() + self.ttl_s, "status": "open", "choice": None}
        self._save(data)
        return did

    def tap(self, did, which):
        """which: an option index or 'p'. Returns (outcome, decision): outcome is chosen, postponed, unknown,
        already, expired."""
        data = self._load()
        d = data.get(did)
        if d is None:
            return "unknown", None
        if d["status"] == "done":
            return "already", d
        if d["status"] == "expired" or self.now() >= d["expires"]:
            d["status"] = "expired"
            self._save(data)
            return "expired", d
        if which == "p":
            d["status"] = "postponed"
            self._save(data)
            return "postponed", d
        if not (0 <= which < len(d["options"])):
            return "unknown", None
        d["status"], d["choice"] = "done", d["options"][which]
        self._save(data)
        return "chosen", d

    def expire_due(self):
        """Open or postponed decisions past their time: marked expired and returned, each only once."""
        data, gone = self._load(), []
        for d in data.values():
            if d["status"] in ("open", "postponed") and self.now() >= d["expires"]:
                d["status"] = "expired"
                gone.append(d)
        if gone:
            self._save(data)
        return gone
