import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import run, speak_first  # noqa: E402
from herald.engine import EngineConfig  # noqa: E402
from test_room import Voice  # noqa: E402
from test_run import OWNER, FakeBot, FakeEngine, text  # noqa: E402


class RoomInTelegram(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = os.path.join(self.tmp.name, "control")
        self.ws = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.control)
        os.makedirs(self.ws)
        self.bot = FakeBot()
        self.voices = {"Raven": Voice("Raven", ["idea", "yes and", "vote", "summary: go with idea"], cost=0.01),
                       "Owl": Voice("Owl", ["risk", "fix", "vote"], cost=0.01)}

    def make(self, cap=None, agents=True):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        return run.Herald(self.bot, FakeEngine(), EngineConfig(), gate, [], self.control, self.ws, {OWNER},
                          transcript_roots=[], max_usd_per_day=cap,
                          room_agents=self.voices if agents else None, room_sync=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_room_runs_its_rounds_and_sends_the_summary(self):
        h = self.make()
        h.handle(text("/room name the bot"))
        texts = [t for _, t in self.bot.texts]
        self.assertIn("name the bot", texts[0])                       # started, says who sits there
        self.assertIn("Raven", texts[0])
        self.assertIn("go with idea", texts[-1])
        self.assertEqual(len(os.listdir(os.path.join(self.control, "rooms"))), 1)

    def test_room_money_goes_into_the_daily_ledger(self):
        h = self.make()
        h.handle(text("/room t"))
        with open(os.path.join(self.control, "costs.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f]
        self.assertAlmostEqual(sum(r["usd"] for r in rows), 0.07, places=4)   # 6 replies + summary
        self.assertEqual(rows[-1].get("kind"), "room")

    def test_the_daily_cap_keeps_a_room_from_starting(self):
        h = self.make(cap=0.0)
        h.handle(text("/room t"))
        self.assertIn("daily budget", self.bot.texts[-1][1])
        self.assertEqual(self.voices["Raven"].prompts, [])

    def test_without_agents_room_explains_how(self):
        h = self.make(agents=False)
        h.handle(text("/room t"))
        self.assertIn("agents", self.bot.texts[-1][1])

    def test_room_without_a_topic(self):
        h = self.make()
        h.handle(text("/room"))
        self.assertIn("/room", self.bot.texts[-1][1])


if __name__ == "__main__":
    unittest.main()
