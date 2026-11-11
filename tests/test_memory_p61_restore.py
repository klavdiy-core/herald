"""P61 red tests (R1a, R1b, R4, R10): an erasure must survive restoring an older copy.
Written red-first against 42da2cd. test author, 11.11.2026, by reviewer TZ p61 v2.
Each test names the reason it must be red on 42da2cd in its docstring."""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import Store, PolicyRefused  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}
SECRET = "zebra pancake quartz"


def _blob(x):
    return x if isinstance(x, str) else repr(x)


class Restore(Base):
    def snapshot(self, name):
        """Whole folder copy while no process holds the store."""
        self.s.close()
        dst = os.path.join(self.tmp.name, name)
        shutil.copytree(self.root, dst)
        self.s = Store(self.root, clock=self.clock)
        return dst

    def restore_db_only(self, snap):
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))

    def restore_folder(self, snap):
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(snap, self.root)

    def found(self, scope):
        return SECRET in _blob(self.s.search(SECRET, reader_scope=scope))

    def test_r1a_db_only_restore_keeps_erased_text_gone(self):
        """R1a: red on 42da2cd because the restored memory.sqlite brings X back and rebuilds the index."""
        mid = self.s.add(rec(SECRET), "x")
        snap = self.snapshot("copy_before_erase")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertFalse(self.found("main"), "stand: the erase itself must work before the restore")
        self.restore_db_only(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(mid, **MAIN), "X must stay gone for main")
        self.assertFalse(self.found("main"), "erased text is searchable by main after restore")
        self.assertFalse(self.found("worker"), "erased text is searchable by worker after restore")

    def test_r1b_folder_restore_keeps_erased_text_and_blob_gone(self):
        """R1b: red on 42da2cd because the whole folder copy brings the version rows and archive back."""
        mid = self.s.add(rec(SECRET), "x")
        snap = self.snapshot("folder_before_erase")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.restore_folder(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(mid, **MAIN), "X is back for main after a whole-folder restore")
        self.assertFalse(self.found("main"), "erased text is searchable after a whole-folder restore")
        archived = b""
        for d, _, names in os.walk(self.root):
            for n in names:
                if "memory.sqlite" in n:
                    continue
                with open(os.path.join(d, n), "rb") as f:
                    archived += f.read()
        self.assertNotIn(SECRET.encode(), archived, "erased text still sits in a file outside the db")

    def test_r4a_an_erased_id_stays_dead_after_restore(self):
        """R4a (reviewer M-21361): red on 42da2cd: the restored copy has no tombstone of X, so add(memory_id=X) with another
        text is accepted. The exception class name ErasedIdReused is provisional (author names it); any refusal that is
        not a plain success passes the 'raises' part, the name is checked only when herald.memory has it."""
        import herald.memory as hm
        snap = self.snapshot("copy_before_x_r4a")
        mid = self.s.add(rec(SECRET, memory_id="MEM-r4afixedid001"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.restore_db_only(snap)
        self.s = Store(self.root, clock=self.clock)
        want = getattr(hm, "ErasedIdReused", Exception)
        with self.assertRaises(want):
            self.s.add(rec("a new and different record", memory_id=mid), "reuse", **MAIN)
        self.assertIn(mid, _blob(self.s.verify(**MAIN)), "verify(main) must name the dead id")

    def test_r4b_a_foreign_x_with_another_birth_is_not_erased_and_is_named(self):
        """R4b (reviewer M-21361): red on 42da2cd at the verify line: X of B stays alive (fine), but nothing says 'id reused'.
        Two stores A and B; the same id, different text; X erased in A; B's database put in A's place, A's journal stays."""
        root_b = os.path.join(self.tmp.name, "store_b")
        b = Store(root_b, clock=self.clock)
        b.add(rec("text of the store B", memory_id="MEM-r4bfixedid001"), "b", **MAIN)
        b.close()
        mid = self.s.add(rec("text of the store A", memory_id="MEM-r4bfixedid001"), "a", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(root_b, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)
        got = self.s.get(mid, **MAIN)
        self.assertIsNotNone(got, "X of B must stay alive: its birth differs from the erased X of A")
        self.assertIn("text of the store B", _blob(got))
        self.assertIn("id reused", _blob(self.s.verify(**MAIN)))

    def test_r10_erased_text_is_refused_again_even_for_an_id_not_in_the_copy(self):
        """R10: red on 42da2cd because tombstones were rolled back with the db, so the same text is accepted."""
        snap = self.snapshot("copy_without_y")
        y = self.s.add(rec(SECRET), "y")
        self.s.erase(y, "asked", OP, **MAIN)
        self.restore_db_only(snap)
        self.s = Store(self.root, clock=self.clock)
        with self.assertRaises(PolicyRefused):
            self.s.add(rec(SECRET), "again")


if __name__ == "__main__":
    unittest.main()
