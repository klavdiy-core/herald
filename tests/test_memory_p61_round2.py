"""P61 round 2: seven real findings of the author's control round on 4d77fa2 (M-21459), one red test each, written by
test author independently of his tests (reviewer M-21462). Order: finding 4, then 1 and 7, then 2, 3, 6, 5.
Every test here must be RED on 4d77fa2 for the reason in its docstring. test author, 11.11.2026."""
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
TEXT = "round two record zebra"


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

    def copy_folder(self, name):
        self.s.close()
        dst = os.path.join(self.tmp.name, name)
        shutil.copytree(self.root, dst)
        self.s = Store(self.root, clock=self.clock)
        return dst

    def put_db_back(self, snap):
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))

    def journal_lines(self):
        with open(self.journal, "rb") as f:
            return [ln for ln in f.read().split(b"\n") if ln.strip()]


class Finding4(Env):
    def test_f4_scope_of_a_reapplied_erase_comes_from_the_journal_line_not_from_the_copy(self):
        """F4: red on 4d77fa2: the record was worker-scope in the copy and became private (main) before its erase; the
        reapply takes the scope from the copy, so its erase_reapplied event goes into the WORKER chain and the ledger
        becomes visible to a worker, while the journal line says main."""
        mid = self.s.add(rec(TEXT, memory_id="MEM-f4recordaaaa001", sensitivity="internal"), "x", **MAIN)
        snap = self.copy_folder("f4_copy")
        self.s.correct(mid, 1, {"sensitivity": "private"}, "became private", OP, **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.put_db_back(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(mid, **MAIN), "stand: the record must be gone for main")
        self.assertEqual(self.s.events(mid, **WORKER), [], "a worker sees an event of a record that was private when erased")
        self.assertEqual(self.s.erasure_ledger(mid, **WORKER), [], "a worker sees the erasure ledger of a private record")


class Finding1and7(Env):
    def test_f1_an_erase_made_while_the_witness_is_missing_survives_the_journal_coming_back(self):
        """F1: red on 4d77fa2: while the journal is missing the erase goes into no journal; when an older journal comes
        back and the database is restored from a copy before the erase, X is back and verify says nothing."""
        mid = self.s.add(rec(TEXT, memory_id="MEM-f1recordaaaa001"), "x", **MAIN)
        snap = self.copy_folder("f1_copy")
        with open(self.journal, "rb") as f:
            old_journal = f.read()
        self.s.close()
        os.remove(self.journal)                                  # the witness goes missing
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(mid, "asked", OP, **MAIN)                   # main still works, the erase is real
        self.s.close()
        with open(self.journal, "wb") as f:                      # the old journal comes back (or a repair makes one)
            f.write(old_journal)
        self.put_db_back(snap)                                   # and the database is restored from the copy before the erase
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(mid, **MAIN), "an erase made while the witness was missing came back")

    def test_f7_a_journal_deleted_under_an_open_store_is_not_quietly_made_again(self):
        """F7: red on 4d77fa2: the next erase creates a new journal with seq N+1 beside the missing state; after reopen
        nothing says the witness was lost (workers are served, verify(main) is silent)."""
        a = self.s.add(rec(TEXT, memory_id="MEM-f7recordaaaa001"), "a", **MAIN)
        b = self.s.add(rec(TEXT + " two", memory_id="MEM-f7recordbbbb001", predicate="owns"), "b", **MAIN)
        self.s.erase(a, "asked", OP, **MAIN)
        os.remove(self.journal)                                  # deleted while the store stays open
        self.s.erase(b, "asked", OP, **MAIN)
        self.reopen()
        problems = self.s.verify(**MAIN)
        self.assertTrue(any("erasure witness" in p or "journal" in p for p in problems), problems)


class Finding2(Env):
    def test_f2_a_failed_commit_after_the_journal_line_does_not_give_the_next_erase_the_same_seq(self):
        """F2: red on 4d77fa2: the commit of an erase fails after its journal line is on disk; the next erase takes seq
        from the anchor in meta (rolled back) and not from the file: two lines with one seq; after a restore from a copy the
        store does not open (ErasureJournalCorrupt)."""
        from unittest import mock
        x = self.s.add(rec(TEXT, memory_id="MEM-f2recordxxxx001"), "x", **MAIN)
        y = self.s.add(rec(TEXT + " two", memory_id="MEM-f2recordyyyy001", predicate="owns"), "y", **MAIN)
        snap = self.copy_folder("f2_copy")
        real = Store._set_meta
        state = {"hit": False}

        def failing(store, key, value):
            if key == "tombstone_count" and not state["hit"]:
                state["hit"] = True
                raise OSError("injected: the commit does not happen")
            return real(store, key, value)

        with mock.patch.object(Store, "_set_meta", failing):
            with self.assertRaises(OSError):
                self.s.erase(x, "asked", OP, **MAIN)
        self.assertEqual(len(self.journal_lines()), 1, "stand: the line of the failed erase is on disk")
        self.s.erase(y, "asked", OP, **MAIN)
        self.put_db_back(snap)
        self.s = Store(self.root, clock=self.clock)           # must open; the journal knows both erasures
        self.assertIsNone(self.s.get(y, **MAIN), "the erase of Y is lost")
        self.assertIsNone(self.s.get(x, **MAIN), "the intent of the failed erase of X (ordered by main) is lost")
        seqs = [json.loads(ln)["seq"] for ln in self.journal_lines()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)), "seq in the file must be 1..n without a repeat")


class Finding3(Env):
    def test_f3_a_store_opened_before_another_process_migrated_still_writes_its_erase_to_the_journal(self):
        """F3: red on 4d77fa2: S1 opened a legacy base (cached witness 'legacy'); S2 migrated; S1 erases X and writes no
        journal line (the check is on the cached state); restoring the database from a copy before the erase brings X back."""
        x = self.s.add(rec(TEXT, memory_id="MEM-f3recordxxxx001"), "x", **MAIN)
        self.s.close()
        os.remove(self.journal)
        os.remove(os.path.join(self.root, "erasures.born"))
        s1 = Store(self.root, clock=self.clock)                # legacy: records and no journal
        self.addCleanup(lambda: getattr(s1, "close", lambda: None)())
        s2 = Store(self.root, clock=self.clock)
        s2.migrate_legacy(actor=OP, reader_scope="main")        # another process repairs the witness
        s2.close()
        snap = os.path.join(self.tmp.name, "f3_copy")
        shutil.copytree(self.root, snap)                        # journal and born exist; X alive
        s1.erase(x, "asked", OP, **MAIN)                        # S1 still believes 'legacy'
        s1.close()
        self.s = s1
        self.put_db_back(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(x, **MAIN), "an erase made through a store with a stale witness left no trace")


class Finding6(Env):
    def test_f6_a_second_opener_of_a_new_store_does_not_wipe_the_journal(self):
        """F6: red on 4d77fa2: two processes open a new store; the loser (which looked before the winner wrote) writes an
        empty journal over the winner's, after the winner has erased: the erasure line is gone. Simulated: the loser's two
        looks (files absent, no records) are forced; the journal must keep its line."""
        from unittest import mock
        x = self.s.add(rec(TEXT, memory_id="MEM-f6recordxxxx001"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        before = self.journal_lines()
        self.assertEqual(len(before), 1, "stand: one erase line")
        real_exists = os.path.exists
        journal, born = self.journal, os.path.join(self.root, "erasures.born")

        def blind(p):
            return False if p in (journal, born) else real_exists(p)

        loser = None
        try:
            with mock.patch("os.path.exists", blind), mock.patch.object(Store, "_count", lambda store, table: 0):
                try:
                    loser = Store(self.root, clock=self.clock)
                except Exception:
                    loser = None                                # refusing to open is fine; wiping is not
        finally:
            if loser is not None:
                loser.close()
        with open(self.journal, "rb") as f:
            after = [ln for ln in f.read().split(b"\n") if ln.strip()]
        self.assertEqual(after, before, "the journal of the winner was overwritten")


class Finding5(Env):
    def test_f5_a_whole_last_line_without_a_newline_is_completed_not_cut(self):
        """F5: red on 4d77fa2: the last journal line is complete JSON but lacks the final newline; it is cut as torn, and
        a restore from a copy before the erase brings X back. TZ p.3: an intent on disk is completed."""
        x = self.s.add(rec(TEXT, memory_id="MEM-f5recordxxxx001"), "x", **MAIN)
        snap = self.copy_folder("f5_copy")
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.endswith(b"\n"))
        with open(self.journal, "wb") as f:
            f.write(raw[:-1])                                   # whole line, newline missing
        self.put_db_back(snap)
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNone(self.s.get(x, **MAIN), "a whole journal line without a newline was thrown away")


class Findings2459(Env):
    def test_w2_bytes_of_a_failed_journal_write_do_not_glue_to_the_next_line(self):
        """W2 (reviewer M-21514): red on 4d77fa2: a failed write left a torn piece (no newline) in a LIVE store; the next erase
        in the same store appends right after it; reopen -> ErasureJournalCorrupt 'not a JSON line'."""
        self.s.add(rec(TEXT, memory_id="MEM-w2recordxxxx001"), "x", **MAIN)
        y = self.s.add(rec(TEXT + " two", memory_id="MEM-w2recordyyyy001", predicate="owns"), "y", **MAIN)
        with open(self.journal, "ab") as f:
            f.write(b'{"at":"2026-11-08T10:00:00Z","birth":"abc","id":"torn-piece')       # no newline: a failed write
        self.s.erase(y, "asked", OP, **MAIN)                                            # same store, still open
        self.s.close()
        self.s = Store(self.root, clock=self.clock)                                     # must open
        self.assertIsNone(self.s.get(y, **MAIN), "the erase made after the torn piece is lost")

    def test_w4_repair_keeps_what_the_base_knew_about_an_erased_record_it_never_held(self):
        """W4 (reviewer M-21514): red on 4d77fa2: X is added after copy C1 and erased; after restoring C1 the base holds only
        the JRN tombstone and the erased id (no ledger row); the journal is lost and repaired from the ledger alone, so the
        knowledge is dropped; after restoring C1 again the erased TEXT is accepted."""
        from herald.memory import PolicyRefused
        self.s.add(rec("plain filler word w4"), "f", **MAIN)
        snap = self.copy_folder("w4_c1")
        x = self.s.add(rec(TEXT + " w4", memory_id="MEM-w4recordxxxx001"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.put_db_back(snap)
        self.s = Store(self.root, clock=self.clock)                                     # reapply: tombstone and id of X return
        with self.assertRaises(PolicyRefused):
            self.s.add(rec(TEXT + " w4"), "again", **MAIN)                              # stand: refused before the repair
        self.s.close()
        os.remove(self.journal)                                                         # the journal is lost
        self.s = Store(self.root, clock=self.clock)
        self.s.repair_erasure_journal(OP, **MAIN)
        self.put_db_back(snap)                                                          # C1 again
        self.s = Store(self.root, clock=self.clock)
        with self.assertRaises(PolicyRefused):
            self.s.add(rec(TEXT + " w4"), "once more", **MAIN)

    def test_w5_garbage_after_the_anchor_at_the_end_of_the_file_is_cut_like_a_torn_tail(self):
        """W5 (reviewer M-21514): red on 4d77fa2: 40 NUL bytes and a newline after a correct journal (past the anchor, never
        committed) make reopen raise ErasureJournalCorrupt; such a tail is an unfinished write, not a ruined base."""
        x = self.s.add(rec(TEXT + " w5", memory_id="MEM-w5recordxxxx001"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        with open(self.journal, "ab") as f:
            f.write(b"\x00" * 40 + b"\n")
        self.s = Store(self.root, clock=self.clock)                                     # must open
        self.assertIsNone(self.s.get(x, **MAIN))

    def test_w9_a_damaged_anchor_range_is_named_not_a_bare_index_error(self):
        """W9 (reviewer M-21514): red on 4d77fa2: journal_seq in meta is -2 (damaged); the open dies with a bare IndexError
        (or, for -1 / 0 with a matching head, passes silently). It must be an ErasureJournalCorrupt or a note in verify."""
        import sqlite3
        from herald.memory import ErasureJournalCorrupt
        x = self.s.add(rec(TEXT + " w9", memory_id="MEM-w9recordxxxx001"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        try:
            con.execute("UPDATE meta SET value='-2' WHERE key='journal_seq'")
            con.commit()
        finally:
            con.close()
        try:
            self.s = Store(self.root, clock=self.clock)
        except ErasureJournalCorrupt:
            return                                                                      # a typed refusal is fine
        problems = self.s.verify(**MAIN)
        self.assertTrue(any("journal" in p for p in problems), "an anchor out of range passed without a trace: %r" % problems)


class Finding9Zero(Env):
    def test_w9_seq_zero_with_a_non_empty_head_is_noted_not_taken_silently(self):
        """W9 (reviewer M-21518): red on 4d77fa2: journal_seq=0 with the head of the last journal line passes silently
        (reapplied 0, no trace); a fix that checks only negative numbers would leave this open."""
        import hashlib
        import sqlite3
        x = self.s.add(rec(TEXT + " w9zero", memory_id="MEM-w9zerorecord0001"), "x", **MAIN)
        self.s.erase(x, "asked", OP, **MAIN)
        self.s.close()
        last = self.journal_lines()[-1]
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        try:
            con.execute("UPDATE meta SET value='0' WHERE key='journal_seq'")
            con.execute("UPDATE meta SET value=? WHERE key='journal_head'", (hashlib.sha256(last).hexdigest(),))
            con.commit()
        finally:
            con.close()
        from herald.memory import ErasureJournalCorrupt
        try:
            self.s = Store(self.root, clock=self.clock)
        except ErasureJournalCorrupt:
            return
        problems = self.s.verify(**MAIN)
        self.assertTrue(any("journal" in p or "anchor" in p for p in problems), "seq 0 with a head passed silently: %r" % problems)


class Findings6and7(Env):
    def test_w6_evidence_bytes_cited_by_an_erased_private_record_do_not_come_back_with_the_folder(self):
        """W6 (reviewer M-21503): red on 4d77fa2: the journal line carries no evidence; the folder copy (made before the
        private record cited the evidence) brings the evidence row and its bytes back, and nothing removes them."""
        data = b"evidence bytes cited by a private record: mango"
        e = self.s.put_evidence(data, "note.md", "tool_output", OP, **MAIN)
        import hashlib
        blob = os.path.join(self.root, "archive", hashlib.sha256(data).hexdigest())
        snap = self.copy_folder("w6_copy")
        mid = self.s.add(rec("private record citing evidence", memory_id="MEM-w6recordaaaa001",
                             sensitivity="private", evidence_ids=[e]), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertFalse(os.path.exists(blob), "stand: the erase must remove the bytes at once")
        self.s.close()
        shutil.rmtree(self.root)
        shutil.copytree(snap, self.root)                        # the whole folder comes back from the copy
        self.s = Store(self.root, clock=self.clock)
        self.assertFalse(os.path.exists(blob), "the bytes of the evidence of an erased private record are back on disk")

    def test_w7_a_foreign_x_with_another_birth_still_gets_the_erased_text_refused(self):
        """W7 (reviewer M-21503): red on 4d77fa2: on a birth mismatch _reapply returns before the tombstone loop, the anchor
        moves past the line, and the erased TEXT of A is accepted again as a new record."""
        from herald.memory import PolicyRefused
        root_b = os.path.join(self.tmp.name, "w7_b")
        b = Store(root_b, clock=self.clock)
        b.add(rec("text of the store B", memory_id="MEM-w7recordxxxx001"), "b", **MAIN)
        b.close()
        mid = self.s.add(rec("text of the store A", memory_id="MEM-w7recordxxxx001"), "a", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(root_b, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNotNone(self.s.get(mid, **MAIN), "stand: the foreign X of B must stay alive")
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("text of the store A", memory_id="MEM-w7otherid00001"), "again", **MAIN)


class Round2Guards(Env):
    """Added by test author after the ff047ad mutant matrix (p61_matrix2.py): green on ff047ad, red on the mutant named in each."""

    def test_f6b_a_journal_that_appears_before_the_rebuild_writes_is_not_overwritten(self):
        """F6b (mutant: exclusive=False in _rebuild_journal): the journal is missing, main repairs; between the sync that
        saw it missing and the write somebody puts the old journal back. xb refuses (the journal stays whole); a plain
        write would replace it with the rebuilt, shorter one."""
        from unittest import mock
        mid = self.s.add(rec(TEXT, memory_id="MEM-f6brecordaaa001"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        with open(self.journal, "rb") as f:
            old_journal = f.read()
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        real = Store._journal_sync

        def sync_then_file_comes_back(store):
            state = real(store)
            if state == "missing" and not os.path.exists(store.journal):
                with open(store.journal, "wb") as f:
                    f.write(old_journal)
            return state

        with mock.patch.object(Store, "_journal_sync", sync_then_file_comes_back):
            try:
                self.s.repair_erasure_journal(OP, **MAIN)
            except FileExistsError:
                pass
        with open(self.journal, "rb") as f:
            self.assertEqual(f.read(), old_journal, "the journal that came back was overwritten by the rebuild")

    def test_f2_pending_merged_into_a_journal_ahead_of_the_base_continues_the_file_chain(self):
        """F2 (mutant: seq from the anchor in _journal_append): _merge_pending appends BEFORE the reconcile; when the
        base is behind the journal (restored from a copy) the anchor is behind the file, and a seq taken from the anchor
        would give the pending line the seq of a line already in the file."""
        x = self.s.add(rec(TEXT, memory_id="MEM-f2brecordxxx001"), "x", **MAIN)
        z = self.s.add(rec(TEXT + " zed", memory_id="MEM-f2brecordzzz001", predicate="owns"), "z", **MAIN)
        snap = self.copy_folder("f2b_copy")
        self.s.erase(x, "asked", OP, **MAIN)
        with open(self.journal, "rb") as f:
            journal_with_x = f.read()
        self.s.close()
        os.remove(self.journal)                                  # the witness goes missing
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(z, "asked", OP, **MAIN)                     # goes to the pending file
        self.s.close()
        self.assertTrue(os.path.exists(self.pending), "stand: the erase made without a journal waits in pending")
        with open(self.journal, "wb") as f:
            f.write(journal_with_x)                              # the journal with X comes back, one line ahead of the base
        self.put_db_back(snap)                                   # the base is behind it
        self.s = Store(self.root, clock=self.clock)
        seqs = [json.loads(ln)["seq"] for ln in self.journal_lines()]
        self.assertEqual(seqs, [1, 2], "the pending line did not continue the file's chain")
        self.assertIsNone(self.s.get(x, **MAIN))
        self.assertIsNone(self.s.get(z, **MAIN))


if __name__ == "__main__":
    unittest.main()
