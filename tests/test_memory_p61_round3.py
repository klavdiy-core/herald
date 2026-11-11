"""P61 round 3 on ff047ad: the reviewer's findings W10a, W10b, W12 (M-21614), one red test each (W10b in two cuts), written
by test author independently of his probes (his repro_ff/ files only told me the scenario). Red on ff047ad for the reason
in each docstring. test author, 11.11.2026."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import Store  # noqa: E402
from test_memory_core import Clock, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
WORKER = {"reader_scope": "worker"}
OP = {"id": "ENT-operator", "role": "operator"}


class Env(unittest.TestCase):
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

    @property
    def pending(self):
        return self.root + ".erasures.pending.jsonl"

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, clock=self.clock)

    def pending_with_one_erase(self):
        """Two records; the journal goes missing (the mark stays); the erase of the first one lands in pending."""
        a = self.s.add(rec("round three record alpha"), "x", **MAIN)
        b = self.s.add(rec("round three record beta", predicate="owns"), "x", **MAIN)
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(a, "asked", OP, **MAIN)
        self.assertTrue(os.path.exists(self.pending), "stand: an erase made without a journal waits in pending")
        return a, b

    def tear_pending(self, keep_of_a_new_line):
        """Append a piece of a further entry without its newline: an unfinished write."""
        with open(self.pending, "rb") as f:
            first = f.read().split(b"\n")[0]
        piece = first[:keep_of_a_new_line]
        with open(self.pending, "ab") as f:
            f.write(piece)
        return piece


class W10(Env):
    def test_w10a_a_torn_last_line_of_pending_does_not_kill_the_repair(self):
        """W10a: red on ff047ad: the repair of a missing journal reads pending, and the last unfinished write is meant to
        be skipped (its commit cannot have happened) but the check compares a line with a fresh split of the data, so it
        is never true: ErasureJournalCorrupt at line 2, the repair is dead."""
        a, b = self.pending_with_one_erase()
        self.s.close()
        self.tear_pending(20)
        self.s = Store(self.root, clock=self.clock)
        self.s.repair_erasure_journal(OP, **MAIN)
        self.assertIsNone(self.s.get(a, **MAIN), "the erase waiting in pending was lost")
        self.assertIsNotNone(self.s.get(b, **MAIN))

    def _second_erase_after_a_torn_tail(self, kept):
        a, b = self.pending_with_one_erase()
        self.s.close()
        self.tear_pending(kept)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(b, "asked", OP, **MAIN)
        with open(self.pending, "rb") as f:
            lines = [ln for ln in f.read().split(b"\n") if ln.strip()]
        for number, raw in enumerate(lines, 1):
            try:
                json.loads(raw.decode("utf-8"))
            except ValueError:
                self.fail("pending line %d is glued from two writes: %r" % (number, raw[-80:]))
        self.reopen()
        self.s.repair_erasure_journal(OP, **MAIN)
        self.assertIsNone(self.s.get(a, **MAIN))
        self.assertIsNone(self.s.get(b, **MAIN))

    def test_w10b_a_second_erase_does_not_glue_to_a_torn_pending_tail_short_piece(self):
        """W10b (cut at 1 byte, the line start without its newline): red on ff047ad: the append is "ab" straight after
        the unfinished piece, two writes become one broken line in the MIDDLE of the file, and the repair is dead."""
        self._second_erase_after_a_torn_tail(1)

    def test_w10b_a_second_erase_does_not_glue_to_a_torn_pending_tail_40_bytes(self):
        """W10b (cut at 40 bytes): the same, with a longer unfinished piece."""
        self._second_erase_after_a_torn_tail(40)


class W12(unittest.TestCase):
    X = b"round three evidence bytes X"

    def scenario(self, name, main_row, same_bytes):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root, clock = os.path.join(tmp.name, "s"), Clock()
        s = Store(root, clock=clock)
        w = s.put_evidence(self.X, "n1", "user_message", OP, **WORKER)
        s.close()
        snap = os.path.join(tmp.name, "snap")
        shutil.copytree(root, snap)
        s = Store(root, clock=clock)
        wr = s.add(rec("round three worker fact", evidence_ids=[w]), "x", **WORKER)
        if main_row:
            e = s.put_evidence(self.X if same_bytes else b"other bytes entirely", "n2", "user_message", OP, **MAIN)
            s.add(rec("round three private fact", evidence_ids=[e], sensitivity="private", predicate="owns"), "x", **MAIN)
        s.erase(wr, "asked", OP, **MAIN)
        s.close()
        shutil.rmtree(root)
        shutil.copytree(snap, root)
        s = Store(root, clock=clock)
        try:
            s.add(rec("round three probe", evidence_ids=[w], predicate="likes"), "x", **WORKER)
            return "usable"
        except Exception:
            return "gone"
        finally:
            s.close()

    def test_w12_the_fate_of_a_workers_evidence_does_not_tell_whether_main_holds_the_same_bytes(self):
        """W12 (privacy): red on ff047ad: a worker's evidence W is cited by the worker record WR; main puts evidence E
        with the SAME bytes under a private record; WR is erased, the folder restored. W stays usable for the worker only
        when main's row with the same bytes exists: the worker learns from the fate of its own evidence that main holds
        private bytes like those. The outcome must be the same with no main row, with a main row of other bytes and with a
        main row of the same bytes: the erased record's evidence is named in the journal line by its id, not by whether the
        bytes are still held."""
        no_main = self.scenario("a", False, True)
        other = self.scenario("b", True, False)
        same = self.scenario("c", True, True)
        self.assertEqual((no_main, other), ("gone", "gone"), "stand: the controls")
        self.assertEqual(same, "gone", "W stays usable for the worker only because main holds the same bytes")


class W13(Env):
    def test_w13_a_base_made_with_the_old_erased_ids_table_opens_and_keeps_the_erasure(self):
        """W13 (reviewer M-21700): red on 9213f18: erased_ids had one column (id_key) in ff047ad, four now; CREATE TABLE IF NOT
        EXISTS leaves the old table as it was, and the first sync reads a column that is not there (OperationalError: no
        such column: birth): a base made one version earlier does not open at all. The erasure it held must hold."""
        import sqlite3
        from herald.memory import PolicyRefused
        empty_copy = os.path.join(self.tmp.name, "w13_empty")
        self.s.close()
        shutil.copytree(self.root, empty_copy)
        self.s = Store(self.root, clock=self.clock)
        mid = self.s.add(rec("round three text of the erased one", memory_id="MEM-w13recordxxx001"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(empty_copy, self.root)                   # a base from before the record: the journal brings the mark
        self.s = Store(self.root, clock=self.clock)
        self.s.close()
        db = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        marks = [r[0] for r in db.execute("SELECT id_key FROM erased_ids")]
        self.assertEqual(len(marks), 1, "stand: the reapply left one erased id mark in the base")
        db.execute("DROP TABLE erased_ids")
        db.execute("CREATE TABLE erased_ids (id_key TEXT PRIMARY KEY)")      # the shape ff047ad made
        db.execute("INSERT INTO erased_ids VALUES (?)", (marks[0],))
        db.execute("UPDATE meta SET value='0' WHERE key='journal_seq'")       # and the base is behind its journal: the
        db.execute("UPDATE meta SET value='' WHERE key='journal_head'")       # next open applies the line again (full pass)
        db.commit()
        db.close()
        self.s = Store(self.root, clock=self.clock)               # must open
        self.assertIsNone(self.s.get(mid, **MAIN))
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("round three text of the erased one", memory_id="MEM-w13otherid0001"), "again", **MAIN)
        self.assertEqual(self.s.verify_journal(**MAIN), [], "the migrated base is not clean for main")


class Merge(Env):
    def test_r3a_a_pending_line_with_the_same_id_but_another_birth_is_still_merged(self):
        """R3a (mutant: merge compares ids only): the journal knows an erasure of id I born B1; pending holds an erasure
        of the same id born B2 (another store's record under that id). The pending line is another erasure and must reach
        the journal, whatever the id says."""
        x = self.s.add(rec("round three merge alpha", memory_id="MEM-r3arecordxxx001"), "x", **MAIN)
        y = self.s.add(rec("round three merge beta", memory_id="MEM-r3arecordyyy001", predicate="owns"), "y", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        with open(self.journal, "rb") as f:
            first = json.loads([ln for ln in f.read().split(b"\n") if ln.strip()][0].decode("utf-8"))
        self.s.close()
        entry = {"id": first["id"], "birth": "another-birth-b2", "keys": [], "scope": "worker", "at": first["at"]}
        with open(self.pending, "wb") as f:
            f.write(json.dumps(entry, sort_keys=True).encode("utf-8") + b"\n")
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(y, "asked", OP, **MAIN)                      # a write: the sync merges pending first
        with open(self.journal, "rb") as f:
            lines = [json.loads(ln.decode("utf-8")) for ln in f.read().split(b"\n") if ln.strip()]
        births = [ln["birth"] for ln in lines]
        self.assertIn("another-birth-b2", births, "a pending erasure of another birth was dropped as 'known' by its id")
        self.assertFalse(os.path.exists(self.pending))


    def test_r3b_evidence_listed_only_by_a_pending_line_survives_the_rebuild_of_the_journal(self):
        """R3b (mutant: the rebuild drops the evidence of the pending line): the erase of WR was made while the journal was
        missing, so only its pending line lists the evidence W it removed; the base's ledger cannot. The repair writes the
        journal from the ledger, and the pending evidence must be added to that line, or a copy of the folder from before
        the erase brings W back with nothing to remove it."""
        w = self.s.put_evidence(b"round three evidence for r3b", "n1", "user_message", OP, **WORKER)
        self.s.close()
        snap = os.path.join(self.tmp.name, "r3b_copy")
        shutil.copytree(self.root, snap)                          # W is in the copy, the record that cites it is not
        self.s = Store(self.root, clock=self.clock)
        wr = self.s.add(rec("round three r3b worker fact", evidence_ids=[w]), "x", **WORKER)
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(wr, "asked", OP, **MAIN)                     # into pending, with the evidence it removed
        self.assertTrue(os.path.exists(self.pending), "stand: the erase waits in pending")
        self.s.repair_erasure_journal(OP, **MAIN)
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(snap, self.root)                          # the folder comes back from before the erase
        self.s = Store(self.root, clock=self.clock)
        from herald.memory import ValidationError
        with self.assertRaises(ValidationError) as caught:
            self.s.add(rec("round three r3b probe", evidence_ids=[w], predicate="likes"), "x", **WORKER)
        self.assertIn("no such evidence", str(caught.exception))


class MigratedMark(Env):
    def test_r4a_a_migrated_erased_id_mark_still_refuses_the_id_to_a_new_record(self):
        """R4a on a migrated base (reviewer M-21908, a teammate M-21888): a mark made before the birth column was added (old shape,
        id_key only) is migrated with an empty birth. A new record under that id, whatever its birth, must still be
        refused (ErasedIdReused): a check by (id_key, birth) would let the erased id come back."""
        import sqlite3
        from herald.memory import ErasedIdReused
        empty_copy = os.path.join(self.tmp.name, "r4a_empty")
        self.s.close()
        shutil.copytree(self.root, empty_copy)
        self.s = Store(self.root, clock=self.clock)
        mid = self.s.add(rec("round three record for r4a migrated", memory_id="MEM-r4amigrated001"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(empty_copy, self.root)                    # a base from before the record: the journal brings the mark
        self.s = Store(self.root, clock=self.clock)
        self.s.close()
        db = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        marks = [r[0] for r in db.execute("SELECT id_key FROM erased_ids")]
        self.assertEqual(len(marks), 1, "stand: one mark in the base")
        db.execute("DROP TABLE erased_ids")
        db.execute("CREATE TABLE erased_ids (id_key TEXT PRIMARY KEY)")      # the shape ff047ad made
        db.execute("INSERT INTO erased_ids VALUES (?)", (marks[0],))
        db.execute("UPDATE meta SET value='0' WHERE key='journal_seq'")      # no journal line names the id any more: the
        db.execute("UPDATE meta SET value='' WHERE key='journal_head'")       # migration cannot find its birth
        db.commit()
        db.close()
        with open(self.journal, "wb") as f:
            f.write(b"")
        self.s = Store(self.root, clock=self.clock)               # the migration runs here: the mark keeps an empty birth
        rows = [(r["id_key"], r["birth"]) for r in self.s.db.execute("SELECT id_key, birth FROM erased_ids")]
        self.assertEqual(rows, [(marks[0], "")], "stand: migrated, and the birth is empty")
        with self.assertRaises(ErasedIdReused):
            self.s.add(rec("a different new text under the erased id", memory_id="MEM-r4amigrated001"), "again", **MAIN)


class KeyScope(Env):
    def test_k4_a_key_in_a_main_line_is_never_less_private_than_its_line(self):
        """K4 (reviewer M-21614 #4, low: needs a hand on the journal): a journal line of a PRIVATE erasure whose key is
        marked 'worker' would make the erased private text refused to a worker, and a refusal tells the worker that text
        existed. The scope of a key comes back as the maximum of its own and its line's. Red on ff047ad."""
        text = "round three private text for k4"
        empty_copy = os.path.join(self.tmp.name, "k4_empty")
        self.s.close()
        shutil.copytree(self.root, empty_copy)
        self.s = Store(self.root, clock=self.clock)
        mid = self.s.add(rec(text, memory_id="MEM-k4recordxxxx001", sensitivity="private"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "rb") as f:
            raw = [ln for ln in f.read().split(b"\n") if ln.strip()]
        self.assertEqual(len(raw), 1, "stand: one line, so its chain is just seq 1 and an empty prev_hash")
        line = json.loads(raw[0].decode("utf-8"))
        self.assertEqual(line["scope"], "main", "stand: a private record is erased in the main scope")
        self.assertTrue(line["keys"], "stand: the line carries content keys")
        line["keys"] = [[k, "worker"] for k, _ in line["keys"]]
        with open(self.journal, "wb") as f:
            f.write(json.dumps(line, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
        shutil.rmtree(self.root)
        shutil.copytree(empty_copy, self.root)                    # a base from before the record: the line is applied again
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec(text, predicate="likes"), "w", **WORKER)   # a worker may write it


if __name__ == "__main__":
    unittest.main()
