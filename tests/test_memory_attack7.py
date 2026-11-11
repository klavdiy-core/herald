"""Confirmed defects from a seventh independent attack (kind A)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import recall  # noqa: E402
from herald.memory import Store, sources  # noqa: E402
from herald.notebook import Notebook  # noqa: E402
from test_memory_attack4 import Env, line  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

GLUED = "\U0001f970"


class Fence(Env):
    def test_a_private_block_inside_a_public_line_is_closed(self):
        body = {"type": "user", "timestamp": "t", "sensitivity": "public",
                "message": {"content": [{"type": "text", "text": "ZQBLOCK words", "sensitivity": "private"}]}}
        folder = self.session("a.jsonl", json.dumps(body) + "\n")
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQBLOCK"), [])

    def test_a_cased_label_inside_a_public_line_is_closed(self):
        folder = self.session("a.jsonl", line("ZQCASED words", sensitivity="public", Sensitivity="private"))
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQCASED"), [])

    def test_a_plain_public_line_beside_a_closed_one_stays(self):
        folder = self.session("a.jsonl", line("ZQPLAIN words", sensitivity="public"), line("x", sensitivity="private"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQPLAIN"))


class Recall(Env):
    def test_an_exact_glued_word_is_found(self):
        word = "ZQWORD" + GLUED + "END"
        folder = self.session("a.jsonl", line(word + " tail"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, word))

    def test_a_bom_does_not_lose_the_first_line(self):
        folder = self.session("a.jsonl", "﻿" + line("ZQBOM words"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQBOM"))

    def test_a_list_saved_with_a_bom_is_read(self):
        path = os.path.join(self.d, "sources.json")
        with open(path, "w", encoding="utf-8-sig") as out:
            json.dump({"roots": [{"path": self.d, "class": "team"}]}, out)
        os.environ[sources.ENV] = path
        self.assertEqual(sources.load().class_of(os.path.join(self.d, "x")), "team")

    def test_the_folder_in_another_case_is_the_same_folder(self):
        folder = self.session("a.jsonl", line("ZQCASE words"))
        recall.index(self.db, folder)
        recall.index(self.db, folder.upper())
        self.assertEqual(len(recall.search(self.db, "ZQCASE")), 1)

    def test_an_appended_labelled_line_is_not_a_restart(self):
        folder = self.session("a.jsonl", line("ZQA words", sensitivity="public"))
        recall.index(self.db, folder)
        self.session("a.jsonl", line("ZQB words", sensitivity="public"))
        self.assertEqual(recall.index(self.db, folder), 1)

    def test_a_newly_closing_line_still_restarts(self):
        folder = self.session("a.jsonl", line("ZQEARLY words"))
        recall.index(self.db, folder)
        self.session("a.jsonl", line("x", sensitivity="private"))
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQEARLY"), [])


class Notes(Env):
    def nb(self):
        return Notebook(tempfile.mkdtemp(dir=self.d))

    def test_a_skill_merge_keeps_undecodable_bytes(self):
        nb = self.nb()
        folder = os.path.join(nb.dir, "skills", "s")
        os.makedirs(folder)
        path = os.path.join(folder, "SKILL.md")
        with open(path, "wb") as out:
            out.write(b"---\nname: s\ndescription: d\n---\n\n## One\n\xff\xfe\n")
        nb.add_skill("---\nname: s\ndescription: d\n---\n\n## Two\nB\n")
        with open(path, "rb") as data:
            self.assertIn(b"\xff\xfe", data.read())

    def test_a_lone_surrogate_does_not_stop_a_write(self):
        nb = self.nb()
        nb.add_memory("zq \ud83d tail")
        self.assertEqual(len(nb.memory()), 1)
        nb.set_pending([{"kind": "memory", "draft": "zq \ud83d"}])
        self.assertEqual(len(nb.pending()), 1)
        self.assertFalse(os.path.exists(nb.pending_path + ".tmp"))


class Core(Base):
    def test_a_lost_database_is_refused_while_evidence_exists(self):
        self.s.put_evidence(b"bytes", "a.md", "tool_output", {"id": "w", "role": "w"})
        self.s.close()
        db = os.path.join(self.root, "memory.sqlite")
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    open(db, "wb").close()
                else:
                    os.rename(db, db + ".away")
                with self.assertRaises(RuntimeError):
                    Store(self.root, clock=self.clock)
                self.assertEqual(len(os.listdir(os.path.join(self.root, "archive"))), 1)
                if not empty:
                    os.rename(db + ".away", db)
                    self.s = Store(self.root, clock=self.clock)
                    self.s.close()
        os.remove(db)
        self.s = None

    def tearDown(self):
        if self.s is not None:
            super().tearDown()

    def test_a_document_without_a_label_does_not_stop_the_open(self):
        import sqlite3
        self.s.add(rec("x"), "x")
        self.s.close()
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        doc = json.loads(con.execute("SELECT doc FROM versions").fetchone()[0])
        del doc["sensitivity"]
        con.execute("UPDATE versions SET doc=?", (json.dumps(doc),))
        con.commit()
        con.close()
        os.remove(os.path.join(self.root, "lexical_main.sqlite"))
        self.s = Store(self.root, clock=self.clock)
        self.assertTrue(self.s.verify(reader_scope="main"))

    def test_a_fresh_store_opens(self):
        root = tempfile.mkdtemp()
        Store(root).close()
        Store(root).close()

    def test_a_word_after_many_symbol_chunks_can_be_written(self):
        mid = self.s.add(rec("| " * 300 + "zqafter"), "x")
        self.assertEqual(self.s.search("zqafter")["hits"][0]["memory_id"], mid)


if __name__ == "__main__":
    unittest.main()
