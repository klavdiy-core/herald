"""Jev (TypeSafe System One) as the room's moderator: one narrow yes/no judgment with a probability.

The key is read from TYPESAFE_API_KEY at start and only sent to api.typesafe.ai. If Jev cannot be reached the reply
is let through (a moderator outage must not silence the room) and the error is kept in .last_error.
"""
import json
import os
import urllib.request

URL = "https://api.typesafe.ai/v1/systemone"
QUESTION = {
    "type": "noul",
    "instructions": "Does `reply` attack, insult or belittle a person taking part in the discussion? Firm criticism "
                    "of an idea, a plan or an argument is not an attack; mocking or blaming the person is.",
    "criteria": {"true": "the reply targets a participant as a person",
                 "false": "the reply criticizes or discusses ideas, or agrees, or is neutral"},
}


def _post(url, body, headers):
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


class Judge:
    def __init__(self, key, model="jev-latest", transport=None):
        self.key, self.model, self.transport, self.last_error = key, model, transport or _post, None

    def attack(self, text, topic):
        body = json.dumps({"model": self.model, "state": {"topic": topic, "reply": text},
                           "questions": {"attack": QUESTION}})
        try:
            raw = self.transport(URL, body, {"Authorization": f"Bearer {self.key}",
                                             "Content-Type": "application/json"})
            return float(json.loads(raw)["answers"]["attack"]["noul"])
        except Exception as e:  # noqa: BLE001 — see module doc: fail open, keep the reason
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return 0.0


def from_env():
    key = os.environ.get("TYPESAFE_API_KEY")
    return Judge(key) if key else None
