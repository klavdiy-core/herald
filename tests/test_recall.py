import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import notebook, recall, run, speak_first  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_learning import T, Transcript  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def sessions(folder):
    os.makedirs(folder, exist_ok=True)
    a = Transcript()
    a.user("which database did we pick for the shop?", T(0))
    a.say("We picked SQLite for the shop because it needs no server.", T(1))
    b = Transcript()
    b.user("напомни, как зовут кота в истории", T(0, h=11))
    b.say("Кота в истории зовут Пуговица.", T(1, h=11))
    b.tool("Bash", {"command": "cat secrets.txt"}, "TOKEN=abc123", T(2, h=11))     # tool output is not indexed
    for name, t in (("s-a", a), ("s-b", b)):
        os.replace(t.write(folder), os.path.join(folder, f"{name}.jsonl"))


class Index(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.tmp.name, "proj")
        sessions(self.proj)
        self.db = os.path.join(self.tmp.name, "recall.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_what_was_said_in_an_earlier_session(self):
        recall.index(self.db, self.proj)
        hits = recall.search(self.db, "database shop")
        self.assertEqual(hits[0]["session"], "s-a")
        self.assertIn("SQLite", hits[0]["text"])

    def test_russian_words_any_case(self):
        recall.index(self.db, self.proj)
        hits = recall.search(self.db, "КОТА")
        self.assertTrue(any("Пуговица" in h["text"] for h in hits))

    def test_a_message_sent_mid_turn_is_found(self):
        with open(os.path.join(self.proj, "s-a.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "attachment", "timestamp": T(9),
                                "attachment": {"type": "queued_command", "prompt": "use pastel colours"}}) + "\n")
            f.write(json.dumps({"type": "attachment", "timestamp": T(9),
                                "attachment": {"type": "queued_command", "prompt": "<task-notification> x"}}) + "\n")
        recall.index(self.db, self.proj)
        self.assertTrue(recall.search(self.db, "pastel"))
        self.assertEqual(recall.search(self.db, "notification"), [])

    def test_tool_output_is_not_indexed(self):
        recall.index(self.db, self.proj)
        self.assertEqual(recall.search(self.db, "abc123"), [])

    def test_indexing_again_adds_only_what_is_new(self):
        self.assertEqual(recall.index(self.db, self.proj), 4)
        self.assertEqual(recall.index(self.db, self.proj), 0)
        with open(os.path.join(self.proj, "s-a.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": T(5), "message": {"content": "and the shop logo?"}}) + "\n")
        self.assertEqual(recall.index(self.db, self.proj), 1)
        self.assertTrue(any("logo" in h["text"] for h in recall.search(self.db, "logo")))

    def test_a_rewritten_file_replaces_its_old_messages(self):
        # two outside reviews (25.10): a file that shrank was read again from the start, but its old rows stayed -
        # duplicates, and words that are no longer in the session still found
        recall.index(self.db, self.proj)
        t = Transcript()
        t.user("which database did we pick for the shop?", T(0))
        t.say("Postgres, after all.", T(1))
        os.replace(t.write(self.tmp.name), os.path.join(self.proj, "s-a.jsonl"))     # rewritten, and shorter
        recall.index(self.db, self.proj)
        self.assertEqual(recall.search(self.db, "SQLite"), [])                        # gone from the session
        self.assertEqual(len([h for h in recall.search(self.db, "database shop") if h["session"] == "s-a"]), 1)

    def test_search_syntax_cannot_break_the_query(self):
        recall.index(self.db, self.proj)
        for q in ('"', "shop OR", "NEAR(", "*", "a AND NOT b", "'; DROP TABLE x; --", ""):
            recall.search(self.db, q)                       # no exception

    def test_a_missing_folder_indexes_nothing(self):
        self.assertEqual(recall.index(self.db, os.path.join(self.tmp.name, "nope")), 0)
        self.assertFalse(os.path.exists(self.db))                 # and leaves no empty index behind


class Telegram(unittest.TestCase):
    def test_recall_in_the_chat(self):
        with tempfile.TemporaryDirectory() as d:
            control, ws = os.path.join(d, "c"), os.path.join(d, "w")
            os.makedirs(control)
            os.makedirs(ws)
            root = os.path.join(d, "home")
            sessions(os.path.join(root, "projects", notebook.mangle(ws)))
            bot = FakeBot()
            gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(control, "s.json"),
                                    os.path.join(control, "STOP"))
            h = run.Herald(bot, lambda *a, **k: TurnResult(True, "ok", "s", 0.0), EngineConfig(), gate, [],
                           control, ws, {OWNER}, transcript_roots=[root])
            h.handle(text("/recall database"))
            self.assertIn("SQLite", bot.texts[-1][1])
            h.handle(text("/recall zzzz-nothing"))
            self.assertIn("nothing", bot.texts[-1][1].lower())
            h.handle(text("/recall"))
            self.assertIn("/recall", bot.texts[-1][1])


class Cli(unittest.TestCase):
    def test_recall_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "proj")
            sessions(proj)
            r = subprocess.run([sys.executable, "-m", "herald", "recall", proj, "--query", "database",
                                "--db", os.path.join(d, "r.db")], cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("SQLite", r.stdout)


if __name__ == "__main__":
    unittest.main()
