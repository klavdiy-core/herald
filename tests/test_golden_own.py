"""Golden-set runner and what it needs from the store: my own tests, written apart from the test author's organ1/test_golden_runner.py.
author 11.11.2026."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import sqlite3  # noqa: E402
from unittest import mock  # noqa: E402

from herald import golden, recall  # noqa: E402
from herald.memory import Store, ValidationError  # noqa: E402
from test_memory_core import Clock, rec  # noqa: E402

MAIN = {"reader_scope": "main"}


def snapshot(folder):
    out = {}
    for base, _, names in os.walk(folder):
        for n in names:
            if not n.endswith(("-wal", "-shm")):
                p = os.path.join(base, n)
                with open(p, "rb") as f:
                    out[p] = hashlib.sha256(f.read()).hexdigest()
    return out


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "store")
        s = Store(self.root, clock=Clock())
        self.x = s.add(rec("ostrich teapot"), "stand", **MAIN)
        s.close()

    def run_golden(self, rows, *args):
        path = os.path.join(self.tmp.name, "set.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        out, err = io.StringIO(), io.StringIO()
        code = golden.main(["--set", path] + list(args), out, err)
        return code, out.getvalue(), err.getvalue()


class HealthyOpenWritesNothing(Tmp):
    def test_opening_and_searching_a_healthy_store_leaves_every_file_as_it_was(self):
        """A write transaction that changed nothing used to rewrite the two kept counts, so even an open changed the
        bytes of memory.sqlite: the golden runner could not be a pure reader."""
        before = snapshot(self.tmp.name)
        s = Store(self.root, clock=Clock())
        self.assertEqual(s.search("ostrich teapot", **MAIN, limit=5)["outcome"], "found")
        s.close()
        self.assertEqual(snapshot(self.tmp.name), before)

    def test_the_kept_counts_still_follow_a_real_change(self):
        s = Store(self.root, clock=Clock())
        s.erase(self.x, "asked", {"id": "ENT-operator", "role": "operator"}, **MAIN)
        self.assertEqual(s.verify(**MAIN), [])
        s.close()


class Runner(Tmp):
    def test_an_id_that_could_carry_text_is_a_broken_line(self):
        code, out, err = self.run_golden([{"id": "ostrich teapot", "query": "ostrich teapot", "expect": [self.x]}],
                                         "--store", self.root)
        self.assertEqual((code, out), (4, ""))
        self.assertNotIn("ostrich", err)
        self.assertIn("line 1", err)

    def test_a_broken_line_anywhere_prints_no_row_at_all(self):
        code, out, _ = self.run_golden([{"id": "a", "query": "ostrich teapot", "expect": [self.x]},
                                        {"id": "b", "query": "ostrich teapot", "expect": [""]}], "--store", self.root)
        self.assertEqual((code, out), (4, ""))      # [] is the zero class since M-22224; [""] is still broken

    def test_a_duplicate_id_is_a_broken_line(self):
        code, _, err = self.run_golden([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}] * 2, "--store", self.root)
        self.assertEqual(code, 4)
        self.assertIn("duplicate_id", err)

    def test_a_search_that_raises_is_unknown_and_names_only_the_class(self):
        def boom(*a, **k):
            raise ValidationError("ostrich teapot inside the message")
        with mock.patch.object(Store, "search", boom):
            code, out, err = self.run_golden([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}], "--store", self.root)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["a\tunknown\t-", "TOTAL 1 PASS 0"])
        self.assertEqual(err.strip(), "a\tValidationError")

    def test_a_query_that_cannot_be_encoded_is_a_broken_line(self):
        code, out, err = self.run_golden([{"id": "a", "query": "ostrich \ud800 teapot", "expect": [self.x]}],
                                         "--store", self.root)
        self.assertEqual((code, out, err.strip()), (4, "", "a\tbad_query"))

    def test_an_empty_folder_is_not_turned_into_a_store(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.mkdir(empty)
        code, out, _ = self.run_golden([{"id": "a", "query": "q", "expect": ["x"]}], "--store", empty)
        self.assertEqual(code, 7)
        self.assertIn("GOLDEN UNAVAILABLE", out)
        self.assertEqual(os.listdir(empty), [])


class ControlRound1(Tmp):
    """wf_f1a3eafa, 7 findings confirmed by two skeptics each; every test is red on golden.py sha 287dd57f (before the round)."""

    def run_unchanged(self, rows, *args):
        before = snapshot(self.tmp.name)
        result = self.run_golden(rows, *args)
        after = snapshot(self.tmp.name)
        self.assertEqual({p: h for p, h in after.items() if not p.endswith("set.jsonl")},
                         {p: h for p, h in before.items() if not p.endswith("set.jsonl")}, "a file changed or appeared")
        return result

    # F1: a store that needs a repair on open was repaired by the runner (indexes rebuilt, files written)
    def test_f1_a_store_that_needs_a_repair_is_refused_and_left_as_it_was(self):
        os.remove(os.path.join(self.root, "lexical_main.sqlite"))
        code, out, _ = self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}], "--store", self.root)
        self.assertEqual(code, 7)
        self.assertIn("GOLDEN UNAVAILABLE", out)

    def test_f1_erasures_waiting_in_the_pending_file_refuse_the_store(self):
        with open(self.root + ".erasures.pending.jsonl", "wb") as f:
            f.write(b"")
        code, _, _ = self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}], "--store", self.root)
        self.assertEqual(code, 7)

    # mutants that survived the first matrix: each refusal reason on its own stand, and the write made impossible
    def test_f1_an_index_of_another_generation_is_refused(self):
        con = sqlite3.connect(os.path.join(self.root, "lexical_main.sqlite"))
        con.execute("UPDATE gen SET value='stale'")
        con.commit()
        con.close()
        code, _, _ = self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}], "--store", self.root)
        self.assertEqual(code, 7)

    def test_f1_a_journal_that_does_not_match_the_base_is_refused(self):
        s = Store(self.root, clock=Clock())
        y = s.add(rec("other fact", predicate="likes"), "y", **MAIN)
        s.erase(y, "asked", {"id": "ENT-operator", "role": "operator"}, **MAIN)
        s.close()
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        con.execute("UPDATE meta SET value='0' WHERE key='journal_seq'")
        con.execute("UPDATE meta SET value='' WHERE key='journal_head'")
        con.commit()
        con.close()
        code, _, _ = self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}], "--store", self.root)
        self.assertEqual(code, 7)

    def test_f1_a_folder_without_a_database_is_store_needs_repair_and_stays_empty(self):
        from herald.memory import ReadOnlyStore, StoreNeedsRepair
        empty = os.path.join(self.tmp.name, "nothing_here")
        os.mkdir(empty)
        with self.assertRaises(StoreNeedsRepair):
            ReadOnlyStore(empty)
        self.assertEqual(os.listdir(empty), [])

    def test_f1_a_store_without_its_salt_is_refused_and_no_salt_is_made(self):
        from herald.memory import ReadOnlyStore, StoreNeedsRepair
        salt = os.path.join(self.root, "tombstone.salt")
        os.remove(salt)
        with self.assertRaises(StoreNeedsRepair):
            ReadOnlyStore(self.root)
        self.assertFalse(os.path.exists(salt))

    def test_f1_the_reader_cannot_write_its_database(self):
        from herald.memory import ReadOnlyStore
        reader = ReadOnlyStore(self.root)
        self.addCleanup(reader.close)
        with self.assertRaises(sqlite3.OperationalError):
            reader.db.execute("INSERT OR REPLACE INTO meta VALUES ('probe', '1')")

    def test_f2_an_index_missing_a_table_is_left_as_it_was(self):
        folder = os.path.join(self.tmp.name, "s2")
        os.makedirs(folder)
        session = os.path.join(folder, "one.jsonl")
        with open(session, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "ostrich teapot"}, "timestamp": "2026-11-11T10:00:00"}) + "\n")
        db = os.path.join(self.tmp.name, "r2.db")
        recall.index(db, folder, reader_scope="main")
        con = sqlite3.connect(db)
        con.execute("DROP TABLE meta")             # a read-write connection would create it again
        con.commit()
        con.close()
        code, out, _ = self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [os.path.realpath(session)]}],
                                          "--recall-db", db)
        self.assertEqual((code, out.splitlines()), (0, ["a\tpass\t1", "TOTAL 1 PASS 1"]))

    def test_a_bad_argument_never_echoes_its_text(self):
        out, err = io.StringIO(), io.StringIO()
        try:
            code = golden.main(["--set", "x.jsonl", "--store", self.root, "--bogus", "ostrich teapot"], out, err)
        except SystemExit:
            self.fail("argparse exited and printed the arguments")
        self.assertEqual(code, 7)
        self.assertNotIn("ostrich", out.getvalue() + err.getvalue())

    # F2: recall's _connect dropped an old index (no source column) or wrote a schema into a foreign file
    def test_f2_an_old_recall_index_is_refused_and_not_dropped(self):
        db = os.path.join(self.tmp.name, "old.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session TEXT, role TEXT, ts TEXT, text TEXT)")
        con.execute("INSERT INTO messages VALUES (1, 's', 'user', 't', 'ostrich teapot')")
        con.commit()
        con.close()
        code, out, _ = self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [os.path.abspath(db)]}],
                                          "--recall-db", db)
        self.assertEqual(code, 7, out)

    def test_f2_an_empty_file_is_not_turned_into_an_index(self):
        db = os.path.join(self.tmp.name, "empty.db")
        open(db, "wb").close()
        code, _, _ = self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [os.path.abspath(db)]}],
                                        "--recall-db", db)
        self.assertEqual(code, 7)

    # F3: emit/warn ran inside the except block: a failing print chained the search error (and its text) into a traceback
    def test_f3_a_broken_stdout_never_lets_a_chained_traceback_out(self):
        class Broken(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")

        def boom(*a, **k):
            raise RuntimeError("ostrich teapot inside the message")
        path = os.path.join(self.tmp.name, "set.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "a", "query": "ostrich teapot", "expect": [self.x]}) + "\n")
        err = io.StringIO()
        with mock.patch.object(Store, "search", boom):
            try:
                code = golden.main(["--set", path, "--store", self.root], Broken(), err)
            except BaseException as escaped:        # an escaped exception IS the traceback a real run would print
                self.fail("an exception left main: %r" % (type(escaped),))
        self.assertEqual(code, 1)
        self.assertNotIn("ostrich", err.getvalue())

    # F4: an open through Store wrote a missing evidence_count back as a count
    def test_f4_a_missing_kept_count_stays_missing(self):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        con.execute("DELETE FROM meta WHERE key='evidence_count'")
        con.commit()
        con.close()
        self.run_unchanged([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}], "--store", self.root)

    # F5: a set file saved with a BOM was a broken line 1
    def test_f5_a_set_file_with_a_bom_runs(self):
        path = os.path.join(self.tmp.name, "set.jsonl")
        with open(path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"id": "a", "query": "ostrich teapot", "expect": [self.x]}).encode() + b"\r\n")
        out, err = io.StringIO(), io.StringIO()
        code = golden.main(["--set", path, "--store", self.root], out, err)
        self.assertEqual((code, out.getvalue().splitlines()), (0, ["a\tpass\t1", "TOTAL 1 PASS 1"]), err.getvalue())

    # F6: a query with no searchable word passed and was always "fail"
    def test_f6_a_query_with_no_searchable_word_is_a_broken_line(self):
        code, out, err = self.run_golden([{"id": "a", "query": "!!! ???", "expect": [self.x]}], "--store", self.root)
        self.assertEqual((code, out, err.strip()), (4, "", "a\tbad_query"))

    # F7: a relative expect path was resolved against the current folder, so a bare file name could pass
    def test_f7_a_relative_expect_path_is_a_broken_line_in_the_recall_branch(self):
        folder = os.path.join(self.tmp.name, "s")
        os.makedirs(folder)
        with open(os.path.join(folder, "one.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "ostrich teapot"}, "timestamp": "2026-11-11T10:00:00"}) + "\n")
        db = os.path.join(self.tmp.name, "r.db")
        recall.index(db, folder, reader_scope="main")
        code, out, err = self.run_golden([{"id": "a", "query": "ostrich teapot", "expect": ["one.jsonl"]}], "--recall-db", db)
        self.assertEqual((code, out, err.strip()), (4, "", "a\tbad_expect"))


class ZeroClass(Tmp):
    """reviewer M-22224 (A), lead M-22215: an empty expect is the zero class. No abstention exists before organ 3, so its
    search is not run: the row is unknown and the total counts it apart."""

    def test_an_empty_expect_is_unknown_and_counted_apart(self):
        with mock.patch.object(Store, "search", side_effect=AssertionError("a zero row must not be searched")):
            code, out, err = self.run_golden([{"id": "z", "query": "ostrich teapot", "expect": []}], "--store", self.root)
        self.assertEqual((code, out.splitlines(), err), (0, ["z\tunknown\t-", "TOTAL 1 PASS 0 ZERO_UNCHECKED 1"], ""))

    def test_zero_rows_beside_normal_rows(self):
        code, out, _ = self.run_golden([{"id": "a", "query": "ostrich teapot", "expect": [self.x]},
                                        {"id": "z", "query": "ostrich teapot", "expect": []}], "--store", self.root)
        self.assertEqual((code, out.splitlines()), (0, ["a\tpass\t1", "z\tunknown\t-", "TOTAL 2 PASS 1 ZERO_UNCHECKED 1"]))

    def test_without_zero_rows_the_total_line_is_unchanged(self):
        _, out, _ = self.run_golden([{"id": "a", "query": "ostrich teapot", "expect": [self.x]}], "--store", self.root)
        self.assertEqual(out.splitlines()[-1], "TOTAL 1 PASS 1")

    def test_an_empty_expect_in_the_recall_branch_is_zero_too(self):
        folder = os.path.join(self.tmp.name, "s3")
        os.makedirs(folder)
        with open(os.path.join(folder, "one.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "ostrich teapot"}, "timestamp": "2026-11-11T10:00:00"}) + "\n")
        db = os.path.join(self.tmp.name, "r3.db")
        recall.index(db, folder, reader_scope="main")
        code, out, _ = self.run_golden([{"id": "z", "query": "ostrich teapot", "expect": []}], "--recall-db", db)
        self.assertEqual((code, out.splitlines()), (0, ["z\tunknown\t-", "TOTAL 1 PASS 0 ZERO_UNCHECKED 1"]))

    def test_an_expect_that_is_not_a_list_is_still_a_broken_line(self):
        code, _, err = self.run_golden([{"id": "a", "query": "ostrich teapot", "expect": None}], "--store", self.root)
        self.assertEqual((code, err.strip()), (4, "a\tbad_expect"))


class RecallSource(unittest.TestCase):
    def test_with_source_adds_the_full_path_and_the_old_call_is_unchanged(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        folder = os.path.join(tmp.name, "s")
        os.makedirs(folder)
        path = os.path.join(folder, "one.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "ostrich teapot"}, "timestamp": "2026-11-11T10:00:00"}) + "\n")
        db = os.path.join(tmp.name, "r.db")
        recall.index(db, folder, reader_scope="main")
        plain = recall.search(db, "ostrich teapot", reader_scope="main")
        self.assertNotIn("source", plain[0])
        with_source = recall.search(db, "ostrich teapot", reader_scope="main", with_source=True)
        self.assertEqual(os.path.normcase(os.path.realpath(with_source[0]["source"])), os.path.normcase(os.path.realpath(path)))


if __name__ == "__main__":
    unittest.main()
