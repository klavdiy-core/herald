"""p61, fourth control round on 6a34c3b (wf_d82bf437, 11.11.2026): one test per real finding, each red on 6a34c3b."""
import contextlib
import inspect
import os
import shutil
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import PolicyRefused, Store, ValidationError  # noqa: E402
from herald.memory import store as store_module  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}


class Crash(Exception):
    pass


class Round5(Base):
    @property
    def journal(self):
        return self.root + ".erasures.jsonl"

    def restore_db(self, snap_db):
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(snap_db, os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)

    # 1 (two lenses, confirmed x2 each)
    def test_reapplying_the_erasure_of_a_record_made_private_keeps_the_workers_own_evidence(self):
        w = self.s.put_evidence(b"worker's own notes", "chat", "user_message", OP)
        x = self.s.add(rec("X team fact", evidence_ids=[w]), "x")
        self.s.close()
        copy = os.path.join(self.tmp.name, "copy.sqlite")
        shutil.copy2(os.path.join(self.root, "memory.sqlite"), copy)
        self.s = Store(self.root, clock=self.clock)
        self.s.correct(x, 1, {"sensitivity": "private"}, "made private", OP, **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.add(rec("still citing my notes", evidence_ids=[w]), "control")      # stand: w lives on without a restore
        self.restore_db(copy)
        self.assertEqual(self.s.reapplied, 1)
        self.s.add(rec("citing my notes again", evidence_ids=[w], predicate="likes"), "after the restore")

    # 2 (confirmed x2)
    def test_a_failed_repair_leaves_workers_refused(self):
        x = self.s.add(rec("team fact X that main erased"), "x", **MAIN)
        self.s.close()
        c0 = os.path.join(self.tmp.name, "c0")
        shutil.copytree(self.root, c0)
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(c0, self.root)
        self.s = Store(self.root, clock=self.clock)
        real = store_module.Store._set_meta
        armed = {"on": False}

        def failing(self_, key, value):
            if armed["on"] and key == "tombstone_count":
                raise Crash("commit lost")
            return real(self_, key, value)

        real_rebuild = store_module.Store._rebuild_journal

        def rebuild_then_arm(self_, expected):
            out = real_rebuild(self_, expected)
            armed["on"] = True                  # the next tombstone_count write is the one at COMMIT
            return out

        with mock.patch.object(store_module.Store, "_set_meta", failing), \
                mock.patch.object(store_module.Store, "_rebuild_journal", rebuild_then_arm):
            with self.assertRaises(Crash):
                self.s.repair_erasure_journal(OP, **MAIN)
        with self.assertRaises(PolicyRefused):
            self.s.get(x, reader_scope="worker")

    # 3 (confirmed x2)
    def test_an_intent_left_by_a_failed_commit_is_not_served_by_an_open_store(self):
        x = self.s.add(rec("team fact X main erases"), "x", **MAIN)
        other = Store(self.root, clock=self.clock)          # another process, serving workers
        self.addCleanup(other.close)
        self.assertIsNotNone(other.get(x, reader_scope="worker"), "stand")
        orig = store_module.Store._journal_append

        def write_then_crash(self_, entries, move_anchor=True):
            orig(self_, entries, move_anchor)
            raise Crash("COMMIT-time failure")

        with mock.patch.object(store_module.Store, "_journal_append", write_then_crash):
            with self.assertRaises(Crash):
                self.s.erase(x, "asked", OP, **MAIN)
        try:
            served = other.get(x, reader_scope="worker")
        except PolicyRefused as err:
            served = None
            # reviewer M-21970: the refusal tells only that the journal is ahead - never the record, its keyed id, scope or birth
            with open(self.journal, "rb") as f:
                line = __import__("json").loads(f.read().rstrip(b"\n").rsplit(b"\n", 1)[-1])
            for secret in (x, line["id"], line["birth"], line["scope"]):
                self.assertNotIn(secret, str(err))
        self.assertIsNone(served, "an open store serves a record whose erasure main ordered")
        self.assertTrue(any("journal" in p for p in other.verify(**MAIN)), "verify(main) is silent about the intent")

    # 4 (confirmed x2)
    def test_two_processes_migrating_an_old_base_do_not_strip_births(self):
        self.s.add(rec("filler"), "f", **MAIN)
        self.s.close()
        c0 = os.path.join(self.tmp.name, "c0")
        shutil.copytree(self.root, c0)
        self.s = Store(self.root, clock=self.clock)
        a = self.s.add(rec("A secret", memory_id="MEM-a000000000000001"), "a", **MAIN)
        self.s.erase(a, "asked", OP, **MAIN)
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(c0, self.root)
        self.s = Store(self.root, clock=self.clock)          # A held only as an id mark
        self.s.close()
        db = os.path.join(self.root, "memory.sqlite")
        con = sqlite3.connect(db)
        keys = [r[0] for r in con.execute("SELECT id_key FROM erased_ids")]
        con.executescript("DROP TABLE erased_ids; CREATE TABLE erased_ids (id_key TEXT PRIMARY KEY);")
        con.executemany("INSERT INTO erased_ids VALUES (?)", [(k,) for k in keys])
        con.commit()
        con.close()
        old = os.path.join(self.tmp.name, "old")
        shutil.copytree(self.root, old)
        os.rename(self.journal, self.journal + ".away")
        self.s = Store(self.root, clock=self.clock)
        b = self.s.add(rec("B secret", predicate="likes"), "b", **MAIN)
        self.s.erase(b, "asked", OP, **MAIN)                  # pending: B's line reaches the journal only when merged
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        shutil.rmtree(self.root)
        shutil.copytree(old, self.root)                       # the old base without B, a pending file beside it
        real_writing = store_module.Store._writing
        state = {"done": False}

        @contextlib.contextmanager
        def writing(self_):
            if not state["done"] and inspect.stack()[2].function == "_migrate_erased_ids":
                state["done"] = True
                first = Store(self.root, clock=self.clock)    # the other process opens fully meanwhile
                first.close()
            with real_writing(self_):
                yield

        with mock.patch.object(store_module.Store, "_writing", writing):
            self.s = Store(self.root, clock=self.clock)
        births = [r[0] for r in self.s.db.execute("SELECT birth FROM erased_ids")]
        self.assertTrue(births and "" not in births, "a stale second migration stripped births: %r" % births)

    # 4b (my mutant r5_migrate_check_outside survived the stand above: there the second run re-reads a journal that
    # already holds every line, so it comes out the same). The decision must be taken under the lock: the other process
    # migrates and then erases C while the journal is away, so C's birth lives only in the base and the pending file.
    def test_a_stale_migration_does_not_strip_a_birth_held_only_in_the_pending_file(self):
        self.s.add(rec("filler"), "f", **MAIN)
        self.s.close()
        db = os.path.join(self.root, "memory.sqlite")
        con = sqlite3.connect(db)
        con.executescript("DROP TABLE erased_ids; CREATE TABLE erased_ids (id_key TEXT PRIMARY KEY);")
        con.commit()
        con.close()
        real_writing = store_module.Store._writing
        state = {"done": False}

        @contextlib.contextmanager
        def writing(self_):
            if not state["done"] and inspect.stack()[2].function == "_migrate_erased_ids":
                state["done"] = True
                with mock.patch.object(store_module.Store, "_writing", real_writing):
                    other = Store(self.root, clock=self.clock)       # migrates the base, holds the lock only for that
                    c = other.add(rec("C secret", predicate="likes"), "c", **MAIN)
                    os.rename(self.journal, self.journal + ".away")
                    other.erase(c, "asked", OP, **MAIN)                # C's line waits in the pending file
                    other.close()
                    os.rename(self.journal + ".away", self.journal)
                    pending = self.root + ".erasures.pending.jsonl"
                    with open(pending, "rb") as f:
                        line = __import__("json").loads(f.read().rstrip(b"\n").rsplit(b"\n", 1)[-1])
                    side = sqlite3.connect(db)                     # the mark a restored base without C would hold
                    side.execute("INSERT INTO erased_ids VALUES (?,?,?,?)", (line["id"], line["birth"], "main", "[]"))
                    side.commit()
                    side.close()
            with real_writing(self_):
                yield

        with mock.patch.object(store_module.Store, "_writing", writing):
            self.s = Store(self.root, clock=self.clock)                # read the old schema before the other process ran
        births = [r[0] for r in self.s.db.execute("SELECT birth FROM erased_ids")]
        self.assertTrue(births and "" not in births, "a migration decided outside the lock stripped births: %r" % births)


if __name__ == "__main__":
    unittest.main()
