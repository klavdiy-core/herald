"""p61, second control round on ff047ad (wf_7349bc1e, 11.11.2026): one test per real finding, each red on ff047ad.
All seven sit in the paths the round-2 rework added: the pending file, journal lines rebuilt from id marks, blob drops."""
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


class Round3(Base):
    @property
    def journal(self):
        return self.root + ".erasures.jsonl"

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, clock=self.clock)

    def snap_folder(self, name):
        self.s.close()
        dst = os.path.join(self.tmp.name, name)
        shutil.copytree(self.root, dst)
        self.s = Store(self.root, clock=self.clock)
        return dst

    def restore_folder(self, snap):
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(snap, self.root)
        self.s = Store(self.root, clock=self.clock)

    def restore_db(self, snap):
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)

    # 1
    def test_a_torn_last_line_of_the_pending_file_is_an_unfinished_write(self):
        a = self.s.add(rec("erased into pending"), "a")
        b = self.s.add(rec("erase killed mid-write", predicate="likes"), "b")
        self.s.close()
        os.rename(self.journal, self.journal + ".away")
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(a, "asked", OP, **MAIN)

        def torn(self_, entry):
            with open(self_.pending, "ab") as out:
                out.write(b'{"at":"2026-11-0')
            raise KeyboardInterrupt("killed")

        with mock.patch.object(store_module.Store, "_pending_append", torn):
            with self.assertRaises(KeyboardInterrupt):
                self.s.erase(b, "asked", OP, **MAIN)
        self.s.close()
        os.rename(self.journal + ".away", self.journal)
        self.s = Store(self.root, clock=self.clock)      # must open: the torn piece never committed
        self.assertIsNone(self.s.get(a, **MAIN))
        self.assertIsNotNone(self.s.get(b, **MAIN), "b's erase was rolled back")

    # 2
    def test_evidence_listed_in_pending_survives_the_repair_and_goes_after_a_restore(self):
        e = self.s.put_evidence(b"private evidence bytes", "mail", "user_message", OP, **MAIN)
        sha = __import__("hashlib").sha256(b"private evidence bytes").hexdigest()
        snap = self.snap_folder("before_cite")
        p = self.s.add(rec("private with evidence", sensitivity="private", evidence_ids=[e]), "p", **MAIN)
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(p, "asked", OP, **MAIN)
        self.s.repair_erasure_journal(OP, **MAIN)
        self.restore_folder(snap)
        self.assertFalse(os.path.exists(os.path.join(self.root, "archive", sha)),
                         "the bytes of the erased private evidence came back: the repair dropped the pending blobs")

    # 3
    def test_a_line_rebuilt_from_an_id_mark_still_erases_the_record_in_an_older_copy(self):
        self.s.add(rec("alpha one"), "a")
        c0 = self.snap_folder("c0")
        x = self.s.add(rec("secret diagnosis", sensitivity="private"), "x", **MAIN)
        c1 = self.snap_folder("c1")
        self.s.erase(x, "asked", OP, **MAIN)
        self.restore_folder(c0)                 # X is held only as an id mark now
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.repair_erasure_journal(OP, **MAIN)   # the journal is rebuilt from what the base holds
        self.restore_folder(c1)                 # a copy where X is alive
        self.assertIsNone(self.s.get(x, **MAIN), "the erased record is back: its rebuilt line lost its birth")
        self.assertNotIn("diagnosis", repr(self.s.search("diagnosis", reader_scope="worker")))

    # 4
    def test_a_pending_erasure_of_a_reused_id_is_not_dropped_as_known(self):
        self.s.add(rec("filler"), "f")
        c0 = self.snap_folder("c0")
        x = self.s.add(rec("first text aardvark"), "x")
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "rb") as f:
            journal_with_x = f.read()
        shutil.rmtree(self.root)
        shutil.copytree(c0, self.root)
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)     # missing: nothing marks X in this base
        self.s.add(rec("second text pangolin", memory_id=x), "x2", **MAIN)
        c2 = self.snap_folder("c2")
        self.s.erase(x, "asked", OP, **MAIN)            # pending: X with ANOTHER birth
        self.s.close()
        with open(self.journal, "wb") as f:
            f.write(journal_with_x)
        self.s = Store(self.root, clock=self.clock)     # the pending line must not be dropped as "known"
        self.restore_folder(c2)                         # second X alive in the copy
        self.assertIsNone(self.s.get(x, **MAIN), "the erasure of the second X was dropped because the id was known")

    # 5
    def test_retrying_an_erase_after_its_commit_failed_works(self):
        x = self.s.add(rec("retry me walrus"), "x")
        real = store_module.Store._set_meta
        hit = {"v": False}

        def failing(self_, key, value):
            if key == "tombstone_count" and not hit["v"]:
                hit["v"] = True
                raise OSError("injected: commit lost")
            return real(self_, key, value)

        with mock.patch.object(store_module.Store, "_set_meta", failing):
            with self.assertRaises(OSError):
                self.s.erase(x, "asked", OP, **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)            # must not raise IntegrityError
        self.assertIsNone(self.s.get(x, **MAIN))

    # 6
    def test_a_journal_rollback_does_not_delete_evidence_main_put_again(self):
        self.s.add(rec("base one"), "y", **MAIN)
        self.s.close()
        c0 = os.path.join(self.tmp.name, "c0")
        os.makedirs(c0)
        shutil.copy2(os.path.join(self.root, "memory.sqlite"), os.path.join(c0, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)
        data = b"evidence-bytes-D"
        e1 = self.s.put_evidence(data, "mail", "user_message", OP, **MAIN)
        r = self.s.add(rec("secret R", sensitivity="private", evidence_ids=[e1]), "r", **MAIN)
        self.s.erase(r, "asked", OP, **MAIN)
        self.restore_db(c0)
        self.s.close()
        with open(self.journal, "rb") as f:
            j1 = f.read()
        self.s = Store(self.root, clock=self.clock)
        x = self.s.add(rec("other two"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        e2 = self.s.put_evidence(data, "mail-again", "user_message", OP, **MAIN)
        self.s.close()
        with open(self.journal, "wb") as f:
            f.write(j1)                                 # the journal alone rolled back
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec("uses e2", evidence_ids=[e2]), "y", **MAIN)     # e2 must still exist

    # 7
    def test_tombstones_of_a_foreign_line_go_back_into_a_rebuilt_journal(self):
        self.s.add(rec("filler"), "f", **MAIN)
        c0 = self.snap_folder("c0")
        with open(self.journal, "rb") as f:
            j0 = f.read()
        x = self.s.add(rec("first text aardvark", memory_id="MEM-x000000000000001"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "rb") as f:
            j_with_x = f.read()
        os.remove(self.journal)
        shutil.rmtree(self.root)
        shutil.copytree(c0, self.root)
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec("second text pangolin", memory_id=x), "x2", **MAIN)
        self.s.close()
        with open(self.journal, "wb") as f:
            f.write(j_with_x)
        self.s = Store(self.root, clock=self.clock)     # id reused: the first text is protected here
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("first text aardvark"), "again")
        self.s.close()
        with open(self.journal, "wb") as f:
            f.write(j0)                                 # the journal rolls back: the base writes what it knows into it
        self.s = Store(self.root, clock=self.clock)
        self.restore_folder(c0)
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("first text aardvark"), "again")


    # 4b: the merge itself must compare births: here the base is restored BEFORE the merge, so its ledger cannot rescue
    def test_a_pending_erasure_of_a_reused_id_survives_when_the_base_is_restored_before_the_merge(self):
        self.s.add(rec("filler"), "f")
        c0 = self.snap_folder("c0")
        x = self.s.add(rec("first text aardvark"), "x")
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "rb") as f:
            journal_with_x = f.read()
        shutil.rmtree(self.root)
        shutil.copytree(c0, self.root)
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec("second text pangolin", memory_id=x), "x2", **MAIN)
        c2 = self.snap_folder("c2")
        self.s.erase(x, "asked", OP, **MAIN)            # pending: X with another birth
        self.s.close()
        with open(self.journal, "wb") as f:
            f.write(journal_with_x)
        shutil.rmtree(self.root)
        shutil.copytree(c2, self.root)                  # restored before any open: the ledger knows nothing of it
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(x, **MAIN), "the pending erasure was dropped by an id-only merge")

    # 6b: evidence ids are deterministic: main putting the same bytes from the same source gets the same id again
    def test_a_journal_rollback_does_not_delete_the_same_evidence_main_put_again(self):
        self.s.add(rec("base one"), "y", **MAIN)
        self.s.close()
        c0 = os.path.join(self.tmp.name, "c0")
        os.makedirs(c0)
        shutil.copy2(os.path.join(self.root, "memory.sqlite"), os.path.join(c0, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)
        data = b"evidence-bytes-D"
        e1 = self.s.put_evidence(data, "mail", "user_message", OP, **MAIN)
        r = self.s.add(rec("secret R", sensitivity="private", evidence_ids=[e1]), "r", **MAIN)
        self.s.erase(r, "asked", OP, **MAIN)
        self.restore_db(c0)
        self.s.close()
        with open(self.journal, "rb") as f:
            j1 = f.read()
        self.s = Store(self.root, clock=self.clock)
        x = self.s.add(rec("other two"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        e2 = self.s.put_evidence(data, "mail", "user_message", OP, **MAIN)
        self.assertEqual(e2, e1, "stand: the same bytes and source give the same evidence id")
        self.s.close()
        with open(self.journal, "wb") as f:
            f.write(j1)                                 # the journal alone rolled back: a union pass
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec("uses e2", evidence_ids=[e2]), "y", **MAIN)

    # W13 (reviewer M-21698): a base written by ff047ad holds erased_ids(id_key) only; the new code must migrate it on open
    def test_a_base_with_the_old_erased_ids_table_opens_and_keeps_the_erasure(self):
        import sqlite3
        snap = self.snap_folder("without_x")
        x = self.s.add(rec("old schema secret", sensitivity="private"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.restore_db(snap)                       # x held only as an id mark now
        self.s.close()
        db = os.path.join(self.root, "memory.sqlite")
        con = sqlite3.connect(db)
        keys = [r[0] for r in con.execute("SELECT id_key FROM erased_ids")]
        self.assertEqual(len(keys), 1, "stand: one id mark")
        con.executescript("DROP TABLE erased_ids; CREATE TABLE erased_ids (id_key TEXT PRIMARY KEY);")
        con.executemany("INSERT INTO erased_ids VALUES (?)", [(k,) for k in keys])
        con.commit()
        con.close()
        self.s = Store(self.root, clock=self.clock)   # must open
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("old schema secret"), "again", **MAIN)
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("another text", memory_id=x), "same id", **MAIN)
        con = sqlite3.connect(db)
        births = [r[0] for r in con.execute("SELECT birth FROM erased_ids")]
        con.close()
        self.assertEqual(len(births), 1)
        self.assertNotEqual(births[0], "", "the birth was in the journal line and must be recovered from it")

    def test_an_old_id_mark_no_journal_line_names_is_kept_and_named_by_verify(self):
        import sqlite3
        x = self.s.add(rec("filler"), "x", **MAIN)
        self.s.close()
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        con.executescript("DROP TABLE erased_ids; CREATE TABLE erased_ids (id_key TEXT PRIMARY KEY);")
        con.execute("INSERT INTO erased_ids VALUES (?)", ("f" * 64,))
        con.commit()
        con.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertTrue(any("without a birth" in p for p in self.s.verify(**MAIN)), self.s.verify(**MAIN))
        self.assertIsNotNone(self.s.get(x, **MAIN))

    # reviewer M-21613 (4)
    def test_a_key_is_never_less_private_than_its_line(self):
        """reviewer M-21613 (4): a line of scope main whose key says 'worker' must not make the erased private text refusable
        to a worker (the refusal itself would tell the worker that such a private text existed)."""
        x = self.s.add(rec("private and erased", sensitivity="private"), "x", **MAIN)
        snap = os.path.join(self.tmp.name, "before")
        self.s.close()
        shutil.copytree(self.root, snap)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "rb") as f:
            raw = f.read().rstrip(b"\n")
        line = json.loads(raw)
        line["keys"] = [[k, "worker"] for k, _ in line["keys"]]
        forged = store_module._dumps(line).encode("utf-8")
        with open(self.journal, "wb") as f:
            f.write(forged + b"\n")
        self.s = Store(self.root, clock=self.clock)
        self.restore_folder(snap)
        self.s.add(rec("private and erased"), "a worker writes the same words")     # must NOT be refused


if __name__ == "__main__":
    unittest.main()
