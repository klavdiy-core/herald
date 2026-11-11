"""Confirmed defects from a fifth independent attack on the memory core and its readers."""
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
from herald import forum, recall, team  # noqa: E402
from herald.memory import Store, sources  # noqa: E402
from herald.notebook import Notebook  # noqa: E402
from test_memory_attack4 import Env, line  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}


class Notes(Env):
    def nb(self):
        return Notebook(tempfile.mkdtemp(dir=self.d))

    def raw(self, nb, text):
        with open(nb.mem_path, "w", encoding="utf-8") as out:
            out.write(text + "\n")

    def test_every_spelling_of_a_closed_entry_stays_closed(self):
        for entry in ("- [sensitivity: private] ZQ", "[sensitivity=private] ZQ", "​[sensitivity: private] ZQ",
                      "[sensitivity private] ZQ", "sensitivity: private ZQ", "[sensitivity：private] ZQ",
                      "**[sensitivity: private]** ZQ", "- sensi​tivity: private ZQ"):
            with self.subTest(entry):
                nb = self.nb()
                self.raw(nb, entry)
                self.assertEqual(nb.memory(), [])

    def test_a_note_through_the_app_keeps_its_public_label(self):
        nb = self.nb()
        self.raw(nb, "- [sensitivity: internal] ZQ")
        self.assertEqual(len(nb.memory()), 1)

    def test_a_note_with_no_label_stays_open(self):
        nb = self.nb()
        self.raw(nb, "- a plain lesson ZQ")
        self.assertEqual(len(nb.memory()), 1)

    def test_a_later_line_does_not_decide(self):
        nb = self.nb()
        self.raw(nb, "a plain lesson\nsensitivity is a word here")
        self.assertEqual(len(nb.memory()), 1)

    def test_every_yaml_form_of_a_skill_label_stays_closed(self):
        for head in ("'sensitivity': private", '"sensitivity": private', "{sensitivity: private}", "- sensitivity: private",
                     "sensi​tivity: private"):
            with self.subTest(head):
                nb = self.nb()
                folder = os.path.join(nb.dir, "skills", "s")
                os.makedirs(folder)
                with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as out:
                    out.write("---\nname: s\n%s\n---\n\nbody\n" % head)
                self.assertEqual(nb.skills(), {})

    def test_undecodable_files_do_not_stop_the_prompt(self):
        nb = self.nb()
        with open(nb.mem_path, "wb") as out:
            out.write(b"\xff\xfe plain\n")
        folder = os.path.join(nb.dir, "skills", "s")
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "wb") as out:
            out.write(b"---\nname: s\nsensitivity: private" b"\n---\n\n\xff\n")
        self.assertNotIn("sensitivity: private", nb.compose("P"))

    def test_overlapping_separators_cannot_split_an_entry(self):
        nb = self.nb()
        nb.add_memory("[sensitivity:private] one\n§\n§\nZQTAIL")
        self.assertEqual(nb.memory(), [])

    def test_writes_reach_the_disk(self):
        nb = self.nb()
        with mock.patch("os.fsync") as sync:
            nb.add_memory("x")
        self.assertTrue(sync.called)


class Recall(Env):
    def hits(self, word):
        return recall.search(self.db, word)

    def test_a_deleted_file_leaves_the_index(self):
        folder = self.session("a.jsonl", line("ZQGONE words"))
        recall.index(self.db, folder)
        os.remove(os.path.join(folder, "a.jsonl"))
        recall.index(self.db, folder)
        self.assertEqual(self.hits("ZQGONE"), [])

    def test_a_file_moved_under_a_closed_root_leaves_the_index(self):
        top = os.path.join(self.d, "top")
        opened, closed = os.path.join(top, "open"), os.path.join(top, "closed")
        os.makedirs(opened)
        os.makedirs(closed)
        self.roots((opened, "team"), (closed, "main-only"))
        with open(os.path.join(opened, "a.jsonl"), "w", encoding="utf-8") as out:
            out.write(line("ZQMOVED words"))
        recall.index(self.db, top)
        self.assertTrue(self.hits("ZQMOVED"))
        os.replace(os.path.join(opened, "a.jsonl"), os.path.join(closed, "a.jsonl"))
        recall.index(self.db, top)
        self.assertEqual(self.hits("ZQMOVED"), [])

    def test_indexing_one_folder_keeps_another(self):
        one = self.session("a.jsonl", line("ZQONE words"), folder="one")
        two = self.session("b.jsonl", line("ZQTWO words"), folder="two")
        recall.index(self.db, one)
        recall.index(self.db, two)
        self.assertTrue(self.hits("ZQONE"))

    def test_a_restart_keeps_the_same_name_elsewhere(self):
        top = os.path.join(self.d, "top")
        for name, text in (("a", "ZQAAA words"), ("b", "ZQBBB words")):
            os.makedirs(os.path.join(top, name))
            with open(os.path.join(top, name, "same.jsonl"), "w", encoding="utf-8") as out:
                out.write(line(text) + line("padding"))
        recall.index(self.db, top)
        with open(os.path.join(top, "a", "same.jsonl"), "w", encoding="utf-8") as out:
            out.write(line("ZQNEW"))
        recall.index(self.db, top)
        self.assertTrue(self.hits("ZQBBB"))
        self.assertEqual(self.hits("ZQAAA"), [])

    def test_a_relabelled_line_in_a_closed_file_is_noticed(self):
        folder = self.session("a.jsonl")
        path = os.path.join(folder, "a.jsonl")
        with open(path, "w", encoding="utf-8") as out:
            out.write(line("ZQFLIP words", sensitivity="internal") + line("x", sensitivity="private"))
        recall.index(self.db, folder)
        self.assertTrue(self.hits("ZQFLIP"))
        with open(path, "w", encoding="utf-8") as out:
            out.write(line("ZQFLIP words", sensitivity="private") + line("xxxxxxx", sensitivity="private"))
        recall.index(self.db, folder)
        self.assertEqual(self.hits("ZQFLIP"), [])

    def test_a_malformed_line_does_not_stop_the_run(self):
        bad = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": 5}]}}) + "\n"
        deep = "[" * 100000 + "\n"
        surrogate = json.dumps({"type": "user", "timestamp": "\ud800", "message": {"content": "zqsur"}}) + "\n"
        folder = self.session("a.jsonl", bad, deep, surrogate, line("ZQAFTER words"))
        recall.index(self.db, folder)
        self.assertTrue(self.hits("ZQAFTER"))

    def test_closed_text_does_not_stay_in_the_file(self):
        folder = self.session("a.jsonl", line("ZQBYTES words"))
        recall.index(self.db, folder)
        self.roots((folder, "main-only"))
        recall.index(self.db, folder)
        with open(self.db, "rb") as data:
            self.assertNotIn(b"ZQBYTES", data.read())
        self.assertNotIn(b"zqbytes", open(self.db, "rb").read())

    def test_closed_text_of_a_dropped_file_does_not_stay_either(self):
        folder = self.session("a.jsonl", line("ZQDROPPED words"), line("zqother words"))
        self.session("b.jsonl", line("ZQKEPT words"))
        recall.index(self.db, folder)
        os.remove(os.path.join(folder, "a.jsonl"))
        recall.index(self.db, folder)
        self.assertNotIn(b"zqdropped", open(self.db, "rb").read().lower())


class Roots(Env):
    def test_a_long_path_prefix_on_a_closed_root_still_closes(self):
        opened = os.path.join(self.d, "pub")
        closed = os.path.join(opened, "priv")
        os.makedirs(closed)
        self.roots((opened, "public"), ("\\\\?\\" + closed, "main-only"))
        self.assertEqual(sources.load().class_of(os.path.join(closed, "x")), "main-only")


class Team(Env):
    def test_a_closed_session_loses_its_old_lessons(self):
        folder, out = os.path.join(self.d, "p"), os.path.join(self.d, "out")
        os.makedirs(folder)
        path = os.path.join(folder, "a.jsonl")
        tail = "".join(self.turns())
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "custom-title", "customTitle": "zqrole"}) + "\n" + tail)
        team.run(folder, out, min_sessions=1)
        self.assertTrue(os.path.exists(os.path.join(out, "zqrole.md")))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line("x", sensitivity="private"))
        team.run(folder, out, min_sessions=1)
        self.assertFalse(os.path.exists(os.path.join(out, "zqrole.md")))

    @staticmethod
    def turns():
        yield line("start")
        for n in range(3):
            yield json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:0%d" % n, "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t%d" % n, "name": "Bash", "input": {"command": "python a.py"}}]}}) + "\n"
            yield json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:0%d" % n, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t%d" % n, "content": "No module named zqm", "is_error": n < 2}]}}) + "\n"


class Forum(unittest.TestCase):
    def test_a_label_written_during_a_turn_survives(self):
        control = tempfile.mkdtemp()
        path = os.path.join(control, "tasks.json")
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"tasks": {"tp": {"name": "n", "session_id": "S0"}}}, out)
        f = object.__new__(forum.Forum)
        f.control, f.tasks_root, f.workspace, f.path = control, os.path.join(control, "ws"), control, path
        f._vetoed, f.chat_id, f.group = set(), 1, None
        f.stop_path, f.her_stop_path_value = os.path.join(control, "S"), os.path.join(control, "H")
        f._say = lambda thread, text: None
        h = mock.MagicMock()
        h._over_budget.return_value = False
        h.workspace, h.control = control, control
        h.now.return_value = 1762600000.0

        def turn(*a, **k):
            with open(path, "w", encoding="utf-8") as out:
                json.dump({"tasks": {"tp": {"name": "n", "session_id": "S0", "sensitivity": "private"}}}, out)
            return SimpleNamespace(ok=True, text="A", session_id="S1", error=None)

        h._turn.side_effect = turn
        f.h = h
        data = f._tasks()
        f._turn_in("tp", "hello", data, data["tasks"]["tp"])
        saved = json.load(open(path, encoding="utf-8"))["tasks"]["tp"]
        self.assertEqual(saved["sensitivity"], "private")
        self.assertEqual(saved["session_id"], "S1")


class Core(Base):
    def forge(self, *sql):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        for statement in sql:
            con.execute(statement)
        con.commit()
        con.close()

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, clock=self.clock)

    def test_a_rollback_of_the_newest_version_is_reported(self):
        mid = self.s.add(rec("first"), "x")
        self.s.correct(mid, 1, {"object": {"value": "second"}}, "r", OP)
        self.forge("DELETE FROM versions WHERE version=2", "UPDATE records SET current_version=1")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_damaged_document_does_not_stop_the_open(self):
        self.s.add(rec("x"), "x")
        self.forge("UPDATE versions SET doc='{broken'")
        os.remove(os.path.join(self.root, "lexical_main.sqlite"))
        self.reopen()
        self.assertTrue(self.s.verify(**MAIN))

    def test_unreadable_cells_are_a_report_not_an_exception(self):
        cases = ("UPDATE versions SET doc=x'ff'", "UPDATE event_keys SET payload=x'00'", "UPDATE evidence SET span=x'00'",
                 "UPDATE meta SET value='zz' WHERE key='evidence_count'")
        for statement in cases:
            with self.subTest(statement):
                self.tearDown()
                self.setUp()
                e = self.s.put_evidence(b"b", "b.md", "tool_output", {"id": "w", "role": "w"})
                self.s.add(rec("x", evidence_ids=[e], status="pending", authorship_channel="tool_output"), "x")
                self.forge(statement)
                self.assertTrue(self.s.verify(**MAIN))

    def test_a_damaged_counter_does_not_stop_writes_and_stays_reported(self):
        self.s.add(rec("x"), "x")
        self.forge("UPDATE meta SET value='zz' WHERE key='evidence_count'")
        self.s.add(rec("y"), "x")
        self.assertTrue([p for p in self.s.verify(**MAIN) if "rows are missing" in p])

    def test_one_unreadable_record_is_named_and_the_rest_still_checked(self):
        self.s.add(rec("x"), "x")
        self.forge("UPDATE versions SET doc=x'ff'")
        problems = self.s.verify(**MAIN)
        self.assertTrue([p for p in problems if "versions: unreadable" in p], problems)

    def test_a_ref_to_nothing_is_reported(self):
        self.s.add(rec("x"), "x")
        self.forge("INSERT INTO evidence_refs VALUES ('EVD-none', 'MEM-none')")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_missing_or_garbled_index_is_reported(self):
        self.s.add(rec("x"), "x")
        os.remove(os.path.join(self.root, "lexical_worker.sqlite"))
        self.assertTrue(self.s.verify())
        self.reopen()
        with open(os.path.join(self.root, "lexical_main.sqlite"), "wb") as out:
            out.write(b"garbage" * 1000)
        self.assertTrue(self.s.verify(**MAIN))

    def test_an_index_ahead_of_the_snapshot_is_no_alarm(self):
        self.s.add(rec("x"), "x")
        con = sqlite3.connect(os.path.join(self.root, "lexical_worker.sqlite"))
        con.execute("INSERT INTO lex VALUES ('MEM-x', 'zq')")
        con.execute("UPDATE gen SET value='ahead'")
        con.commit()
        con.close()
        self.assertEqual([p for p in self.s.verify() if "index" in p], [])

    def test_a_bad_ledger_state_is_reported(self):
        mid = self.s.add(rec("gone"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.forge("UPDATE erasure_ledger SET state='whatever'")
        self.assertTrue(self.s.verify(**MAIN))

    def test_versions_of_nothing_are_reported(self):
        self.s.add(rec("x"), "x")
        self.forge("INSERT INTO versions VALUES ('MEM-none', 1, '{}', NULL, NULL)")
        self.assertTrue(self.s.verify(**MAIN))


if __name__ == "__main__":
    unittest.main()
