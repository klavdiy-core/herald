"""p61, third control round on 9213f18 (wf_7fed8e5c, 11.11.2026): one test per real finding, each red on 9213f18.
Four of six share one root: evidence was dropped by the wrong rule ("not in a union pass"). The right one: drop it only the
first time a line is applied to this base; if it was applied before, evidence on disk now was put again."""
import hashlib
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import Store  # noqa: E402
from herald.memory import store as store_module  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}
DATA = b"PRIVATE evidence bytes kiwi"
SHA = hashlib.sha256(DATA).hexdigest()


class Crash(Exception):
    pass


class Round4(Base):
    @property
    def journal(self):
        return self.root + ".erasures.jsonl"

    def blob_back(self):
        return os.path.exists(os.path.join(self.root, "archive", SHA))

    def copy_folder(self, name):
        self.s.close()
        dst = os.path.join(self.tmp.name, name)
        shutil.copytree(self.root, dst)
        return dst

    def put_folder_back(self, src):
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(src, self.root)

    # 1 (confirmed x2): a folder copy older than a repair
    def test_a_folder_copy_older_than_a_repair_still_loses_the_erased_evidence(self):
        y = self.s.add(rec("Y gone"), "y", **MAIN)
        self.s.erase(y, "a", OP, **MAIN)
        e = self.s.put_evidence(DATA, "n", "user_message", OP, **MAIN)
        copy = self.copy_folder("copy")
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.repair_erasure_journal(OP, **MAIN)     # every line rewritten: the copy's anchor is no prefix any more
        x = self.s.add(rec("X priv", sensitivity="private", evidence_ids=[e]), "x", **MAIN)
        self.s.erase(x, "a", OP, **MAIN)
        self.put_folder_back(copy)
        self.s = Store(self.root, clock=self.clock)
        self.assertFalse(self.blob_back(), "the union pass skipped the line's evidence")

    # 2 (confirmed x2)
    def test_merging_a_pending_erasure_keeps_evidence_main_put_again(self):
        e1 = self.s.put_evidence(DATA, "mail", "user_message", OP, **MAIN)
        r = self.s.add(rec("private R", sensitivity="private", evidence_ids=[e1]), "r", **MAIN)
        self.s.close()
        os.rename(self.journal, self.journal + ".away")
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(r, "asked", OP, **MAIN)
        e2 = self.s.put_evidence(DATA, "mail", "user_message", OP, **MAIN)
        self.assertEqual(e1, e2, "stand: the same id")
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec("cites it again", evidence_ids=[e2]), "y", **MAIN)

    # 3 (confirmed x2)
    def test_a_cuttable_journal_tail_is_cut_even_with_a_pending_file(self):
        x = self.s.add(rec("secret X"), "x", **MAIN)
        z = self.s.add(rec("other Z", predicate="likes"), "z", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "ab") as f:
            f.write(b"\x00" * 40 + b"\n")
        os.rename(self.journal, self.journal + ".away")
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(z, "asked", OP, **MAIN)
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        self.s = Store(self.root, clock=self.clock)     # must open and cut, as without a pending file
        self.assertIsNone(self.s.get(z, **MAIN))

    # 4 (confirmed x2): one rule for a whole pending entry without its newline
    def test_a_whole_pending_entry_without_newline_survives_the_next_pending_write(self):
        b = self.s.add(rec("b to erase"), "b", **MAIN)
        c = self.s.add(rec("c to erase", predicate="likes"), "c", **MAIN)
        snap = self.copy_folder("before")
        os.rename(self.journal, self.journal + ".away")
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(b, "asked", OP, **MAIN)
        with open(self.s.pending, "rb") as f:
            data = f.read()
        with open(self.s.pending, "wb") as f:
            f.write(data.rstrip(b"\n"))               # the newline of a whole entry was lost
        self.s.erase(c, "asked", OP, **MAIN)          # must not cut b's whole entry
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        shutil.rmtree(self.root)
        shutil.copytree(snap, self.root)              # a copy where b and c are alive
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(b, **MAIN), "b's erasure was cut as a torn piece")
        self.assertIsNone(self.s.get(c, **MAIN))

    # 5 (confirmed x2): a crash inside the repair
    def test_a_crash_inside_the_repair_still_drops_the_erased_evidence_on_the_next_open(self):
        f1 = self.s.add(rec("filler"), "f", **MAIN)
        self.s.erase(f1, "x", OP, **MAIN)
        e = self.s.put_evidence(DATA, "mail", "user_message", OP, **MAIN)
        c0 = self.copy_folder("c0")
        self.s = Store(self.root, clock=self.clock)
        p = self.s.add(rec("private P", sensitivity="private", evidence_ids=[e]), "p", **MAIN)
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(p, "asked", OP, **MAIN)            # pending, lists e
        self.put_folder_back(c0)
        self.s = Store(self.root, clock=self.clock)

        def boom(self_, entries, move_anchor=True):
            raise Crash()

        with mock.patch.object(store_module.Store, "_journal_append", boom):
            with self.assertRaises(Crash):
                self.s.repair_erasure_journal(OP, **MAIN)
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        if self.s._witness != "ok":
            self.s.repair_erasure_journal(OP, **MAIN)
        self.assertIsNone(self.s.get(p, **MAIN))
        self.assertFalse(self.blob_back(), "the evidence of the pending erasure survived a crash inside the repair")

    # 6 (confirmed x2): a foreign line still drops the erased record's evidence
    def test_a_foreign_line_still_drops_the_evidence_of_the_erased_record(self):
        self.s.add(rec("filler"), "f", **MAIN)
        e = self.s.put_evidence(DATA, "mail", "user_message", OP, **MAIN)
        c0 = self.copy_folder("c0")
        self.s = Store(self.root, clock=self.clock)
        x = self.s.add(rec("private X", sensitivity="private", evidence_ids=[e], memory_id="MEM-x000000000000001"),
                       "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        os.rename(self.journal, self.journal + ".away")
        shutil.rmtree(self.root)
        shutil.copytree(c0, self.root)
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec("new X other text", memory_id=x), "x2", **MAIN)
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.assertFalse(self.blob_back(), "a foreign line kept the erased record's evidence")


if __name__ == "__main__":
    unittest.main()
