"""The daily cap with two consumers at once (external review + Tsaplya, 25.10): the ledger was read before a paid call
and written after it, so the bot and a room could both pass with a few cents left and both pay. Red before the hold."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import run  # noqa: E402


def _row(usd, **kw):
    return json.dumps({"day": time.strftime("%Y-%m-%d"), "t": "00:00:00", "usd": usd, **kw}) + "\n"


class DailyHold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.costs = os.path.join(self.tmp.name, "costs.jsonl")
        with open(self.costs, "w", encoding="utf-8") as f:
            f.write(_row(0.90, ok=True))                                       # cap 1.00: 0.10 left

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_consumers_cannot_both_take_the_last_ten_cents(self):
        got, start = [], threading.Barrier(2)

        def consumer():
            start.wait()
            got.append(run.reserve(self.costs, 0.10, 1.00, "turn"))

        ts = [threading.Thread(target=consumer) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(1 for g in got if g), 1)
        self.assertLessEqual(run.spent_today(self.costs)[0], 1.00 + 1e-9)

    def test_settle_releases_the_hold_and_writes_the_real_cost(self):
        rid = run.reserve(self.costs, 0.10, 1.00, "turn")
        self.assertAlmostEqual(run.spent_today(self.costs)[0], 1.00)
        run.settle(self.costs, rid, 0.10, json.loads(_row(0.03, ok=True)))
        usd, turns = run.spent_today(self.costs)
        self.assertAlmostEqual(usd, 0.93)
        self.assertEqual(turns, 2)                                             # holds are not turns
        self.assertFalse(os.path.exists(self.costs + ".lock"))


if __name__ == "__main__":
    unittest.main()
