import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import run, speak_first  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from herald.telegram import Message  # noqa: E402
from test_room import Voice  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402


class Engine:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt, workspace, cfg, session_id=None):
        self.prompts.append(prompt)
        return TurnResult(True, self.answers.pop(0) if self.answers else "ok", "s1", 0.0)


def tap(data, uid=50):
    return Message(uid, OWNER, "callback", text=data, callback_id=f"cb{uid}")


class Buttons(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control, self.ws = os.path.join(self.tmp.name, "c"), os.path.join(self.tmp.name, "w")
        os.makedirs(self.control)
        os.makedirs(self.ws)
        self.bot = FakeBot()
        self.clock = [1000.0]

    def make(self, answers, agents=None):
        self.engine = Engine(answers)
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        return run.Herald(self.bot, self.engine, EngineConfig(), gate, [], self.control, self.ws, {OWNER},
                          transcript_roots=[], room_agents=agents, room_sync=True, decision_ttl_s=3600,
                          now=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def offered(self):
        return self.bot.buttons[-1]

    def test_every_turn_is_told_how_to_offer_buttons(self):
        from test_notebook import Engine as SystemEngine
        eng = SystemEngine()
        persona = os.path.join(self.tmp.name, "p.md")
        with open(persona, "w", encoding="utf-8") as f:
            f.write("You are a herald.")
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        h = run.Herald(self.bot, eng, EngineConfig(append_system_prompt_file=persona), gate, [], self.control,
                       self.ws, {OWNER}, transcript_roots=[])
        h.handle(text("hi"))
        self.assertIn("You are a herald.", eng.systems[-1])
        self.assertIn("[[choices:", eng.systems[-1])

    def test_choices_in_an_answer_become_buttons_with_postpone(self):
        h = self.make(["Two ways to go.\n[[choices: Rewrite | Patch it]]"])
        h.handle(text("what now?"))
        shown, buttons = self.offered()[1], self.offered()[2]
        self.assertNotIn("[[", shown)
        self.assertEqual([b[0] for b in buttons], ["Rewrite", "Patch it", "Postpone"])

    def test_a_tap_counts_once_and_goes_back_to_the_model(self):
        h = self.make(["Pick.\n[[choices: Rewrite | Patch it]]", "Patching now."])
        h.handle(text("what now?"))
        data = self.offered()[2][1][1]                     # the "Patch it" button
        h.handle(tap(data))
        self.assertIn("The owner chose: Patch it", self.engine.prompts[-1])
        self.assertEqual(self.bot.texts[-1][1], "Patching now.")
        h.handle(tap(data, uid=51))                        # a double tap
        self.assertEqual(len(self.engine.prompts), 2)
        self.assertIn("already", self.bot.answers[-1][1].lower())

    def test_postpone_then_it_expires_into_one_line(self):
        h = self.make(["Pick.\n[[choices: A | B]]"])
        h.handle(text("q"))
        h.handle(tap(self.offered()[2][2][1]))             # Postpone
        self.assertIn("postponed", self.bot.answers[-1][1].lower())
        self.clock[0] += 3601
        h.tick()
        h.tick()
        expired = [t for _, t in self.bot.texts if "without a decision" in t]
        self.assertEqual(len(expired), 1)                  # said once, buttons not pushed again
        h.handle(tap(self.offered()[2][0][1], uid=60))
        self.assertIn("expired", self.bot.answers[-1][1].lower())

    def test_an_unknown_or_forged_button(self):
        h = self.make([])
        for data in ("d:nope:0", "garbage", "d:" + "x" * 80):
            h.handle(tap(data))
            self.assertIn("unknown", self.bot.answers[-1][1].lower())
        self.assertEqual(self.engine.prompts, [])

    def test_a_late_tap_is_expired_even_before_any_tick(self):
        h = self.make(["Pick.\n[[choices: A | B]]"])
        h.handle(text("q"))
        self.clock[0] += 3601
        h.handle(tap(self.offered()[2][0][1]))
        self.assertIn("expired", self.bot.answers[-1][1].lower())
        self.assertEqual(len(self.engine.prompts), 1)

    def test_a_forged_tap_on_a_real_decision_is_refused(self):
        h = self.make(["Pick.\n[[choices: A | B]]"])
        h.handle(text("q"))
        did = self.offered()[2][0][1].split(":")[1]
        for data in (f"d:{did}:7", f"d:{did}:0;rm", f"xd:{did}:0"):
            h.handle(tap(data))
            self.assertIn("unknown", self.bot.answers[-1][1].lower(), data)
        self.assertEqual(len(self.engine.prompts), 1)

    def test_decisions_survive_a_restart(self):
        h = self.make(["Pick.\n[[choices: A | B]]", "ok A"])
        h.handle(text("q"))
        data = self.offered()[2][0][1]
        h2 = self.make(["ok A"])
        h2.handle(tap(data))
        self.assertIn("The owner chose: A", self.engine.prompts[-1])     # the new herald's engine

    def test_a_room_summary_comes_with_accept_more_and_postpone(self):
        voices = {"Raven": Voice("Raven", ["i", "y", "v", "summary: go A", "i2", "summary: go B"])}
        h = self.make([], agents=voices)
        h.handle(text("/room name it"))
        labels = [b[0] for b in self.offered()[2]]
        self.assertEqual(labels, ["Accept", "One more round", "Postpone"])
        h.handle(tap(self.offered()[2][1][1]))             # one more round
        self.assertIn("go B", self.offered()[1])
        h.handle(tap(self.offered()[2][0][1], uid=70))     # accept
        rooms = os.path.join(self.control, "rooms")
        newest = sorted(os.listdir(rooms))[-1]
        with open(os.path.join(rooms, newest), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
