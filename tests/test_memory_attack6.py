"""Confirmed defects from a sixth independent attack (kind A: reachable without editing store files)."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import forum, learning, recall, team  # noqa: E402
from herald.memory import Store, sources  # noqa: E402
from herald.notebook import Notebook  # noqa: E402
from test_memory_attack4 import Env, line  # noqa: E402
from test_memory_attack5 import Team as Turns  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
BOT = "\U0001f916"


class Labels(Env):
    def closed_lines(self):
        return {"torn": '{"type":"user","sensitivity":"private","message":{"content":"x\n',
                "bom": "﻿" + line("x", sensitivity="private"),
                "cased": line("x", Sensitivity="private"),
                "nested": line("x", meta={"sensitivity": "private"})}

    def test_recall_a_label_it_cannot_read_closes_the_file(self):
        for name, first in self.closed_lines().items():
            with self.subTest(name):
                folder = self.session("a.jsonl", first, line("ZQREPLY words"), folder=name)
                db = os.path.join(self.d, name + ".sqlite")
                recall.index(db, folder)
                self.assertEqual(recall.search(db, "ZQREPLY"), [])

    def test_learning_a_label_it_cannot_read_closes_the_session(self):
        known = sources.load()
        for name, first in self.closed_lines().items():
            with self.subTest(name):
                folder = self.session("a.jsonl", first, line("y"), folder="l" + name)
                self.assertFalse(learning._session_open(os.path.join(folder, "a.jsonl"), known))

    def test_the_word_in_a_message_does_not_close_a_file(self):
        folder = self.session("a.jsonl", line("sensitivity of the ZQMODEL words"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQMODEL"))

    def test_a_deep_line_does_not_stop_learning(self):
        folder = self.session("a.jsonl", "[" * 200000 + "\n", line("y"))
        learning.analyze_many([os.path.join(folder, "a.jsonl")])


class Notes(Env):
    def nb(self):
        return Notebook(tempfile.mkdtemp(dir=self.d))

    def test_a_trailing_dot_cannot_replace_a_hidden_skill(self):
        nb = self.nb()
        folder = os.path.join(nb.dir, "skills", "abc")
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as out:
            out.write("---\nname: abc\ndescription: d\nsensitivity: private" "\n---\n\nZQKEEP\n")
        with self.assertRaises(ValueError):
            nb.add_skill("---\nname: abc.\ndescription: d\n---\n\n## Two\nB\n")
        self.assertIn("ZQKEEP", nb.skills(reader_scope="main")["abc"])

    def test_two_labels_the_closed_one_decides(self):
        nb = self.nb()
        with open(nb.mem_path, "w", encoding="utf-8") as out:
            out.write("[sensitivity: internal][sensitivity: private] ZQ\n")
        self.assertEqual(nb.memory(), [])

    def test_an_invisible_mark_inside_the_word_closes(self):
        for mark in ("️", "͏"):
            nb = self.nb()
            with open(nb.mem_path, "w", encoding="utf-8") as out:
                out.write("[sensi%stivity: private] ZQ\n" % mark)
            self.assertEqual(nb.memory(), [])

    def test_a_write_keeps_undecodable_bytes(self):
        nb = self.nb()
        with open(nb.mem_path, "wb") as out:
            out.write(b"[sensitivity: private] \xff\xfe hidden\n")
        nb.add_memory("new line")
        with open(nb.mem_path, "rb") as data:
            self.assertIn(b"\xff\xfe hidden", data.read())


class Recall(Env):
    def test_a_rewritten_line_is_read_again(self):
        folder = self.session("a.jsonl", line("ZQOLD words"))
        path = os.path.join(folder, "a.jsonl")
        recall.index(self.db, folder)
        with open(path, "w", encoding="utf-8") as out:
            out.write(line("ZQNEW words and a few more"))
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQOLD"), [])
        self.assertTrue(recall.search(self.db, "ZQNEW"))

    def test_an_append_is_not_a_restart(self):
        folder = self.session("a.jsonl", line("ZQFIRST words"))
        recall.index(self.db, folder)
        self.session("a.jsonl", line("ZQSECOND words"))
        self.assertEqual(recall.index(self.db, folder), 1)

    def test_a_file_name_that_cannot_be_encoded_is_skipped(self):
        folder = self.session("good.jsonl", line("ZQGOOD words"))
        try:
            with open(os.path.join(folder, "bad\ud800.jsonl"), "w", encoding="utf-8") as out:
                out.write(line("x"))
        except (OSError, UnicodeError):
            self.skipTest("this file system refuses the name")
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQGOOD"))

    def test_two_spellings_of_a_folder_are_one_folder(self):
        folder = self.session("a.jsonl", line("ZQONCE words"))
        recall.index(self.db, folder)
        recall.index(self.db, folder.replace("\\", "/"))
        self.assertEqual(len(recall.search(self.db, "ZQONCE")), 1)


class Roots(Env):
    def test_a_root_with_no_drive_is_a_broken_list(self):
        self.roots(("\\ZQpub", "public"))
        self.assertEqual(sources.load().roots, [])


class TeamWindow(Env):
    def test_a_window_does_not_hide_a_closed_session(self):
        folder, out = os.path.join(self.d, "p"), os.path.join(self.d, "out")
        os.makedirs(folder)
        path = os.path.join(folder, "a.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "custom-title", "customTitle": "zqrole"}) + "\n" + "".join(Turns.turns()))
        team.run(folder, out, min_sessions=1)
        self.assertTrue(os.path.exists(os.path.join(out, "zqrole.md")))
        self.roots((folder, "main-only"))
        old = time.time() - 7200
        os.utime(path, (old, old))
        team.run(folder, out, since_hours=1, min_sessions=1)
        self.assertFalse(os.path.exists(os.path.join(out, "zqrole.md")))


class Threads(Env):
    def test_a_thread_missing_from_the_store_is_closed_under_a_list(self):
        control = tempfile.mkdtemp(dir=self.d)
        with open(os.path.join(control, "tasks.json"), "w", encoding="utf-8") as out:
            json.dump({"tasks": {}}, out)
        forum.write_tail(control, "tp", {"done": "ZQ", "next": "n", "files": "f", "turn_at": "2026-11-08T10:00:00Z", "session": "s"})
        self.roots((control, "team"))
        self.assertEqual(forum.read_tail(control, "tp"), ("none", None))

    def test_without_a_list_a_missing_thread_stays_open(self):
        control = tempfile.mkdtemp(dir=self.d)
        with open(os.path.join(control, "tasks.json"), "w", encoding="utf-8") as out:
            json.dump({"tasks": {}}, out)
        forum.write_tail(control, "tp", {"done": "ZQ", "next": "n", "files": "f", "turn_at": "2026-11-08T10:00:00Z", "session": "s"})
        self.assertEqual(forum.read_tail(control, "tp")[0], "tail")


class Core(Base):
    def test_a_private_version_does_not_lend_its_date_to_history(self):
        mid = self.s.add(rec("x", valid_time={"from": "2026-01-01", "to": None}), "x")
        self.s.correct(mid, 1, {"sensitivity": "private", "valid_time": {"from": "2031-03-03T03:03:03Z", "to": None}}, "r", OP, **MAIN)
        self.s.correct(mid, 2, {"sensitivity": "internal"}, "r", OP, **MAIN)
        self.assertIsNone(self.s.history(mid)[0]["valid_time"]["to"])
        self.assertIsNotNone(self.s.history(mid, **MAIN)[0]["valid_time"]["to"])

    def test_a_symbol_the_index_keeps_can_be_searched(self):
        mid = self.s.add(rec("words " + BOT), "x")
        self.assertEqual(self.s.search(BOT)["hits"][0]["memory_id"], mid)

    def test_a_symbol_the_index_keeps_narrows_a_query(self):
        self.s.add(rec("zqshared"), "x")
        self.assertEqual(self.s.search("zqshared " + BOT)["outcome"], "not_found")

    def test_a_long_path_root_can_be_written(self):
        root = "\\\\?\\" + os.path.realpath(tempfile.mkdtemp())
        store = Store(root, clock=self.clock)
        self.addCleanup(store.close)
        mid = store.add(rec("zqlong"), "x")
        self.assertEqual(store.search("zqlong")["hits"][0]["memory_id"], mid)


if __name__ == "__main__":
    unittest.main()
