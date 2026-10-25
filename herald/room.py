"""A room where several agents discuss one topic in turns: brainstorm or decide, each in their own voice, no attacks.

The owner picks who sits in the room and the role each one plays this time, watches it live, and can pause, speak
into it, choose who talks next, ask to go deeper, and end it for a summary. A judge (Jev or anything with
`attack(text, topic) -> probability`) checks each reply before it is shown: an attack on a person is sent back once
to be said about the idea; a second one is held back. Money is counted per reply and the room stops at its budget.
"""
import json
import os
import re
import time
from dataclasses import dataclass

ROLES = {
    "ideas": "bring new ideas; quantity first",
    "skeptic": "find the weak points of ideas and offer a fix for each; kind, never personal",
    "specialist": "bring facts, constraints and how it would really be built",
    "what-if": "turn ideas around: what if the opposite, bigger, smaller, for someone else",
    "goal-keeper": "keep the room on the owner's goal; say when it drifts",
    "moderator": "keep it fair, bring quiet voices in, sum up where people stand",
}
PHASES = {
    "brainstorm": ["Ideas only, no evaluation: offer one to three new ideas.",
                   "Yes, and: pick one idea from the others and make it better.",
                   "Vote: name the two ideas you would keep (whose and a short name) and why, one line each."],
    "decide": ["Lay out options: one to three distinct options to decide between.",
               "For the options on the table give pros, cons and cost.",
               "Recommend one option and say what would change your mind."],
}
RULES = ("Rules for everyone: speak in your own voice and briefly (under 120 words). Criticize the idea, not the "
         "person: no attacks, no flattery. Build on what others said when you can.")
REPHRASE = "Your last reply attacked a person. Say it again about the idea, not the person."
SUMMARY = ("Sum the discussion up for the owner: the strongest ideas or options, where people agreed and where not, "
           "and one recommendation. Plain words, under 200 words.")
ATTACK = 0.5


@dataclass
class Seat:
    agent: object          # anything with .name and .speak(prompt) -> (text, cost_usd)
    role: str = "ideas"


class Room:
    def __init__(self, topic, seats, fmt="brainstorm", budget_usd=None, judge=None, record_dir=None, history=12,
                 should_stop=None):
        if fmt not in PHASES:
            raise ValueError(f"format must be one of {sorted(PHASES)}")
        if not seats:
            raise ValueError("a room needs at least one seat")
        self.topic, self.seats, self.fmt, self.budget, self.judge = topic, list(seats), fmt, budget_usd, judge
        self.record_dir, self.history = record_dir, history
        self.messages, self.events, self.listeners = [], [], []
        self.spent, self.turn, self.rounds = 0.0, 0, 0
        self.paused, self.forced, self.focus, self.closed, self.over = False, None, None, False, False
        self.record_path = None
        self.should_stop, self.halted = should_stop, None   # should_stop() -> a reason ("stop", "budget") or None

    # ---------- events ----------
    def listen(self, fn):
        self.listeners.append(fn)

    def _emit(self, kind, **data):
        e = {"type": kind, "t": time.time(), **data}
        self.events.append(e)
        for fn in list(self.listeners):
            try:
                fn(e)
            except Exception:  # noqa: BLE001 — a closed browser tab must not stop the room
                pass
        return e

    # ---------- the owner's levers ----------
    def pause(self):
        self.paused = True
        self._emit("paused")

    def resume(self):
        self.paused = False
        self._emit("resumed")

    def inject(self, text):
        m = {"speaker": "Owner", "role": "owner", "text": text.strip(), "round": self.rounds, "held": False}
        self.messages.append(m)
        self._emit("message", **m)

    def next_speaker(self, name):
        if not any(s.agent.name == name for s in self.seats):
            raise KeyError(name)
        self.forced = name

    def deeper(self, text):
        self.focus = text.strip()
        self._emit("focus", text=self.focus)

    # ---------- talking ----------
    def phase(self):
        p = PHASES[self.fmt]
        return p[min(self.rounds, len(p) - 1)]

    def _prompt(self, seat):
        role = ROLES.get(seat.role, seat.role)
        said = "\n".join(f"[{i}] {m['speaker']} ({m['role']}): {m['text']}"
                         for i, m in enumerate(self.messages[-self.history:], 1)) or "(nobody has spoken yet)"
        focus = f"\nThe owner asks to go deeper on: {self.focus}" if self.focus else ""
        return (f"You are {seat.agent.name} in a small room of AI agents discussing: \"{self.topic}\".\n"
                f"Your role this time: {seat.role} - {role}.\n{RULES}\nThis round: {self.phase()}{focus}\n\n"
                f"Discussion so far:\n{said}\n\nYour turn, {seat.agent.name}:")

    def _say(self, agent, prompt):
        try:
            text, cost = agent.speak(prompt)
        except Exception as e:  # noqa: BLE001 — one broken participant must not take the room down
            text, cost = f"({agent.name} could not answer: {type(e).__name__})", 0.0
        self.spent += float(cost or 0)
        return (text or "").strip()

    def _reserve(self):
        """What one more reply may cost at most (the largest per-reply cap at the table; 0 when not known).
        A reply starts only if it fits: the budget used to be checked against what was already spent, so the last
        reply always went over it."""
        return max((float(getattr(s.agent, "reply_cap", 0) or 0) for s in self.seats), default=0.0)

    def step(self):
        """One reply. False when paused, closed or out of budget."""
        if self.paused or self.closed or self.halted:
            return False
        why = self.should_stop() if self.should_stop else None   # the owner's STOP and money hold before every reply
        if why:
            self.halted = why
            self._emit("halted", reason=why, spent=round(self.spent, 4))
            return False
        if self.budget is not None and (self.spent >= self.budget
                                        or self.spent + self._reserve() > self.budget + 1e-9):
            if not self.over:
                self.over = True
                self._emit("budget", spent=round(self.spent, 4), budget=self.budget)
            return False
        forced = bool(self.forced)
        if forced:                                       # an extra turn the owner asked for: not part of a round
            idx = next(i for i, s in enumerate(self.seats) if s.agent.name == self.forced)
            self.forced = None
        else:
            idx = self.turn % len(self.seats)
            self.turn += 1
        seat = self.seats[idx]
        self._emit("thinking", speaker=seat.agent.name)
        prompt = self._prompt(seat)
        text, held = self._say(seat.agent, prompt), False
        if self.judge and self.judge.attack(text, self.topic) >= ATTACK:
            self._emit("moderated", speaker=seat.agent.name)
            text = self._say(seat.agent, prompt + "\n\n" + REPHRASE)
            if self.judge.attack(text, self.topic) >= ATTACK:
                text, held = "(held back by the moderator)", True
        m = {"speaker": seat.agent.name, "role": seat.role, "text": text, "round": self.rounds, "held": held}
        self.messages.append(m)
        self._emit("message", spent=round(self.spent, 4), **m)
        if not forced and self.turn % len(self.seats) == 0:
            self.rounds += 1
            done = self.rounds >= len(PHASES[self.fmt])
            self._emit("round", n=self.rounds, next="all rounds done" if done else self.phase())
        return True

    def round(self):
        for _ in range(len(self.seats)):
            if not self.step():
                return False
        return True

    def reopen(self):
        """After a summary, for one more round."""
        self.closed = False

    def decide(self, decision):
        """The owner's decision goes into the latest record."""
        if self.record_path and os.path.exists(self.record_path):
            with open(self.record_path, encoding="utf-8") as f:
                rec = json.load(f)
            rec["decision"] = decision
            with open(self.record_path, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)

    def end(self, summarizer=None):
        """Close the room: a summary for the owner and a record on disk."""
        self.closed = True
        agent = summarizer or next((s.agent for s in self.seats if s.role == "moderator"), self.seats[0].agent)
        said = "\n".join(f"{m['speaker']} ({m['role']}): {m['text']}" for m in self.messages if not m["held"])
        if self.budget is not None and self.spent + self._reserve() > self.budget + 1e-9:
            # no money left for a paid summary: the last word of each speaker, free, and said so
            last = {}
            for m in self.messages:
                if not m["held"]:
                    last[m["speaker"]] = m["text"]
            summary = ("(the room's budget is used up, so this is not a written summary: the last word of each "
                       "speaker)\n" + "\n".join(f"- {who}: {t[:300]}" for who, t in last.items()))
        else:
            summary = self._say(agent, f"Topic: \"{self.topic}\".\n\n{said}\n\n{SUMMARY}")
        self._emit("summary", text=summary, spent=round(self.spent, 4))
        if self.record_dir:
            os.makedirs(self.record_dir, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", self.topic.lower())[:40].strip("-") or "room"
            path = os.path.join(self.record_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{len(self.messages):03d}-{slug}.json")
            self.record_path = path
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"topic": self.topic, "format": self.fmt, "spent_usd": round(self.spent, 4),
                           "seats": [{"name": s.agent.name, "role": s.role} for s in self.seats],
                           "messages": self.messages, "summary": summary}, f, ensure_ascii=False, indent=1)
        return summary
