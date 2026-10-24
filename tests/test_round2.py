"""Bugs the second side-by-side evaluation with Hermes confirmed, 25.10. Each test is red on 0.10.0."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import recall, room, room_web, run, speak_first  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_room import Voice  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class WebRoomGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spent, self.stop = [], [None]
        self.voices = {"A": Voice("A", ["a"] * 30, cost=0.03)}
        self.srv = room_web.serve(self.voices, record_dir=self.tmp.name, port=0, pace_s=0.01,
                                  should_stop=lambda: self.stop[0], on_spend=self.spent.append, default_budget=0.05)
        self.base, self.tok = f"http://127.0.0.1:{self.srv.server_port}", self.srv.token

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path + "?t=" + self.tok, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10).read()

    def events(self):
        with urllib.request.urlopen(self.base + "/api/events?since=0&t=" + self.tok, timeout=10) as r:
            return json.loads(r.read())["events"]

    def wait(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if pred(self.events()):
                return
            time.sleep(0.05)
        self.fail("timed out")

    def start(self, budget=None):
        body = {"topic": "t", "format": "brainstorm", "seats": [{"name": "A", "role": "ideas"}]}
        if budget is not None:
            body["budget_usd"] = budget
        return self.post("/api/start", body)

    def test_without_a_budget_the_default_one_holds_and_spend_reaches_the_ledger(self):
        self.start()
        self.wait(lambda ev: any(e["type"] == "budget" for e in ev))
        self.assertEqual(len(self.voices["A"].prompts), 2)          # 0.03, 0.06: the default 0.05 stops the third
        self.assertAlmostEqual(sum(self.spent), 0.06, places=4)

    def test_stop_and_the_daily_cap_halt_the_browser_room(self):
        self.stop[0] = "budget"
        self.start(budget=1.0)
        self.wait(lambda ev: any(e["type"] == "halted" for e in ev))
        self.assertEqual(self.voices["A"].prompts, [])

    def test_only_one_room_at_a_time(self):
        self.start(budget=1.0)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.start(budget=1.0)
        self.assertEqual(e.exception.code, 400)
        self.post("/api/control", {"action": "end"})
        self.wait(lambda ev: any(e["type"] == "summary" for e in ev))
        self.start(budget=1.0)                                      # a closed room makes way for a new one


class Guard(unittest.TestCase):
    def test_the_browser_room_uses_the_configs_stop_cap_and_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w") as f:
                json.dump({"owners": [1], "workspace": os.path.join(d, "w"), "control": os.path.join(d, "c"),
                           "max_usd_per_day": 0.1}, f)
            cfg = run.load_config(p)
            os.makedirs(cfg["control"])
            should_stop, on_spend = run.room_guard(cfg)
            self.assertIsNone(should_stop())
            on_spend(0.06)
            on_spend(0.05)
            self.assertEqual(should_stop(), "budget")
            with open(os.path.join(cfg["control"], "costs.jsonl"), encoding="utf-8") as f:
                rows = [json.loads(l) for l in f]
            self.assertEqual([r["kind"] for r in rows], ["room", "room"])
            open(os.path.join(cfg["control"], "STOP"), "w").close()
            self.assertEqual(should_stop(), "stop")


class Inputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control, self.ws = os.path.join(self.tmp.name, "c"), os.path.join(self.tmp.name, "w")
        os.makedirs(self.control)
        os.makedirs(self.ws)
        self.bot = FakeBot()

    def tearDown(self):
        self.tmp.cleanup()

    def herald(self, cap=None):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        return run.Herald(self.bot, lambda *a, **k: TurnResult(True, "hello", "s", 0.0), EngineConfig(), gate, [],
                          self.control, self.ws, {OWNER}, transcript_roots=[], max_usd_per_day=cap)

    def test_owners_written_as_strings_still_work(self):
        p = os.path.join(self.tmp.name, "cfg.json")
        with open(p, "w") as f:
            json.dump({"owners": ["1001"], "workspace": self.ws, "control": self.control}, f)
        self.assertEqual(run.load_config(p)["owners"], [1001])
        with open(p, "w") as f:
            json.dump({"owners": ["me"], "workspace": self.ws, "control": self.control}, f)
        with self.assertRaises(ValueError):
            run.load_config(p)

    def test_a_damaged_ledger_row_is_skipped(self):
        with open(os.path.join(self.control, "costs.jsonl"), "w") as f:
            f.write(json.dumps({"day": time.strftime("%Y-%m-%d"), "usd": "lots"}) + "\n")
        self.herald(cap=1.0).handle(text("hi"))
        self.assertEqual(self.bot.texts[-1][1], "hello")

    def test_a_superscript_digit_is_not_a_number(self):
        self.herald().handle(text("/approve ²"))
        self.assertIn("no proposal", self.bot.texts[-1][1])

    def test_a_room_agent_that_raises_does_not_take_the_room_down(self):
        class Broken:
            name = "B"

            def speak(self, prompt):
                raise RuntimeError("boom")
        r = room.Room("t", [room.Seat(Broken(), "ideas")])
        r.round()
        self.assertIn("could not answer", r.messages[-1]["text"])


class Recall(unittest.TestCase):
    def test_a_half_written_line_is_read_once_it_is_complete(self):
        with tempfile.TemporaryDirectory() as d:
            p, db = os.path.join(d, "s.jsonl"), os.path.join(d, "r.db")
            line = json.dumps({"type": "user", "timestamp": "2026-10-25T01:00:00Z",
                               "message": {"content": "giraffes are tall"}})
            with open(p, "w", encoding="utf-8") as f:
                f.write(line[:30])                                  # Claude Code is still writing it
            recall.index(db, d)
            with open(p, "w", encoding="utf-8") as f:
                f.write(line + "\n")
            recall.index(db, d)
            self.assertTrue(recall.search(db, "giraffes"))

    def test_a_lone_surrogate_does_not_stop_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            p, db = os.path.join(d, "s.jsonl"), os.path.join(d, "r.db")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"type": "user", "message": {"content": "bad \\ud83d here"}}\n')
                f.write(json.dumps({"type": "user", "message": {"content": "zebras are striped"}}) + "\n")
            recall.index(db, d)
            self.assertTrue(recall.search(db, "zebras"))


class CliErrors(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "herald", *args], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8")

    def test_plain_errors_not_tracebacks(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as f:
                f.write("{oops")
            for args in (("learn", os.path.join(d, "nope.jsonl")), ("learn", d), ("doctor", "--config", bad)):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 2, args)
                self.assertNotIn("Traceback", r.stderr, args)
                self.assertIn("herald:", r.stderr, args)


if __name__ == "__main__":
    unittest.main()
