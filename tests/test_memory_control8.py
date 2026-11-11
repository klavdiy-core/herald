"""Confirmed defects from the control round (privacy and data loss)."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import forum, recall  # noqa: E402
from herald.memory import Store  # noqa: E402
from test_memory_attack4 import Env, line  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

FIELDS = {"done": "ZQTAIL", "next": "n", "files": "f", "turn_at": "2026-11-08T10:00:00Z", "session": "s"}


class Threads(Env):
    def control(self, entry):
        control = tempfile.mkdtemp(dir=self.d)
        with open(os.path.join(control, "tasks.json"), "w", encoding="utf-8") as out:
            json.dump({"tasks": {"tp": entry}}, out)
        forum.write_tail(control, "tp", FIELDS)
        return control

    def test_a_label_in_any_case_or_depth_closes_a_thread(self):
        for entry in ({"Sensitivity": "private"}, {"meta": {"sensitivity": "private"}}, {"name": "n", "SENSITIVITY": "weird"}):
            with self.subTest(entry):
                self.assertEqual(forum.read_tail(self.control(entry), "tp"), ("none", None))

    def test_a_public_label_or_none_stays_open(self):
        for entry in ({"sensitivity": "public"}, {"name": "n"}):
            with self.subTest(entry):
                self.assertEqual(forum.read_tail(self.control(entry), "tp")[0], "tail")


class Store8(Base):
    def test_a_lost_database_is_refused_even_with_no_evidence(self):
        self.s.add(rec("x"), "x")
        self.s.close()
        db = os.path.join(self.root, "memory.sqlite")
        for empty in (False, True):
            with self.subTest(empty=empty):
                os.rename(db, db + ".away")
                if empty:
                    open(db, "wb").close()
                with self.assertRaises(RuntimeError):
                    Store(self.root, clock=self.clock)
                if empty:
                    os.remove(db)
                os.rename(db + ".away", db)
        self.s = Store(self.root, clock=self.clock)

    def test_a_crash_before_the_first_open_finished_may_start_again(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "tombstone.salt"), "w", encoding="ascii") as out:
            out.write("a" * 64)
        Store(root).close()
        Store(root).close()


class Tasks(unittest.TestCase):
    def forum_at(self):
        control = tempfile.mkdtemp()
        f = object.__new__(forum.Forum)
        f.control, f.path = control, os.path.join(control, "tasks.json")
        f.h = mock.MagicMock()
        return f

    def write(self, f, data):
        with open(f.path, "w", encoding="utf-8") as out:
            out.write(data if isinstance(data, str) else json.dumps(data))

    def test_a_transient_read_failure_keeps_the_good_file(self):
        f = self.forum_at()
        good = {"tasks": {"1": {"name": "a"}}, "done_topic": None}
        self.write(f, good)
        real = open

        def failing(path, *a, **k):
            if str(path) == f.path:
                raise OSError(24, "too many open files")
            return real(path, *a, **k)

        with mock.patch("builtins.open", failing):
            f._tasks()
        self.assertTrue(os.path.exists(f.path))
        self.assertFalse(os.path.exists(f.path + ".bad"))
        with self.assertRaises(OSError):
            f._put({"tasks": {}, "done_topic": None})
        self.assertEqual(f._tasks()["tasks"], good["tasks"])

    def test_a_second_broken_file_does_not_overwrite_the_first(self):
        f = self.forum_at()
        self.write(f, "{first broken")
        f._tasks()
        self.write(f, "{second broken")
        f._tasks()
        kept = sorted(n for n in os.listdir(f.control) if ".bad" in n)
        self.assertEqual(len(kept), 2, kept)
        bodies = {open(os.path.join(f.control, n), encoding="utf-8").read() for n in kept}
        self.assertEqual(bodies, {"{first broken", "{second broken"})

    def test_a_cut_after_moving_the_broken_file_still_rises_from_prev(self):
        f = self.forum_at()
        good = {"tasks": {"7": {"name": "a", "session_id": "S"}}, "done_topic": None}
        with open(f.path + ".prev", "w", encoding="utf-8") as out:
            json.dump(good, out)
        with open(f.path + ".bad", "w", encoding="utf-8") as out:
            out.write("{torn")
        self.assertEqual(f._tasks()["tasks"], good["tasks"])
        self.assertTrue(os.path.exists(f.path))

    def test_a_prev_that_cannot_be_opened_stops_writes_and_survives(self):
        f = self.forum_at()
        good = {"tasks": {"7": {"name": "a"}}, "done_topic": None}
        with open(f.path + ".prev", "w", encoding="utf-8") as out:
            json.dump(good, out)
        self.write(f, "{torn")
        real = open

        def held(path, *a, **k):
            if str(path) == f.path + ".prev":
                raise PermissionError(13, "held by another process")
            return real(path, *a, **k)

        with mock.patch("builtins.open", held):
            self.assertEqual(f._tasks()["tasks"], {})
            with self.assertRaises(OSError):
                f._put({"tasks": {"9": {"name": "new"}}, "done_topic": None})
        self.assertEqual(f._tasks()["tasks"], good["tasks"])

    def test_a_prev_held_on_a_later_open_is_never_taken_for_lost(self):
        f = self.forum_at()
        good = {"tasks": {"7": {"name": "a"}}, "done_topic": None}
        with open(f.path + ".prev", "w", encoding="utf-8") as out:
            json.dump(good, out)
        self.write(f, "{torn")
        real, opens = open, []

        def held_second(path, *a, **k):
            if str(path) == f.path + ".prev":
                opens.append(1)
                if len(opens) == 2:
                    raise PermissionError(13, "held by another process")
            return real(path, *a, **k)

        with mock.patch("builtins.open", held_second):
            got = f._tasks()["tasks"]
            if got != good["tasks"]:                      # not restored: then it must be locked, never an empty store that writes
                with self.assertRaises(OSError):
                    f._put({"tasks": {"9": {"name": "new1"}}, "done_topic": None})
        with open(f.path + ".prev", encoding="utf-8") as g:
            self.assertEqual(json.load(g)["tasks"], good["tasks"])
        self.assertEqual(f._tasks()["tasks"], good["tasks"])

    def test_a_prev_of_the_wrong_shape_is_not_restored(self):
        for odd in (["a"], {"tasks": [1]}, {"done_topic": None}):
            f = self.forum_at()
            with open(f.path + ".prev", "w", encoding="utf-8") as out:
                json.dump(odd, out)
            self.write(f, "{torn")
            self.assertEqual(f._tasks()["tasks"], {}, odd)
            with open(f.path + ".prev", encoding="utf-8") as g:
                self.assertEqual(json.load(g), odd)               # left as it was, not copied over tasks.json
            self.assertFalse(os.path.exists(f.path), odd)

    def test_a_missing_file_with_no_broken_copy_stays_empty(self):
        f = self.forum_at()
        with open(f.path + ".prev", "w", encoding="utf-8") as out:
            json.dump({"tasks": {"7": {"name": "a"}}, "done_topic": None}, out)
        self.assertEqual(f._tasks()["tasks"], {})
        self.assertFalse(os.path.exists(f.path))


class Recall(Env):
    def test_a_renamed_folder_leaves_no_old_rows(self):
        folder = self.session("a.jsonl", line("ZQRENAMED words"), folder="a")
        recall.index(self.db, folder)
        moved = os.path.join(self.d, "b")
        os.rename(folder, moved)
        with open(os.path.join(moved, "a.jsonl"), "a", encoding="utf-8") as out:
            out.write(line("x", sensitivity="private"))
        recall.index(self.db, moved)
        self.assertEqual(recall.search(self.db, "ZQRENAMED"), [])

    def test_a_deleted_folder_leaves_no_rows(self):
        folder = self.session("a.jsonl", line("ZQDELETED words"), folder="a")
        recall.index(self.db, folder)
        shutil.rmtree(folder)
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQDELETED"), [])

    def test_a_deleted_folder_leaves_no_words_in_the_database_file(self):
        folder = self.session("a.jsonl", line("ZQGONEWORD words"), folder="a")
        recall.index(self.db, folder)
        shutil.rmtree(folder)
        recall.index(self.db, folder)
        with open(self.db, "rb") as f:
            self.assertNotIn(b"ZQGONEWORD".lower(), f.read().lower())

    def test_a_missing_folder_forgets_under_the_write_lock(self):
        folder = self.session("a.jsonl", line("ZQLOCK words"), folder="a")
        recall.index(self.db, folder)
        shutil.rmtree(folder)
        real, seen = recall._forget_vanished, []

        def spy(con, also):
            seen.append(con.in_transaction)
            return real(con, also)

        with mock.patch.object(recall, "_forget_vanished", spy):
            recall.index(self.db, folder)
        self.assertEqual(seen, [True])

    def test_an_index_from_before_scopes_leaves_no_words_when_dropped(self):
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        con = sqlite3.connect(self.db)
        con.executescript("""CREATE TABLE files (path TEXT PRIMARY KEY, lines INTEGER, size INTEGER);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session TEXT, role TEXT, ts TEXT, text TEXT);""")
        for i in range(400):
            con.execute("INSERT INTO messages(session, role, ts, text) VALUES ('s', 'human', 't', ?)", ("ZQOLDWORD%d padding" % i,))
        con.commit()
        con.close()
        recall.index(self.db, os.path.join(self.d, "nothing"))
        with open(self.db, "rb") as f:
            self.assertNotIn(b"zqoldword", f.read().lower())

    def test_a_missing_folder_does_not_create_a_database(self):
        self.assertEqual(recall.index(self.db, os.path.join(self.d, "nothing")), 0)
        self.assertFalse(os.path.exists(self.db))

    def test_a_folder_that_exists_keeps_another_folders_rows(self):
        one = self.session("a.jsonl", line("ZQKEEP words"), folder="one")
        two = self.session("b.jsonl", line("x"), folder="two")
        recall.index(self.db, one)
        recall.index(self.db, two)
        self.assertTrue(recall.search(self.db, "ZQKEEP"))


if __name__ == "__main__":
    unittest.main()
