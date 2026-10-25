"""Jev (TypeSafe System One): narrow yes/no judgments with a probability. Three jobs, each one question:
the room's moderator (is this reply an attack on a person?), the /learn sieve (is this a lesson or noise?), and the
model router (can a small fast model answer this message well?).

The key is read from TYPESAFE_API_KEY at start and only sent to api.typesafe.ai. The text judged goes there too, so
the sieve and the router are off unless the config's "jev" block turns them on. If Jev cannot be reached nothing is
changed (a reply passes, a lesson stays, the configured model answers) and the error is kept in .last_error.
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


LESSON = {
    "type": "noul",
    "instructions": "Would `proposal` help an assistant next time it meets the same situation? A lesson names a real "
                    "cause and what to do about it. Noise is a one-off, a coincidence (something that merely happened "
                    "next to the fix), a workaround that skips a check, or advice so general it changes nothing.",
    "criteria": {"true": "a reusable lesson: a cause and a concrete thing to do",
                 "false": "noise: one-off, coincidence, a bypass, or too general to act on"},
}
SIMPLE = {
    "type": "noul",
    "instructions": "Can a small, fast model answer `message` as well as a large one? Small is enough for greetings, "
                    "short facts, rewording, a single simple command. A large model is needed for multi-step work, "
                    "code changes, debugging, planning, judgment between options, or anything ambiguous.",
    "criteria": {"true": "a small fast model is enough", "false": "it needs the larger model"},
}


def _post(url, body, headers):
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


class Judge:
    def __init__(self, key, model="jev-latest", transport=None):
        self.key, self.model, self.transport, self.last_error = key, model, transport or _post, None

    def _ask(self, state, name, question):
        """The probability Jev gives, or None when he could not be asked (the caller then changes nothing)."""
        body = json.dumps({"model": self.model, "state": state, "questions": {name: question}})
        try:
            raw = self.transport(URL, body, {"Authorization": f"Bearer {self.key}",
                                             "Content-Type": "application/json"})
            return float(json.loads(raw)["answers"][name]["noul"])
        except Exception as e:  # noqa: BLE001 — see module doc: fail open, keep the reason
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return None

    def attack(self, text, topic):
        p = self._ask({"topic": topic, "reply": text}, "attack", QUESTION)
        return 0.0 if p is None else p

    def lesson(self, title, draft):
        return self._ask({"proposal": {"title": title, "text": draft[:1500]}}, "lesson", LESSON)

    def simple(self, message):
        return self._ask({"message": message[:2000]}, "simple", SIMPLE)


def from_env():
    key = os.environ.get("TYPESAFE_API_KEY")
    return Judge(key) if key else None
