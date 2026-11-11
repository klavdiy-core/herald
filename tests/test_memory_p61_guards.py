"""P61 guards on what already exists (reviewer M-21506): green on 4d77fa2, red on the matrix mutants Q1 and Q2.
test author, 11.11.2026."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import herald.memory as hm  # noqa: E402
from herald.memory import PolicyRefused, Store  # noqa: E402
from test_memory_core import Clock, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "s")
        self.clock = Clock()
        self.s = Store(self.root, clock=self.clock)
        self.addCleanup(lambda: getattr(self.s, "close", lambda: None)())

    def test_q1_verify_journal_names_a_bad_middle_line_that_an_ordinary_open_does_not_see(self):
        ids = []
        for n in range(3):
            mid = self.s.add(rec("guard record %d" % n, memory_id="MEM-guardrecord0000%d" % n, predicate="p%d" % n), "x", **MAIN)
            self.s.erase(mid, "asked", OP, **MAIN)
            ids.append(mid)
        self.s.close()
        journal = self.root + ".erasures.jsonl"
        with open(journal, "rb") as f:
            lines = f.read().split(b"\n")
        self.assertEqual(len([ln for ln in lines if ln.strip()]), 3, "stand: three journal lines")
        lines[1] = b"this middle line is garbage"
        with open(journal, "wb") as f:
            f.write(b"\n".join(lines))
        self.s = Store(self.root, clock=self.clock)             # the last line still matches the anchor: it opens (named limit)
        problems = self.s.verify_journal(**MAIN)
        self.assertTrue(any("line 2" in p for p in problems), problems)
        with self.assertRaises(PolicyRefused):
            self.s.verify_journal(reader_scope="worker")

    def test_q2_erased_id_reused_is_a_policy_refusal_in_the_class_and_on_the_path(self):
        self.assertTrue(issubclass(hm.ErasedIdReused, PolicyRefused))
        self.s.close()
        snap = os.path.join(self.tmp.name, "copy")             # BEFORE X exists: a copy holding X would erase it again by birth
        shutil.copytree(self.root, snap)
        self.s = Store(self.root, clock=self.clock)
        mid = self.s.add(rec("guard text id", memory_id="MEM-guardrecordidxx1"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)             # the erase is applied again; the id is dead
        with self.assertRaises(PolicyRefused) as cm:
            self.s.add(rec("another text", memory_id=mid), "again", **MAIN)
        self.assertEqual(type(cm.exception).__name__, "ErasedIdReused")


if __name__ == "__main__":
    unittest.main()
