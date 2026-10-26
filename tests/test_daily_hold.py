"""The daily cap with two consumers at once (external review + a second review, 25.10): the ledger was read before a paid call
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
        with run._LedgerLock(self.costs, wait_s=0.2):                         # released: free to take at once
            pass


class RoomHolds(unittest.TestCase):
    """should_stop is also polled as should_abort while a reply runs: a hold must be placed once per reply, not per
    poll; the browser room (room_guard) holds too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"control": self.tmp.name, "max_usd_per_day": 1.0,
                    "agents": [{"name": "a", "max_budget_usd": 0.3}, {"name": "b", "max_budget_usd": 0.2}]}
        self.costs = os.path.join(self.tmp.name, "costs.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        if not os.path.exists(self.costs):
            return []
        with open(self.costs, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_room_guard_holds_once_per_reply_and_settles_on_spend(self):
        should_stop, on_spend = run.room_guard(self.cfg)
        for _ in range(50):                                                   # polls during one reply
            self.assertIsNone(should_stop())
        self.assertEqual(sum(1 for r in self.rows() if r.get("kind") == "reserve"), 1)
        self.assertAlmostEqual(run.spent_today(self.costs)[0], 0.3)           # the biggest cap is held
        on_spend(0.05)
        self.assertAlmostEqual(run.spent_today(self.costs)[0], 0.05)

    def test_the_bot_room_holds_once_while_a_reply_is_polled(self):
        from types import SimpleNamespace
        fake = SimpleNamespace(stop_path=os.path.join(self.tmp.name, "STOP"), costs_path=self.costs,
                               max_usd_per_day=1.0)
        fake._room_log = lambda r: run.Herald._room_log(fake, r)
        fake._over_budget = lambda extra=0.0: False
        fake._today = lambda: run.spent_today(self.costs)
        seat = SimpleNamespace(agent=SimpleNamespace(reply_cap=0.3))
        r = SimpleNamespace(spent=0.0, logged=0.0, seats=[seat])
        for _ in range(50):                                                   # before the reply, then polls during it
            self.assertIsNone(run.Herald._room_halt(fake, r))
        self.assertEqual(sum(1 for x in self.rows() if x.get("kind") == "reserve"), 1)
        r.spent = 0.1                                                         # the reply is paid; the next one holds anew
        run.Herald._room_halt(fake, r)
        self.assertEqual(sum(1 for x in self.rows() if x.get("kind") == "reserve"), 2)
        self.assertAlmostEqual(run.spent_today(self.costs)[0], 0.1 + 0.3)

    def test_a_standing_hold_still_sees_the_cap_used_up_elsewhere(self):
        # second review: a free reply leaves the hold standing; another process spends past the cap; the next reply
        # must not go on (0.23.4 stopped checking while a hold stood)
        should_stop, _ = run.room_guard(self.cfg)
        self.assertIsNone(should_stop())                                      # reply 1 held, then cost nothing
        with open(self.costs, "a", encoding="utf-8") as f:
            f.write(_row(5.0, ok=True))
        self.assertEqual(should_stop(), "budget")
        from types import SimpleNamespace
        costs2 = os.path.join(self.tmp.name, "costs2.jsonl")
        fake = SimpleNamespace(stop_path=os.path.join(self.tmp.name, "STOP"), costs_path=costs2, max_usd_per_day=1.0)
        fake._room_log = lambda r: run.Herald._room_log(fake, r)
        fake._over_budget = lambda extra=0.0: False
        fake._today = lambda: run.spent_today(costs2)
        r = SimpleNamespace(spent=0.0, logged=0.0, seats=[SimpleNamespace(agent=SimpleNamespace(reply_cap=0.3))])
        self.assertIsNone(run.Herald._room_halt(fake, r))
        with open(costs2, "a", encoding="utf-8") as f:
            f.write(_row(5.0, ok=True))
        self.assertEqual(run.Herald._room_halt(fake, r), "budget")            # the bot's room too

    def test_room_guard_refuses_when_the_hold_does_not_fit(self):
        with open(self.costs, "w", encoding="utf-8") as f:
            f.write(_row(0.8, ok=True))                                       # 0.2 left, the next reply may cost 0.3
        should_stop, _ = run.room_guard(self.cfg)
        self.assertEqual(should_stop(), "budget")


class HoldReview(unittest.TestCase):
    """A second review of c78a20e."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.costs = os.path.join(self.tmp.name, "costs.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_hold_left_by_a_killed_process_expires(self):
        # reserve, then the process dies before settle: the hold used to count until midnight
        with open(self.costs, "w", encoding="utf-8") as f:
            f.write(_row(0.60, kind="reserve", id="dead-1", until=time.time() - 1))
        self.assertAlmostEqual(run.spent_today(self.costs)[0], 0.0)
        self.assertIsNotNone(run.reserve(self.costs, 0.60, 1.00, "turn"))

    def test_a_live_hold_still_counts(self):
        rid = run.reserve(self.costs, 0.60, 1.00, "turn")
        self.assertIsNotNone(rid)
        self.assertIsNone(run.reserve(self.costs, 0.60, 1.00, "turn"))

    def test_the_per_day_report_does_not_count_holds_as_turns(self):
        rid = run.reserve(self.costs, 0.60, 1.00, "turn")
        run.settle(self.costs, rid, 0.60, json.loads(_row(0.20, ok=True)))
        (_, usd, turns, _), = run.spent_by_day(self.costs, 1)
        self.assertAlmostEqual(usd, 0.20)
        self.assertEqual(turns, 1)

    def test_a_stale_lock_file_is_never_stolen_from_a_live_holder(self):
        # the old lock removed a «stale» file by mtime, so a second waiter could delete a fresh holder's lock
        inside, overlap, start = [0], [False], threading.Barrier(8)
        lock = self.costs + ".lock"
        open(lock, "w").close()
        os.utime(lock, (time.time() - 3600, time.time() - 3600))               # an old file: a crashed holder

        errors = []

        def worker():
            start.wait()
            try:
                for _ in range(20):
                    with run._LedgerLock(self.costs):
                        inside[0] += 1
                        if inside[0] > 1:
                            overlap[0] = True
                        time.sleep(0.001)
                        inside[0] -= 1
            except Exception as e:  # noqa: BLE001 — a thread's error must fail the test, not vanish
                errors.append(repr(e))

        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertFalse(overlap[0])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
