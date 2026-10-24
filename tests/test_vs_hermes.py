"""Bugs a side-by-side evaluation with Hermes Agent confirmed, 25.10. Each test is red on 0.8.0."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import agents, decisions, learning, room, run, speak_first, team  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_learning import T, Transcript  # noqa: E402
from test_room import Voice  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control, self.ws = os.path.join(self.tmp.name, "c"), os.path.join(self.tmp.name, "w")
        os.makedirs(self.control)
        os.makedirs(self.ws)
        self.bot = FakeBot()

    def tearDown(self):
        self.tmp.cleanup()

    def herald(self, voices=None, cap=None, engine=None):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        return run.Herald(self.bot, engine or (lambda *a, **k: TurnResult(True, "ok", "s", 0.0)), EngineConfig(),
                          gate, [], self.control, self.ws, {OWNER}, transcript_roots=[], max_usd_per_day=cap,
                          room_agents=voices, room_sync=True)


class StoppingVoice(Voice):
    """Raises the STOP file (as /stop from another thread would) after its first reply."""

    def __init__(self, name, lines, stop_path, cost=0.01):
        super().__init__(name, lines, cost)
        self.stop_path = stop_path

    def speak(self, prompt):
        out = super().speak(prompt)
        open(self.stop_path, "w").close()
        return out


class Room(Base):
    def test_stop_halts_a_room_that_is_already_talking(self):
        stopper = StoppingVoice("Raven", ["one", "two", "three", "four"], os.path.join(self.control, "STOP"))
        h = self.herald({"Raven": stopper, "Owl": Voice("Owl", ["a", "b", "c"])})
        h.handle(text("/room t"))
        self.assertEqual(len(stopper.prompts) + len(h.room_agents["Owl"].prompts), 1)
        self.assertEqual(self.bot.buttons, [])                      # no summary turn after a stop
        self.assertIn("stopped", self.bot.texts[-1][1].lower())

    def test_the_daily_cap_holds_inside_a_room(self):
        voices = {"Raven": Voice("Raven", ["x"] * 9, cost=0.05), "Owl": Voice("Owl", ["y"] * 9, cost=0.05)}
        h = self.herald(voices, cap=0.1)
        h.handle(text("/room t"))
        spoken = len(voices["Raven"].prompts) + len(voices["Owl"].prompts)
        self.assertEqual(spoken, 2)                                 # 0.05 + 0.05 reaches the cap
        self.assertIn("daily budget", self.bot.texts[-1][1].lower())
        with open(os.path.join(self.control, "costs.jsonl"), encoding="utf-8") as f:
            self.assertAlmostEqual(sum(json.loads(l)["usd"] for l in f), 0.1, places=4)

    def test_a_forced_speaker_does_not_count_as_a_round(self):
        r = room.Room("t", [room.Seat(Voice("A", ["a"]), "ideas"), room.Seat(Voice("B", ["b"]), "ideas")])
        r.next_speaker("B")
        r.step()
        self.assertEqual(r.rounds, 0)


class Money(Base):
    def test_a_turn_never_gets_more_budget_than_today_has_left(self):
        seen = []

        def engine(prompt, ws, cfg, session_id=None):
            seen.append(cfg.max_budget_usd)
            return TurnResult(True, "ok", "s", 0.45)
        h = self.herald(cap=0.5, engine=engine)
        h.handle(text("one"))
        h.handle(text("two"))
        self.assertLessEqual(seen[1], 0.05 + 1e-9)

    def test_caps_must_be_numbers(self):
        p = os.path.join(self.tmp.name, "cfg.json")
        for bad in ({"max_usd_per_day": "0.5"}, {"max_turns": "20"}, {"room": {"budget_usd": "x"}},
                    {"max_usd_per_day": True}):
            with open(p, "w") as f:
                json.dump({"owners": [1], "workspace": self.ws, "control": self.control, **bad}, f)
            with self.assertRaises(ValueError, msg=bad):
                run.load_config(p)


class Boundaries(Base):
    def test_command_agents_do_not_see_herald_secrets(self):
        script = os.path.join(self.tmp.name, "env.py")
        with open(script, "w") as f:
            f.write("import os; print(os.environ.get('HERALD_TELEGRAM_TOKEN', 'none'), "
                    "os.environ.get('TYPESAFE_API_KEY', 'none'), os.environ.get('MY_CLI_KEY', 'none'))")
        os.environ.update(HERALD_TELEGRAM_TOKEN="123:SECRET", TYPESAFE_API_KEY="ts-SECRET", MY_CLI_KEY="cli-ok")
        try:
            a = agents.from_config({"name": "Ext", "kind": "command", "command": ["python", script],
                                    "pass_env": ["MY_CLI_KEY"]}, self.tmp.name)
            self.assertEqual(a.speak("hi")[0], "none none cli-ok")
        finally:
            for k in ("HERALD_TELEGRAM_TOKEN", "TYPESAFE_API_KEY", "MY_CLI_KEY"):
                os.environ.pop(k)

    def test_learn_survives_malformed_records(self):
        p = os.path.join(self.tmp.name, "bad.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for rec in ({"type": "user", "message": "just a string"},
                        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "Bash",
                                                                       "input": ["not", "a", "dict"]}]}},
                        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1",
                                                                   "is_error": True, "content": "boom"}]}},
                        {"type": "user", "message": {"content": [{"type": "text", "text": None}]}},
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": None}]}},
                        {"type": "assistant", "message": {"content": None}}, ["a", "list"]):
                f.write(json.dumps(rec) + "\n")
        learning.analyze(p)                                        # no exception

    def test_team_keeps_files_inside_out_whatever_the_role_is_called(self):
        proj, out = os.path.join(self.tmp.name, "p"), os.path.join(self.tmp.name, "out")
        os.makedirs(proj)
        for i, title in enumerate(("../escape", "a/b", "x:y", ["l"])):
            t = Transcript()
            t.tool("Bash", {"command": "make"}, "error: boom", T(1), True)
            t.recs.append({"type": "custom-title", "customTitle": title})
            os.replace(t.write(self.tmp.name), os.path.join(proj, f"s{i}.jsonl"))
        team.run(proj, out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "escape.md")))
        for name in os.listdir(out):
            self.assertTrue(os.path.isfile(os.path.join(out, name)))

    def test_a_damaged_decision_record_does_not_crash_tick_or_taps(self):
        d = decisions.Decisions(os.path.join(self.control, "decisions.json"), lambda: 10.0)
        with open(d.path, "w") as f:
            json.dump({"abcd1234": {"text": "x"}}, f)
        self.assertEqual(d.expire_due(), [])
        self.assertEqual(d.tap("abcd1234", 0)[0], "unknown")

    def test_private_masks_typed_words_with_digits_glued_to_letters(self):
        t = Transcript()
        t.user("deploy to prod-ALPHA47 with token Tk9zz87", T(0))
        for k in (1, 5):
            t.tool("Bash", {"command": "deploy"}, "error: host prod-ALPHA47 refused token Tk9zz87", T(k), True)
        md = learning.render_md(learning.propose(learning.analyze(t.write(self.tmp.name)), private=True))
        for leak in ("ALPHA4", "Tk9zz8", "ALPHA47", "Tk9zz87"):
            self.assertNotIn(leak, md)


if __name__ == "__main__":
    unittest.main()
