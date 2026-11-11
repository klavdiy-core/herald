"""p61, control round on 4d77fa2 (wf_b05fc4d9, 11.11.2026): one test per real finding, each red on 4d77fa2.
The root of all seven: the code trusted a cached witness state and the meta anchor instead of the journal file under the lock."""
import hashlib
import json
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import PolicyRefused, Store  # noqa: E402
from herald.memory import store as store_module  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}


class Round2(Base):
    @property
    def journal(self):
        return self.root + ".erasures.jsonl"

    def lines(self):
        with open(self.journal, "rb") as f:
            return [l for l in f.read().split(b"\n") if l]

    def snapshot(self, name):
        self.s.close()
        dst = os.path.join(self.tmp.name, name)
        shutil.copytree(self.root, dst)
        self.s = Store(self.root, clock=self.clock)
        return dst

    def restore_db(self, snap):
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))

    def text_found(self, text, scope="main"):
        return text in repr(self.s.search(text, reader_scope=scope))

    # 1 (confirmed x2)
    def test_an_erase_while_the_journal_is_away_survives_its_return_and_a_restore(self):
        mid = self.s.add(rec("quokka secret one", sensitivity="private"), "x", **MAIN)
        snap = self.snapshot("before")
        self.s.close()
        os.rename(self.journal, self.journal + ".away")
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.restore_db(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(mid, **MAIN), "the erase made while the journal was away came back")
        self.assertFalse(self.text_found("quokka"))

    # 1b (confirmed, variant 2)
    def test_an_erase_while_the_journal_is_gone_survives_a_restore_and_the_repair(self):
        mid = self.s.add(rec("quokka secret two", sensitivity="private"), "x", **MAIN)
        snap = self.snapshot("before")
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.restore_db(snap)
        self.s = Store(self.root, clock=self.clock)
        self.s.repair_erasure_journal(OP, **MAIN)
        self.assertIsNone(self.s.get(mid, **MAIN), "repair rebuilt the journal from the restored ledger only")
        self.assertFalse(self.text_found("quokka"))

    # 2 (confirmed x2, two lenses)
    def test_a_failed_commit_leaves_an_intent_that_the_next_erase_completes_and_chains(self):
        a = self.s.add(rec("first to go"), "a")
        b = self.s.add(rec("second to go", predicate="likes"), "b")
        snap = self.snapshot("before")
        real = store_module.Store._set_anchor
        calls = {"n": 0}

        def fail_once(self_, seq, head):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("commit lost after the journal fsync")
            return real(self_, seq, head)

        with mock.patch.object(store_module.Store, "_set_anchor", fail_once):
            with self.assertRaises(OSError):
                self.s.erase(a, "asked", OP, **MAIN)
        self.s.erase(b, "asked", OP, **MAIN)
        self.assertEqual([json.loads(l)["seq"] for l in self.lines()], [1, 2], "a duplicate seq was written")
        self.assertIsNone(self.s.get(a, **MAIN), "main ordered a's erase: its intent must be completed")
        self.assertEqual(self.s.verify_journal(**MAIN), [])
        self.restore_db(snap)
        self.s = Store(self.root, clock=self.clock)       # must open: the chain is whole
        self.assertIsNone(self.s.get(a, **MAIN))
        self.assertIsNone(self.s.get(b, **MAIN))

    # 3 (confirmed x2)
    def test_a_store_opened_while_missing_journals_its_erase_after_another_process_repaired(self):
        x = self.s.add(rec("held by the first process"), "x")
        self.s.close()
        os.remove(self.journal)
        first = Store(self.root, clock=self.clock)
        second = Store(self.root, clock=self.clock)
        second.repair_erasure_journal(OP, **MAIN)
        before = len(self.lines())
        first.erase(x, "asked", OP, **MAIN)
        first.close()
        self.s = second
        self.assertEqual(len(self.lines()), before + 1, "the erase of a store opened while missing left no line")

    # 4
    def test_reapply_uses_the_scope_at_erasure_not_the_scope_in_the_copy(self):
        anchor_worker = self.s.add(rec("a worker fact that stays"), "w")
        x = self.s.add(rec("shared at first, private later"), "x")
        snap = self.snapshot("before")
        self.s.correct(x, 1, {"sensitivity": "private"}, "made private", OP, **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.restore_db(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(self.s.reapplied, 1)
        self.assertEqual(self.s.erasure_ledger(x), [], "the worker sees the ledger of an erasure of a private record")
        b = self.s.add(rec("a worker fact after"), "b")
        # worker chain in the copy: add(anchor_worker) n=1, add(x) n=2 (x was shared then); b must be n=3, a private
        # erasure applied again must not take n=3 for itself
        self.assertEqual(self.s.events(anchor_worker)[-1]["n"], 1)
        self.assertEqual(self.s.events(b)[0]["n"], 3, "the worker chain counted a private erasure")

    # 5
    def test_a_whole_last_line_without_its_newline_is_an_intent_not_garbage(self):
        x = self.s.add(rec("erased, then the newline was lost"), "x")
        snap = self.snapshot("before")
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "rb") as f:
            data = f.read()
        with open(self.journal, "wb") as f:
            f.write(data.rstrip(b"\n"))
        self.restore_db(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(x, **MAIN), "a whole line was cut off as torn and the erase came back")

    # 6
    def test_a_second_opener_of_a_new_store_never_truncates_the_journal(self):
        x = self.s.add(rec("erased by the winner"), "x")
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        stale = {os.path.normcase(self.journal), os.path.normcase(os.path.join(self.root, "erasures.born"))}
        real_exists, real_count = os.path.exists, store_module.Store._count
        looked = set()

        def stale_exists(p):            # the loser looked before the winner created the journal and the mark
            key = os.path.normcase(str(p))
            if key in stale and key not in looked:
                looked.add(key)
                return False
            return real_exists(p)

        def empty_count(self_, table):  # ...and before the winner wrote anything into the base
            return 0 if table in ("records", "erasure_ledger") and len(looked) < 3 else real_count(self_, table)

        with mock.patch.object(store_module.os.path, "exists", stale_exists), \
                mock.patch.object(store_module.Store, "_count", empty_count):
            try:
                other = Store(self.root, clock=self.clock)
                other.close()
            except Exception:           # noqa: BLE001  refusing is fine; truncating is not
                pass
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(len(self.lines()), 1, "the journal was truncated by the second opener")

    # mutant append_from_anchor (round 2): pending erasures merged while the base lags behind the journal must continue
    # the FILE's chain, not the base's anchor
    def test_pending_merged_into_a_journal_ahead_of_a_restored_base_continues_the_file_chain(self):
        y = self.s.add(rec("erased with the journal there"), "y")
        x = self.s.add(rec("erased while the journal was away", predicate="likes"), "x")
        snap = self.snapshot("before_both")             # anchor of the copy: (0, "")
        self.s.erase(y, "asked", OP, **MAIN)            # journal line 1
        self.s.close()
        os.rename(self.journal, self.journal + ".away")
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(x, "asked", OP, **MAIN)            # pending
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        self.restore_db(snap)                           # the base lags: anchor 0 while the file holds line 1
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual([json.loads(l)["seq"] for l in self.lines()], [1, 2], "the pending line broke the chain")
        self.assertIsNone(self.s.get(y, **MAIN))
        self.assertIsNone(self.s.get(x, **MAIN))
        self.assertEqual(self.s.verify_journal(**MAIN), [])

    # 7
    def test_a_journal_deleted_while_running_is_not_silently_recreated(self):
        x = self.s.add(rec("erased after the journal vanished"), "x")
        os.remove(self.journal)
        self.s.erase(x, "asked", OP, **MAIN)
        self.assertFalse(os.path.exists(self.journal) and self.lines() and json.loads(self.lines()[0])["seq"] > 1,
                         "a new journal started mid-chain")
        with self.assertRaises(PolicyRefused):
            self.s.get(x, reader_scope="worker")
        self.assertIn("erasure witness missing", self.s.verify(**MAIN)[0])


if __name__ == "__main__":
    unittest.main()
