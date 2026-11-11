"""p61, the author's own tests: each kills a mutant the reviewer's set let live (work/p61_mutants.py, 11.11.2026).
no_seq_check, fields_not_required, no_torn_drop, reapply_no_anchor."""
import hashlib
import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import ErasureJournalCorrupt, Store  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}


def raw_lines(path):
    with open(path, "rb") as f:
        return f.read().split(b"\n")


class Own(Base):
    @property
    def journal(self):
        return self.root + ".erasures.jsonl"

    def restore_db(self, snap):
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))

    def snapshot(self, name):
        self.s.close()
        dst = os.path.join(self.tmp.name, name)
        shutil.copytree(self.root, dst)
        self.s = Store(self.root, clock=self.clock)
        return dst

    def test_a_wrong_seq_is_named_as_seq_at_its_line(self):
        """no_seq_check: a right chain with seq 3 on line 2 must be refused for its seq, not for some later reason."""
        self.s.erase(self.s.add(rec("one"), "x"), "asked", OP, **MAIN)
        self.s.close()
        first = raw_lines(self.journal)[0]
        second = json.dumps({"seq": 3, "prev_hash": hashlib.sha256(first).hexdigest()}).encode()
        with open(self.journal, "wb") as f:
            f.write(first + b"\n" + second + b"\n")
        with self.assertRaises(ErasureJournalCorrupt) as cm:
            Store(self.root, clock=self.clock)
        self.assertIn("line 2", str(cm.exception))
        self.assertIn("seq", str(cm.exception))
        self.s = Store.__new__(Store)
        self.s.close = lambda: None

    def test_a_line_after_the_anchor_without_its_fields_is_corrupt(self):
        """fields_not_required: a well-chained line the base has not applied, with no id/birth/keys/scope."""
        self.s.erase(self.s.add(rec("one"), "x"), "asked", OP, **MAIN)
        self.s.close()
        first = raw_lines(self.journal)[0]
        second = json.dumps({"seq": 2, "prev_hash": hashlib.sha256(first).hexdigest()}).encode()
        with open(self.journal, "ab") as f:
            f.write(second + b"\n")
        with self.assertRaises(ErasureJournalCorrupt) as cm:
            Store(self.root, clock=self.clock)
        self.assertIn("line 2", str(cm.exception))
        self.assertIn("no id", str(cm.exception))
        self.s = Store.__new__(Store)
        self.s.close = lambda: None

    def test_a_torn_tail_is_dropped_so_the_next_erase_writes_a_whole_line(self):
        """no_torn_drop: without dropping the torn bytes the next erase glues its line to them and the journal breaks."""
        self.s.erase(self.s.add(rec("one"), "x"), "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "ab") as f:
            f.write(b'{"seq": 2, "prev_ha')
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(self.s.add(rec("two"), "y"), "asked", OP, **MAIN)
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(self.s.verify_journal(**MAIN), [])
        self.assertEqual([json.loads(l)["seq"] for l in raw_lines(self.journal) if l], [1, 2])

    def test_after_a_restore_open_the_anchor_is_the_journal_head(self):
        """reapply_no_anchor: after applying the journal again, the base's anchor must move to its last line, so the
        next open reads only the tail and verify_journal is clean."""
        mid = self.s.add(rec("to be erased"), "x")
        snap = self.snapshot("before")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.restore_db(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(self.s.reapplied, 1)
        self.assertEqual(self.s.verify_journal(**MAIN), [])


if __name__ == "__main__":
    unittest.main()
