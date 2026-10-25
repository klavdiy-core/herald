"""Reliability findings from an outside review (25.10): spending after /stop, speaking into quiet hours,
a killed turn costing nothing, jobs marked run without running. Each test is red on 0.12.0."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import engine, speak_first, telegram  # noqa: E402
from herald.telegram import Message  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_run import OWNER, Base  # noqa: E402


class StopAndQuietDuringATurn(Base):
    def job(self):
        return [speak_first.Job("check", "anything new?", every_min=60)]

    def test_a_stop_that_arrives_during_a_proactive_turn_sends_nothing(self):
        h = self.make(jobs=self.job())
        stop = os.path.join(self.control, "STOP")
        real = self.engine

        def engine_then_stop(*a, **k):
            r = real(*a, **k)
            open(stop, "w").close()                      # /stop lands while the turn runs
            return r
        h.run_turn = engine_then_stop
        h.tick()
        self.assertEqual(self.bot.texts, [])

    def test_quiet_hours_that_begin_during_the_turn_send_nothing(self):
        h = self.make(jobs=self.job())
        self.clock.t = datetime(2026, 10, 24, 21, 59)
        real = self.engine

        def slow(*a, **k):
            self.clock.t = datetime(2026, 10, 24, 22, 10)  # the turn took 11 minutes
            return real(*a, **k)
        h.run_turn = slow
        h.tick()
        self.assertEqual(self.bot.texts, [])


class Jobs(Base):
    def test_a_job_that_did_not_run_this_tick_is_still_due(self):
        jobs = [speak_first.Job("a", "first?", every_min=60), speak_first.Job("b", "second?", every_min=60)]
        self.engine.answer = "[SILENT]"                  # silent: the gate stays open for the next tick
        h = self.make(jobs=jobs)
        h.tick()
        h.tick()
        prompts = [c["prompt"] for c in self.engine.calls]
        self.assertTrue(any("first?" in p for p in prompts), prompts)
        self.assertTrue(any("second?" in p for p in prompts), prompts)


class CorruptSpeakState(Base):
    def corrupt(self):
        with open(os.path.join(self.control, "speak.json"), "w") as f:
            f.write("{broken")

    def test_it_stays_closed_on_the_next_tick_too(self):
        self.corrupt()
        self.assertFalse(self.h.gate.may_speak()[0])
        self.assertFalse(self.h.gate.may_speak()[0])      # it used to start fresh here, caps forgotten

    def test_an_owner_reply_opens_it_again(self):
        self.corrupt()
        self.h.gate.record_owner_reply()                  # the owner writes before the gate looks at the file
        self.assertTrue(self.h.gate.may_speak()[0])       # it used to stay shut for good ("broken" was saved)
        self.assertTrue(self.h.gate.may_speak()[0])


class TelegramHiccups(Base):
    def test_a_late_toast_does_not_burn_the_choice(self):
        # the tap is answered after a long turn: Telegram says "query is too old"; the chosen turn must still run
        did = self.h.decisions.offer("which?", ["A", "B"])

        def too_old(*a, **k):
            raise telegram.TelegramError("Bad Request: query is too old")
        self.bot.answer_callback = too_old
        self.h.handle(Message(9, OWNER, "callback", text=f"d:{did}:0", callback_id="cb9"))
        self.assertTrue(any("The owner chose: A" in c["prompt"] for c in self.engine.calls), self.engine.calls)

    def test_a_failed_typing_signal_does_not_lose_the_message(self):
        def blip(*a, **k):
            raise telegram.TelegramError("network blip")
        self.bot.send_action = blip
        self.h.handle(Message(10, OWNER, "text", text="hi"))
        self.assertEqual(len(self.engine.calls), 1)
        self.assertTrue(self.bot.texts)


class FakeRoom:
    def __init__(self):
        self.topic, self.halted, self.spent, self.messages, self.ended = "x", None, 0.0, [], False
        self.record_path = None

    def reopen(self):
        pass

    def round(self):
        return True

    def end(self):
        self.ended = True                                # a paid summary call
        return "summary"


class RoomAfterStop(Base):
    def test_one_more_round_after_stop_spends_nothing(self):
        r = FakeRoom()
        self.h.live_rooms["k"] = r
        open(os.path.join(self.control, "STOP"), "w").close()
        self.h._room_choice(OWNER, {"choice": "One more round", "meta": {"room": "k"}})
        self.assertFalse(r.ended)
        self.assertIn("stop", self.bot.texts[-1][1].lower())

    def test_a_round_halted_by_the_budget_makes_no_summary(self):
        r = FakeRoom()

        def halted_round():
            r.halted = "budget"
            return False
        r.round = halted_round
        self.h.live_rooms["k"] = r
        self.h._room_choice(OWNER, {"choice": "One more round", "meta": {"room": "k"}})
        self.assertFalse(r.ended)


class KilledTurnCost(unittest.TestCase):
    def cfg(self, cmd, timeout):
        return EngineConfig(claude_cmd=[sys.executable, "-c", cmd], timeout_s=timeout, max_budget_usd=0.5)

    def test_a_turn_killed_by_timeout_counts_its_whole_budget(self):
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg("import time; time.sleep(30)", 1))
        self.assertFalse(r.ok)
        self.assertEqual(r.cost_usd, 0.5)
        self.assertTrue(r.cost_estimated)

    def test_a_turn_without_json_counts_its_whole_budget(self):
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg("print('not json')", 30))
        self.assertEqual(r.cost_usd, 0.5)
        self.assertTrue(r.cost_estimated)


class LedgerMarksAnEstimate(Base):
    def test_the_ledger_says_when_a_cost_is_an_estimate(self):
        self.h.run_turn = lambda *a, **k: TurnResult(False, error="timeout", cost_usd=0.5, cost_estimated=True)
        self.h._turn("hi")
        with open(self.h.costs_path, encoding="utf-8") as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual(row["usd"], 0.5)
        self.assertTrue(row["estimated"])


if __name__ == "__main__":
    unittest.main()
