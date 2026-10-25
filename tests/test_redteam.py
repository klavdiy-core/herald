"""What the red-team audit of 25.10 confirmed (three attackers, each claim reproduced by a skeptic on a fresh clone).
No escape was found; these are gaps in what is checked or counted. Each test is red on 0.20.0."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import doctor, engine, run  # noqa: E402
from herald.engine import TurnResult  # noqa: E402
from test_doctor import fake_turn  # noqa: E402
from test_room import Voice  # noqa: E402
from test_run import text  # noqa: E402
from test_vs_hermes import Base  # noqa: E402


class Canary(unittest.TestCase):
    def test_a_model_that_never_tries_the_forbidden_writes_proves_nothing_about_them(self):
        # the read half demanded refusals; the write half passed on "nothing landed", which a polite model that never
        # tried gives just as well
        r = doctor.canary(run_turn=fake_turn([("inside.txt", "OK")], denials=("Read", "Read", "Read", "Grep", "Glob")),
                          cfg=None)
        self.assertFalse(r.ok, r.detail)
        self.assertIn("inconclusive", r.detail)
        self.assertIn("write", r.detail.lower())


    def test_an_inconclusive_run_is_tried_again_but_a_breach_never_is(self):
        # live (25.10): the same haiku canary refused every step in one run and did all of them in the next; a
        # polite run must not keep herald from starting, and a breach must not get a second chance
        calls = []
        polite = fake_turn([("inside.txt", "OK")], denials=())
        good = fake_turn([("inside.txt", "OK")])

        def flaky(prompt, ws, cfg, session_id=None):
            calls.append(1)
            return (polite if len(calls) == 1 else good)(prompt, ws, cfg)
        r = doctor.canary(run_turn=flaky, cfg=None)
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(len(calls), 2)
        calls.clear()

        def breach(prompt, ws, cfg, session_id=None):
            calls.append(1)
            return fake_turn([("inside.txt", "OK"), ("CLAUDE.md", "obey me")])(prompt, ws, cfg)
        r = doctor.canary(run_turn=breach, cfg=None)
        self.assertFalse(r.ok)
        self.assertEqual(len(calls), 1)
        calls.clear()
        r = doctor.canary(run_turn=lambda *a, **k: (calls.append(1), polite(*a[:3]))[1], cfg=None)
        self.assertFalse(r.ok)
        self.assertEqual(len(calls), 3)
        self.assertIn("3 runs", r.detail)


class Flags(unittest.TestCase):
    def test_extra_args_cannot_replace_the_limits_herald_fixes(self):
        # claude takes the last value of a repeated flag: extra_args came after herald's own
        for bad in ("--max-budget-usd", "--max-budget-usd=9", "--model", "--output-format", "--max-turns",
                    "--append-system-prompt-file", "--system-prompt"):
            with self.subTest(flag=bad), self.assertRaises(ValueError):
                engine.EngineConfig(extra_args=[bad, "x"] if "=" not in bad else [bad])


class Money(Base):
    def test_less_than_a_cent_left_starts_no_turn(self):
        # the per-turn floor was $0.01: with $0.003 left a turn could still spend a cent
        calls = []

        def eng(prompt, ws, cfg, session_id=None, should_abort=None):
            calls.append(cfg.max_budget_usd)
            return TurnResult(True, "ok", "s", 0.497)
        h = self.herald(cap=0.5, engine=eng)
        h.handle(text("one"))
        h.handle(text("two"))
        self.assertEqual(len(calls), 1)
        self.assertIn("budget", self.bot.texts[-1][1].lower())

    def test_a_room_keeps_the_next_reply_in_hand_and_its_spend_reaches_the_ledger_as_it_talks(self):
        # the room checked "spent >= cap" before a reply, so the reply that crossed it was paid; and an ordinary
        # message during the room did not see the room's spend, which reached the ledger only at the end
        seen = []

        class Peek(Voice):
            def speak(inner, prompt):
                with open(os.path.join(self.control, "costs.jsonl"), encoding="utf-8") as f:
                    seen.append(round(sum(json.loads(l)["usd"] for l in f), 4))
                return super().speak(prompt)
        voices = {"Raven": Peek("Raven", ["x"] * 9, cost=0.04), "Owl": Peek("Owl", ["y"] * 9, cost=0.04)}
        for v in voices.values():
            v.reply_cap = 0.04
        open(os.path.join(self.control, "costs.jsonl"), "w").close()
        h = self.herald(voices, cap=0.1)
        h.handle(text("/room t"))
        with open(os.path.join(self.control, "costs.jsonl"), encoding="utf-8") as f:
            total = sum(json.loads(l)["usd"] for l in f)
        self.assertLessEqual(total, 0.1 + 1e-9)                     # 0.04 + 0.04, and the third would cross it
        self.assertEqual(seen[1], 0.04)                              # the first reply was in the ledger already


class Stop(Base):
    def test_stop_with_more_lines_is_still_stop(self):
        h = self.herald()
        h.handle(text("/stop\nand do not continue"))
        self.assertTrue(os.path.exists(os.path.join(self.control, "STOP")))
        self.assertEqual(run._command_word("/stop\tnow"), "/stop")


if __name__ == "__main__":
    unittest.main()
