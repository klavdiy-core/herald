import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import run, speak_first  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402


class Engine:
    def __init__(self, cost=0.01, context=6000):
        self.cost, self.context, self.models = cost, context, []

    def __call__(self, prompt, workspace, cfg, session_id=None):
        self.models.append(cfg.model)
        return TurnResult(True, "ok", "sess-1", self.cost, num_turns=1, context_tokens=self.context)


class Economy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = os.path.join(self.tmp.name, "control")
        self.workspace = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.control)
        os.makedirs(self.workspace)
        self.bot, self.engine = FakeBot(), Engine()
        self.h = self.make()

    def make(self, cap=None):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "speak.json"),
                                os.path.join(self.control, "STOP"))
        return run.Herald(bot=self.bot, run_turn=self.engine, engine_cfg=EngineConfig(model="claude-sonnet-5-5"),
                          gate=gate, jobs=[], control=self.control, workspace=self.workspace, owners={OWNER},
                          transcript_roots=[], max_usd_per_day=cap)

    def tearDown(self):
        self.tmp.cleanup()

    def last(self):
        return self.bot.texts[-1][1]

    def test_every_turn_is_counted_and_status_shows_today(self):
        for i in range(3):
            self.h.handle(text(f"q{i}"))
        with open(os.path.join(self.control, "costs.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["context_tokens"], 6000)
        self.h.handle(text("/status"))
        self.assertIn("today $0.03 in 3 turns", self.last())

    def test_the_daily_cap_stops_turns_and_says_why(self):
        self.h = self.make(cap=0.025)
        for i in range(4):
            self.h.handle(text(f"q{i}"))
        self.assertEqual(len(self.engine.models), 3)      # the third turn crossed the cap; the fourth never ran
        self.assertIn("daily budget", self.last())

    def test_the_cap_also_stops_speaking_first(self):
        for cap, want in ((1.0, 2), (0.01, 1)):         # control first: with room in the budget the job does speak
            self.engine.models.clear()
            for name in ("costs.jsonl", "speak.json"):
                p = os.path.join(self.control, name)
                if os.path.exists(p):
                    os.remove(p)
            self.h = self.make(cap=cap)
            self.h.handle(text("q"))
            self.h.jobs = [speak_first.Job("check", "x", every_min=1)]
            self.h.gate.limits = speak_first.Limits(enabled=True, quiet=None)   # no quiet hours: not clock-bound
            self.h.tick()
            self.assertEqual(len(self.engine.models), want, cap)

    def test_only_today_counts(self):
        with open(os.path.join(self.control, "costs.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"day": "2000-01-01", "usd": 5.0}) + "\n")
        self.h = self.make(cap=1.0)
        self.h.handle(text("q"))
        self.assertEqual(len(self.engine.models), 1)

    def test_model_switches_at_runtime_and_survives_a_restart(self):
        self.h.handle(text("/model claude-haiku-5-5"))
        self.h.handle(text("q"))
        self.make().handle(text("q2"))
        self.assertEqual(self.engine.models, ["claude-haiku-5-5", "claude-haiku-5-5"])
        self.h.handle(text("/model default"))
        self.h.handle(text("q3"))
        self.assertEqual(self.engine.models[-1], "claude-sonnet-5-5")

    def test_an_unknown_model_name_is_refused(self):
        self.h.handle(text("/model gpt-9; rm -rf /"))
        self.assertIn("claude-", self.last())
        self.h.handle(text("q"))
        self.assertEqual(self.engine.models, ["claude-sonnet-5-5"])


if __name__ == "__main__":
    unittest.main()
