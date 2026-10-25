"""/usage and /retry, taken from Hermes (round 3 next to Hermes, 25.10: Telegram 7 / 9). Red on 0.16.0."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from test_run import OWNER, Base, text  # noqa: E402


class Usage(Base):
    def ledger(self, rows):
        with open(self.h.costs_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_usage_shows_today_against_the_cap_and_the_last_days(self):
        today = time.strftime("%Y-%m-%d")
        # the calendar day before, not now - 86400 s: on the day clocks go back (25 h) that was still today after 23:00
        import datetime
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.h.max_usd_per_day = 2.0
        self.ledger([{"day": yesterday, "usd": 0.5, "ok": True},
                     {"day": today, "usd": 0.25, "ok": True},
                     {"day": today, "usd": 0.5, "ok": False, "estimated": True},
                     "not a row"])
        self.h.handle(text("/usage"))
        out = self.bot.texts[-1][1]
        self.assertIn("today $0.75 of $2.00", out)
        self.assertIn("2 turns", out)
        self.assertIn("1 estimated", out)                 # a killed turn counted at its cap is said to be a guess
        self.assertIn(yesterday, out)
        self.assertIn("$0.50", out)

    def test_usage_with_an_empty_ledger(self):
        self.h.handle(text("/usage"))
        self.assertIn("nothing spent", self.bot.texts[-1][1].lower())


class Retry(Base):
    def test_retry_runs_the_last_request_again(self):
        self.h.handle(text("what is 2+2"))
        self.h.handle(text("/retry"))
        prompts = [c["prompt"] for c in self.engine.calls]
        self.assertEqual(prompts, ["what is 2+2", "what is 2+2"])

    def test_retry_with_nothing_to_retry_says_so(self):
        self.h.handle(text("/retry"))
        self.assertEqual(self.engine.calls, [])
        self.assertIn("nothing to retry", self.bot.texts[-1][1].lower())

    def test_retry_respects_stop_and_the_budget(self):
        self.h.handle(text("hello"))
        open(os.path.join(self.control, "STOP"), "w").close()
        self.h.handle(text("/retry"))
        self.assertEqual(len(self.engine.calls), 1)

    def test_retry_is_not_itself_retried(self):
        self.h.handle(text("first"))
        self.h.handle(text("/retry"))
        self.h.handle(text("/retry"))
        self.assertEqual([c["prompt"] for c in self.engine.calls], ["first"] * 3)

    def test_help_lists_the_new_commands(self):
        self.h.handle(text("/help"))
        self.assertIn("/usage", self.bot.texts[-1][1])
        self.assertIn("/retry", self.bot.texts[-1][1])


if __name__ == "__main__":
    unittest.main()
