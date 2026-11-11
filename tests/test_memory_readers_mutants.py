"""Tests for what the reader mutants of the fourth round left alive."""
import json
import os
import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import forum, recall  # noqa: E402
from herald.memory import sources  # noqa: E402
from herald.notebook import Notebook  # noqa: E402
from test_memory_attack4 import Env, line  # noqa: E402


class Recall(Env):
    def old_database(self):
        os.makedirs(os.path.dirname(self.db))
        with sqlite3.connect(self.db) as con:
            con.executescript("""CREATE TABLE files (path TEXT PRIMARY KEY, lines INTEGER, size INTEGER);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session TEXT, role TEXT, ts TEXT, text TEXT);
                CREATE VIRTUAL TABLE fts USING fts5(text, content='messages', content_rowid='id');""")

    def test_an_old_index_can_be_indexed_again(self):
        self.old_database()
        folder = self.session("a.jsonl", line("ZQFRESH words"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQFRESH"))

    def test_a_decomposed_text_is_found_by_a_composed_query(self):
        word = "йодза"
        folder = self.session("a.jsonl", line(unicodedata.normalize("NFD", word)))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, word))

    def test_the_following_message_obeys_the_scope(self):
        folder = self.session("a.jsonl", line("ZQPUBLIC words", sensitivity="public"),
                              line("ZQHIDDENNEXT words", role="model", sensitivity="private"))
        recall.index(self.db, folder, reader_scope="main")
        hit = recall.search(self.db, "ZQPUBLIC")[0]
        self.assertNotIn("ZQHIDDENNEXT", hit["text"])
        self.assertIn("ZQHIDDENNEXT", recall.search(self.db, "ZQPUBLIC", reader_scope="main")[0]["text"])

    def test_a_root_closed_later_is_restamped_in_a_main_database(self):
        folder = self.session("a.jsonl", line("ZQSTAMP words"))
        recall.index(self.db, folder, reader_scope="main")
        self.assertTrue(recall.search(self.db, "ZQSTAMP"))
        self.roots((folder, "main-only"))
        recall.index(self.db, folder, reader_scope="main")
        self.assertEqual(recall.search(self.db, "ZQSTAMP"), [])
        self.assertTrue(recall.search(self.db, "ZQSTAMP", reader_scope="main"))

    def test_a_public_line_with_a_bom_stays_public(self):
        folder = self.session("a.jsonl", "\ufeff" + line("ZQPUBBOM words", sensitivity="public"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQPUBBOM"))

    def test_a_file_this_run_could_not_read_leaves_the_index(self):
        folder = self.session("a.jsonl", line("ZQUNSEEN words"))
        recall.index(self.db, folder)
        real = sources.load()
        with mock.patch.object(type(real), "scope_of", lambda s, p, label=sources.UNLABELLED: "main"):
            recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQUNSEEN"), [])

    def test_a_closed_files_name_does_not_reach_a_workers_index(self):
        folder = self.session("zqname.jsonl", line("x"))
        self.roots((folder, "main-only"))
        recall.index(self.db, folder)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM files").fetchone()[0], 0)


class Skills(Env):
    def write(self, nb, name, text):
        folder = os.path.join(nb.dir, "skills", name)
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as out:
            out.write(text)

    def nb(self):
        return Notebook(tempfile.mkdtemp(dir=self.d))

    def test_a_public_label_with_a_comment_or_quotes_is_public(self):
        for head in ("sensitivity: public # note", "sensitivity: 'public'", 'sensitivity: "public"'):
            with self.subTest(head):
                nb = self.nb()
                self.write(nb, "s", "---\nname: s\n%s\n---\n\nbody\n" % head)
                self.assertIn("s", nb.skills())

    def test_a_label_line_without_a_colon_is_closed(self):
        nb = self.nb()
        self.write(nb, "s", "---\nname: s\nsensitivity private" "\n---\n\nbody\n")
        self.assertEqual(nb.skills(), {})

    def test_a_bom_does_not_send_the_label_search_into_the_body(self):
        nb = self.nb()
        self.write(nb, "s", "﻿---\nname: s\nsensitivity: public\n---\n\nsensitivity: private is a word here\n")
        self.assertIn("s", nb.skills())

    def test_a_public_draft_cannot_replace_a_hidden_skill(self):
        nb = self.nb()
        self.write(nb, "s", "---\nname: s\ndescription: d\nsensitivity: private" "\n---\n\n## One\nZQKEEP\n")
        with self.assertRaises(ValueError):
            nb.add_skill("---\nname: s\ndescription: d\n---\n\n## Two\nB\n")
        self.assertIn("ZQKEEP", nb.skills(reader_scope="main")["s"])

    def test_a_worker_cannot_forget_a_hidden_skill(self):
        nb = self.nb()
        self.write(nb, "s", "---\nname: s\nsensitivity: private" "\n---\n\nbody\n")
        with self.assertRaises(KeyError):
            nb.forget_skill("s")
        self.assertIn("s", nb.skills(reader_scope="main"))
        nb.forget_skill("s", reader_scope="main")
        self.assertNotIn("s", nb.skills(reader_scope="main"))


class ForumLabels(Env):
    def test_a_public_label_does_not_outvote_a_private_one(self):
        control = tempfile.mkdtemp(dir=self.d)
        with open(os.path.join(control, "tasks.json"), "w", encoding="utf-8") as out:
            json.dump({"tasks": {"tp": {"sensitivity": "public", "meta": {"sensitivity": "private"}}}}, out)
        self.assertFalse(forum.thread_open(control, "tp"))


class ForumMain(unittest.TestCase):
    def test_a_main_turn_in_a_closed_thread_lists_only_what_it_wrote(self):
        control = tempfile.mkdtemp()
        with open(os.path.join(control, "tasks.json"), "w", encoding="utf-8") as out:
            json.dump({"tasks": {"tp": {"sensitivity": "private"}}}, out)
        f = object.__new__(forum.Forum)
        f.control, f.tasks_root = control, os.path.join(control, "ws", "tasks")
        f._vetoed, f.her_stop_path_value = set(), os.path.join(control, "HERSTOP")
        f.chat_id, f.stop_path, f.group, f.path = 1, os.path.join(control, "STOP"), None, os.path.join(control, "tasks.json")
        f._put = lambda data, keep_prev=True: None
        f._say = lambda thread, text: None
        h = mock.MagicMock()
        h._over_budget.return_value = False
        h._turn.return_value = SimpleNamespace(ok=True, text="ANSWER", session_id="S1", error=None)
        h.now.return_value = 1762600000.0
        h.workspace, h.control = control, control
        f.workspace = control
        f.h = h
        real = os.path.join(f.tasks_root, "tp")
        os.makedirs(real)
        with open(os.path.join(real, "old.txt"), "w", encoding="utf-8") as out:
            out.write("x")
        task = {"name": "n", "session_id": "S0"}
        f._turn_in("tp", "hello", {"tasks": {"tp": task}}, task, reader_scope="main")
        source, text = forum.read_tail(control, "tp", reader_scope="main")
        self.assertEqual(source, "tail")
        self.assertNotIn("old.txt", text)


if __name__ == "__main__":
    unittest.main()
