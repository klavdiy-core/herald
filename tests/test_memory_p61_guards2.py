"""P61 guards 2 (reviewer M-21515): what the author's fix will touch must be held BEFORE his code. Green on 4d77fa2;
each guard goes red on its own mutant (p61_matrix.py G1-G3, T1, C1, S1). test author, 11.11.2026."""
import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import herald.memory.store as store_module  # noqa: E402
from herald.memory import Store  # noqa: E402
from test_memory_core import Clock, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}
NL = chr(10).encode()


def jline(seq, prev, **extra):
    return json.dumps(dict({"seq": seq, "prev_hash": prev}, **extra)).encode("utf-8")


def h(raw):
    return hashlib.sha256(raw).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "s")
        self.clock = Clock()
        self.s = Store(self.root, clock=self.clock)
        self.addCleanup(lambda: getattr(self.s, "close", lambda: None)())

    @property
    def journal(self):
        return self.root + ".erasures.jsonl"

    def write_journal(self, raws):
        with open(self.journal, "wb") as f:
            f.write(NL.join(raws) + NL if raws else b"")

    def problems(self, raws):
        """Full check of a hand-made journal by verify_journal(main) on the open store: no open, no anchor needed."""
        self.write_journal(raws)
        return self.s.verify_journal(**MAIN)

    def corrupt_text(self, raws):
        return " | ".join(p for p in self.problems(raws) if "corrupt" in p)

    def erase_some(self, n):
        for i in range(n):
            mid = self.s.add(rec("guard2 record %d" % i, memory_id="MEM-guardtwo0000000%d" % i, predicate="p%d" % i), "x", **MAIN)
            self.s.erase(mid, "asked", OP, **MAIN)


class Order(Base):
    def test_a_line_that_is_not_json_is_named_before_anything_else(self):
        text = self.corrupt_text([b"not json at all", jline(5, "wrong")])
        self.assertIn("line 1", text)
        self.assertIn("not a JSON line", text)

    def test_json_that_is_not_an_object_is_named(self):
        self.assertIn("not a JSON object", self.corrupt_text([b"[1, 2, 3]"]))

    def test_seq_is_checked_before_prev_hash(self):
        text = self.corrupt_text([jline(7, "wrong prev as well")])
        self.assertIn("seq", text)
        self.assertNotIn("prev_hash", text)

    def test_a_right_seq_with_a_wrong_prev_hash_is_named_as_the_chain(self):
        first = jline(1, "")
        text = self.corrupt_text([first, jline(2, "0" * 64)])
        self.assertIn("line 2", text)
        self.assertIn("prev_hash", text)

    def test_the_first_line_must_carry_an_empty_prev_hash(self):
        self.assertIn("prev_hash", self.corrupt_text([jline(1, "x")]))
        self.assertEqual(self.corrupt_text([jline(1, "")]), "", "an empty prev_hash on line 1 is the rule")

    def test_a_chain_built_by_the_rule_is_accepted(self):
        l1 = jline(1, "")
        l2 = jline(2, h(l1))
        l3 = jline(3, h(l2))
        self.assertEqual(self.corrupt_text([l1, l2, l3]), "")

    def test_seq_must_be_a_real_integer_not_a_bool_or_a_float(self):
        for bad in (True, 1.0, "1"):
            with self.subTest(seq=bad):
                self.assertIn("seq", self.corrupt_text([jline(bad, "")]))


class Anchor(Base):
    def counting(self):
        return mock.patch.object(Store, "_journal_check", autospec=True, side_effect=Store._journal_check)

    def test_a_matching_anchor_means_the_ordinary_open_reads_only_the_last_line(self):
        self.erase_some(2)
        self.s.close()
        with self.counting() as check:
            self.s = Store(self.root, clock=self.clock)
        self.assertEqual(check.call_count, 0, "tail-only: the full structural pass must not run on a matching anchor")

    def test_an_empty_meta_anchor_means_a_full_pass_and_the_anchor_is_written_again(self):
        self.erase_some(2)
        self.s.close()
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        try:
            con.execute("DELETE FROM meta WHERE key IN ('journal_seq', 'journal_head')")
            con.commit()
        finally:
            con.close()
        with self.counting() as check:
            self.s = Store(self.root, clock=self.clock)
        self.assertGreaterEqual(check.call_count, 1, "no anchor in meta: the journal must be read and checked in full")
        self.assertEqual(self.s._anchor()[0], 2, "the anchor must be written again from the journal")


class Slow(Base):
    def test_a_full_pass_over_the_limit_leaves_a_warning_and_a_fast_one_does_not(self):
        self.erase_some(1)
        self.s.close()
        for limit, expect in ((-1.0, True), (3600.0, False)):
            with self.subTest(limit=limit):
                con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
                try:
                    con.execute("DELETE FROM meta WHERE key IN ('journal_seq', 'journal_head')")      # force the full pass
                    con.commit()
                finally:
                    con.close()
                with mock.patch.object(store_module, "JOURNAL_SLOW_SECONDS", limit):
                    if expect:
                        with self.assertLogs(store_module.LOG, level=logging.WARNING) as cm:
                            self.s = Store(self.root, clock=self.clock)
                        self.assertTrue(any("full pass" in m for m in cm.output), cm.output)
                    else:
                        with self.assertNoLogs(store_module.LOG, level=logging.WARNING):
                            self.s = Store(self.root, clock=self.clock)
                self.s.close()


if __name__ == "__main__":
    unittest.main()
