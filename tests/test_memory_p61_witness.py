"""P61 red tests, batch 2: R2, R3, R3b, R5, R6, R7a-c, R8 (signatures agreed with reviewer M-21230).
Written red-first against 42da2cd. test author, 11.11.2026.
Journal path: <root>.erasures.jsonl; birth marker: <root>/erasures.born; ErasureJournalCorrupt from herald.memory.
Test helpers write only minimal journal lines ({"seq": n}); the real line format belongs to the implementation,
these tests check behaviour (refusal, named error, rewritten journal), not the line schema."""
import json
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import herald.memory as hm  # noqa: E402
from herald.memory import Store, PolicyRefused  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}
SECRET = "zebra pancake quartz"


def chain(seqs, wrong_prev_at=None):
    """Journal lines as bytes, format of author M-21354: one JSON object per line, hash = sha256 hex of the line
    bytes without the newline, prev_hash of line n = hash of line n-1, first prev_hash = empty string.
    seqs gives each line's seq (wrong ones on purpose); wrong_prev_at = 1-based line whose prev_hash is broken."""
    import hashlib
    out, prev = [], ""
    for i, n in enumerate(seqs, 1):
        use = "0" * 64 if wrong_prev_at == i else prev
        line = json.dumps({"seq": n, "prev_hash": use}).encode("utf-8")
        out.append(line)
        prev = hashlib.sha256(line).hexdigest()
    return out


def _blob(x):
    return x if isinstance(x, str) else repr(x)


class Helpers(Base):
    @property
    def journal(self):
        return self.root + ".erasures.jsonl"

    @property
    def born(self):
        return os.path.join(self.root, "erasures.born")

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, clock=self.clock)

    def worker_calls(self, mid):
        s = self.s
        return {
            "get": lambda: s.get(mid, reader_scope="worker"),
            "history": lambda: s.history(mid, reader_scope="worker"),
            "search": lambda: s.search("anything", reader_scope="worker"),
            "events": lambda: s.events(mid, reader_scope="worker"),
            "evidence_sources": lambda: s.evidence_sources(mid, reader_scope="worker"),
            "verify": lambda: s.verify(reader_scope="worker"),
            "erasure_ledger": lambda: s.erasure_ledger(mid, reader_scope="worker"),
        }

    def born_with_journal(self):
        """Stand for R2/R3/R5: a store that has erased one record and 'has a journal'; test makes the files itself."""
        mid = self.s.add(rec(SECRET), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        os.makedirs(self.root, exist_ok=True)
        with open(self.born, "w", encoding="ascii") as f:
            f.write("born\n")
        if not os.path.exists(self.journal):  # on 42da2cd nothing writes one; on the real code erase has already made it
            with open(self.journal, "w", encoding="utf-8") as f:
                f.write(json.dumps({"seq": 1}) + "\n")
        self.s = Store(self.root, clock=self.clock)
        return mid



class Witness(Helpers):
    def test_r2_marker_without_journal_refuses_every_worker_call(self):
        """R2: red on 42da2cd: nothing checks the journal, worker calls answer normally."""
        mid = self.born_with_journal()
        self.s.close()
        os.remove(self.journal)
        self.s = Store(self.root, clock=self.clock)
        for name, call in self.worker_calls(mid).items():
            with self.subTest(call=name):
                with self.assertRaises(PolicyRefused):
                    call()
        self.s.get(mid, **MAIN)  # main keeps working: no exception
        first = self.s.verify(**MAIN)
        self.assertTrue(first and "erasure witness missing" in first[0], first)

    def test_r3_garbage_in_the_middle_is_a_named_error_with_line_and_offset(self):
        """R3: red on 42da2cd: Store opens, no error exists.
        Path to the garbage: the test REWRITES the whole journal, so its last line no longer matches the anchor in meta
        (journal_head) and the ordinary open goes through the full pass (reviewer M-21331). A matching anchor would read only
        the last line - that limit is named, and verify_journal() is the other door (not tested here until it exists)."""
        self.born_with_journal()
        self.s.close()
        first = chain([1])[0]
        with open(self.journal, "wb") as f:
            f.write(first + chr(10).encode() + b"this is not json" + chr(10).encode() + first + chr(10).encode())
        err = getattr(hm, "ErasureJournalCorrupt", RuntimeError)
        with self.assertRaises(err) as cm:
            Store(self.root, clock=self.clock)
        self.assertEqual(type(cm.exception).__name__, "ErasureJournalCorrupt")
        text = str(cm.exception)
        self.assertIn("line 2", text, "the message must carry the line number (from 1)")
        self.assertIn("offset " + str(len(first) + 1), text, "the message must carry the byte offset of the bad line")

    def test_r3_torn_tail_without_newline_is_not_an_error(self):
        """R3 (tail half): a torn last line is an unfinished write, not a corrupt journal. Must be green before AND after."""
        self.born_with_journal()
        self.s.close()
        with open(self.journal, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "prev_ha')
        self.s = Store(self.root, clock=self.clock)
        self.assertIsNotNone(self.s)

    def test_r3b_gap_or_repeat_of_seq_in_the_middle_is_corrupt(self):
        """R3b: red on 42da2cd: no check of seq at all."""
        self.born_with_journal()
        self.s.close()
        err = getattr(hm, "ErasureJournalCorrupt", RuntimeError)
        for lines in ([1, 3, 4], [1, 2, 2, 3], [2, 1, 3]):
            with self.subTest(seqs=lines):
                # the chain is CORRECT (each prev_hash = sha256 of the line before); only seq is wrong
                with open(self.journal, "wb") as f:
                    f.write(chr(10).encode().join(chain(lines)) + chr(10).encode())
                with self.assertRaises(err) as cm:
                    opened = Store(self.root, clock=self.clock)
                    opened.close()
                self.assertEqual(type(cm.exception).__name__, "ErasureJournalCorrupt")
        self.s = Store.__new__(Store)
        self.s.close = lambda: None

    def test_r3c_broken_chain_with_right_seq_is_corrupt(self):
        """R3c: red on 42da2cd: no chain check. seq is 1,2,3 and right; only prev_hash of line 3 is wrong."""
        self.born_with_journal()
        self.s.close()
        err = getattr(hm, "ErasureJournalCorrupt", RuntimeError)
        lines = chain([1, 2, 3], wrong_prev_at=3)
        with open(self.journal, "wb") as f:
            f.write(chr(10).encode().join(lines) + chr(10).encode())
        with self.assertRaises(err) as cm:
            opened = Store(self.root, clock=self.clock)
            opened.close()
        self.assertEqual(type(cm.exception).__name__, "ErasureJournalCorrupt")
        self.assertIn("line 3", str(cm.exception))
        self.s = Store.__new__(Store)
        self.s.close = lambda: None

    def test_r5_rolled_back_journal_is_rewritten_from_the_ledger_and_named(self):
        """R5: red on 42da2cd: the journal stays empty and verify says nothing."""
        mid = self.s.add(rec(SECRET), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        open(self.journal, "w").close()  # the journal rolled back to before the erase
        with open(self.born, "w", encoding="ascii") as f:
            f.write("born\n")
        self.s = Store(self.root, clock=self.clock)
        with open(self.journal, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertTrue(lines, "journal must be refilled from erasure_ledger")
        self.assertIn("seq", json.loads(lines[0]))
        self.assertIn("journal behind db", _blob(self.s.verify(**MAIN)))
        self.assertIsNone(self.s.get(mid, **MAIN), "the erase must not come back")

    def test_r6_failure_of_reapply_means_no_store(self):
        """R6: red on 42da2cd: a restored db opens without any erase attempt, so the injected error is never hit."""
        mid = self.s.add(rec(SECRET), "x")
        self.s.close()
        snap = os.path.join(self.tmp.name, "r6_copy")
        shutil.copytree(self.root, snap)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))
        with mock.patch.object(Store, "_erase_rows", side_effect=OSError("injected")) as erase_rows:
            with self.assertRaises(Exception):
                Store(self.root, clock=self.clock)
        self.assertTrue(erase_rows.called, "the reapply must go through _erase_rows")

    def test_r8_reapply_is_idempotent(self):
        """R8: red on 42da2cd at the stand line: after the first open there is no erase_reapplied event to count."""
        mid = self.s.add(rec(SECRET), "x")
        self.s.close()
        snap = os.path.join(self.tmp.name, "r8_copy")
        shutil.copytree(self.root, snap)
        self.s = Store(self.root, clock=self.clock)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)

        def count():
            return _blob(self.s.all_events(reader_scope="main")).count("erase_reapplied")

        first = count()
        self.assertGreaterEqual(first, 1, "stand: the first open must reapply the erase and say so")
        self.reopen()
        self.assertEqual(count(), first, "a second open must not add events")


class Legacy(Helpers):
    def legacy_store(self):
        mid = self.s.add(rec(SECRET), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        for p in (self.journal, self.born):
            if os.path.exists(p):
                os.remove(p)
        self.s = Store(self.root, clock=self.clock)
        return mid

    def test_r7a_without_migrate_workers_are_refused(self):
        """R7a: red on 42da2cd: no marker is looked for, worker calls answer normally."""
        mid = self.legacy_store()
        for name, call in self.worker_calls(mid).items():
            with self.subTest(call=name):
                with self.assertRaises(PolicyRefused):
                    call()
        first = self.s.verify(**MAIN)
        self.assertTrue(first and "legacy store: migrate required" in first[0], first)

    def test_r7b_after_migrate_workers_work_and_nothing_is_reapplied(self):
        """R7b: red on 42da2cd: Store has no migrate_legacy (AttributeError) - the signature is agreed in M-21230."""
        mid = self.legacy_store()
        self.s.migrate_legacy(actor=OP, reader_scope="main")
        self.assertTrue(os.path.exists(self.journal) and os.path.exists(self.born))
        self.s.get(mid, reader_scope="worker")
        self.assertEqual(self.s.reapplied, 0)

    def test_r7d_fresh_store_in_an_empty_folder_is_open_to_workers_and_has_witness_files(self):
        """R7d: red on 42da2cd: a fresh store creates neither the journal nor erasures.born."""
        root = os.path.join(self.tmp.name, "fresh")
        fresh = Store(root, clock=self.clock)
        try:
            self.assertIsNone(fresh.get("MEM-none", reader_scope="worker"))  # no PolicyRefused
            self.assertTrue(os.path.exists(root + ".erasures.jsonl"), "fresh store must create the journal at once")
            self.assertTrue(os.path.exists(os.path.join(root, "erasures.born")), "fresh store must create erasures.born at once")
        finally:
            fresh.close()

    def test_r7c_migrate_by_a_worker_is_refused(self):
        """R7c: red on 42da2cd: no migrate_legacy (AttributeError, not PolicyRefused)."""
        self.legacy_store()
        with self.assertRaises(PolicyRefused):
            self.s.migrate_legacy(actor=OP, reader_scope="worker")


if __name__ == "__main__":
    unittest.main()
