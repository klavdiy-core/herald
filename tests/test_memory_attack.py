"""Every confirmed defect from an independent attack on the memory core, as a test that fails on the code it was found in."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import ReadAfterWriteFailed, Store, ValidationError  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
W = {"id": "ENT-w", "role": "worker"}
MAIN = {"reader_scope": "main"}


def position(event):
    return event["n"] if "n" in event else event["seq"]


class Attack(Base):
    def private(self, text="hidden words", **over):
        return self.s.add(rec(text, sensitivity="private", **over), "x", **MAIN)

    def db(self):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        self.addCleanup(con.close)
        return con

    def lex_rows(self, name):
        con = sqlite3.connect(os.path.join(self.root, name))
        try:
            return con.execute("SELECT count(*) FROM lex").fetchone()[0]
        finally:
            con.close()


class EventOracles(Attack):
    def gap(self, guess):
        a = self.s.add(rec(guess), "x")
        b = self.s.add(rec("marker " + guess), "x")
        return position(self.s.events(b)[0]) - position(self.s.events(a)[0])

    def test_event_numbers_do_not_confirm_an_erased_private_text(self):
        old = self.private("the erased sentence")
        self.s.erase(old, "asked", OP, **MAIN)
        self.assertEqual(self.gap("the erased sentence"), self.gap("a guess that never existed"))

    def test_event_numbers_do_not_count_private_writes(self):
        first = self.s.add(rec("one"), "x")
        self.private()
        self.private("more hidden words")
        second = self.s.add(rec("two"), "x")
        a, b = self.s.events(first)[0], self.s.events(second)[0]
        self.assertEqual(position(b) - position(a), 1)
        self.assertEqual(b["prev_hash"], a["hash"], "a worker's chain has no private link in it")

    def test_main_still_gets_one_total_order(self):
        first = self.s.add(rec("one"), "x")
        hidden = self.private()
        second = self.s.add(rec("two"), "x")
        order = [e["memory_id"] for e in self.s.all_events(reader_scope="main")]
        self.assertEqual(order, [first, hidden, second])
        self.assertNotIn("global_seq", self.s.events(first)[0], "the total order is the main scope's alone")
        with self.assertRaises(Exception):
            self.s.all_events()


class EvidenceScope(Attack):
    def main_evidence(self):
        return self.s.put_evidence(b"hidden bytes", "hidden/letter.txt", "tool_output", OP, **MAIN)

    def team_citing_main(self):
        e = self.main_evidence()
        return e, self.s.add(rec("visible note", evidence_ids=[e], status="pending"), "x", **MAIN)

    def test_a_worker_does_not_see_the_source_of_main_evidence(self):
        _, mid = self.team_citing_main()
        self.assertEqual(self.s.evidence_sources(mid), [])
        self.assertEqual(self.s.evidence_sources(mid, **MAIN), ["hidden/letter.txt"])

    def test_a_worker_does_not_see_the_id_of_main_evidence(self):
        e, mid = self.team_citing_main()
        self.assertEqual(self.s.get(mid)["evidence_ids"], [])
        self.assertEqual(self.s.get(mid, **MAIN)["evidence_ids"], [e])

    def test_a_worker_correction_keeps_the_hidden_citation(self):
        e, mid = self.team_citing_main()
        self.s.correct(mid, 1, {"confidence": 0.5}, "r", W)
        self.assertEqual(self.s.get(mid, **MAIN)["evidence_ids"], [e])

    def test_a_worker_replacing_citations_keeps_the_hidden_one(self):
        e, mid = self.team_citing_main()
        mine = self.s.put_evidence(b"worker bytes", "w.md", "tool_output", W)
        self.s.correct(mid, 1, {"evidence_ids": [mine]}, "r", W)
        self.assertEqual(sorted(self.s.get(mid, **MAIN)["evidence_ids"]), sorted([mine, e]))
        self.assertEqual(self.s.get(mid)["evidence_ids"], [mine])

    def test_erasing_a_private_record_leaves_a_workers_evidence(self):
        e = self.s.put_evidence(b"the worker's bytes", "w.md", "tool_output", W)
        mid = self.s.add(rec("private", evidence_ids=[e], sensitivity="private", status="pending"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.add(rec("still cites", evidence_ids=[e], authorship_channel="tool_output"), "x")
        self.assertEqual(self.s.verify(), [])

    def test_an_evidence_id_cannot_be_computed_from_guessed_bytes(self):
        other = Store(os.path.join(self.tmp.name, "other"), clock=self.clock)
        self.addCleanup(other.close)
        args = (b"guess", "a.md", "tool_output", W)
        self.assertNotEqual(self.s.put_evidence(*args), other.put_evidence(*args))

    def test_verify_names_no_blob_to_a_worker(self):
        import hashlib
        data = b"main bytes"
        self.s.put_evidence(data, "a.md", "tool_output", OP, **MAIN)
        sha = hashlib.sha256(data).hexdigest()
        os.remove(os.path.join(self.root, "archive", sha))
        as_worker = self.s.verify()
        self.assertTrue(as_worker, "the damage is reported")
        self.assertNotIn(sha, repr(as_worker))
        self.assertIn(sha, repr(self.s.verify(**MAIN)))


class Relabel(Attack):
    def declassified(self):
        mid = self.s.add(rec("raw diagnosis text", sensitivity="private"), "private detail", **MAIN)
        self.s.correct(mid, 1, {"object": {"value": "redacted"}, "sensitivity": "internal"}, "declassify", OP, **MAIN)
        return mid

    def test_old_private_versions_stay_hidden_after_a_relabel(self):
        mid = self.declassified()
        self.assertEqual([d["object"]["value"] for d in self.s.history(mid)], ["redacted"])
        v1 = self.s.history(mid, **MAIN)[0]["system_time"]["created_at"]
        self.assertIsNone(self.s.get(mid, as_of=v1))
        self.assertNotIn("private detail", repr(self.s.events(mid)))
        self.assertNotIn("declassify", repr(self.s.events(mid)), "the relabel event itself belongs to the main chain")
        self.assertEqual(self.s.get(mid, as_of=v1, **MAIN)["object"]["value"], "raw diagnosis text")

    def test_the_tombstone_of_an_old_private_version_says_nothing_to_a_worker(self):
        mid = self.declassified()
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.add(rec("raw diagnosis text"), "x")
        with self.assertRaises(Exception):
            self.s.add(rec("redacted"), "x")


class Search(Attack):
    def test_a_rebuild_does_not_reorder_tied_hits(self):
        a = self.s.add(rec("same words here"), "x")
        b = self.s.add(rec("same words here", subject_id="ENT-b"), "x")
        self.s.correct(a, 1, {"confidence": 0.5}, "r", OP)
        before = [h["memory_id"] for h in self.s.search("same words")["hits"]]
        self.s.rebuild()
        self.assertEqual([h["memory_id"] for h in self.s.search("same words")["hits"]], before)
        self.assertEqual(before, sorted([a, b]))

    def test_decomposed_text_can_be_written_and_found(self):
        nfd = unicodedata.normalize("NFD", "Андрей лечит кофейню")
        nfc = unicodedata.normalize("NFC", nfd)
        self.assertNotEqual(nfd, nfc)
        mid = self.s.add(rec(nfd), "x")
        self.assertEqual(self.s.search(nfc)["hits"][0]["memory_id"], mid)
        self.assertEqual(self.s.search(nfd)["hits"][0]["memory_id"], mid)

    def test_combining_marks_stay_inside_a_word(self):
        word = "नमस्ते दुनिया"
        mid = self.s.add(rec(word), "x")
        self.assertEqual(self.s.search(word)["hits"][0]["memory_id"], mid)


    def test_a_word_with_marks_is_one_word_not_scattered_pieces(self):
        self.s.add(rec("ते दुनिया नमस्"), "x")      # the two halves of the word, apart and in the other order
        self.assertEqual(self.s.search("नमस्ते")["outcome"], "not_found")
        whole = self.s.add(rec("नमस्ते दुनिया"), "x")
        self.assertEqual([h["memory_id"] for h in self.s.search("नमस्ते")["hits"]], [whole])


class Erasure(Attack):
    def test_no_field_can_carry_text_past_an_erasure(self):
        mid = self.s.add(rec("zqbody words here", aliases=["zqalias"]), "saw zqbody; aka zqalias", **MAIN)
        self.s.correct(mid, 1, {"object": {"value": "zqsecond"}}, "was zqbody words here", OP, **MAIN)
        self.s.correct(mid, 2, {"object": {"value": "zqthird"}}, "first was zqbody and second zqsecond", OP, **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        data = self.every_byte()
        for marker in (b"zqbody", b"zqalias", b"zqsecond", b"zqthird"):
            self.assertNotIn(marker, data)

    def test_the_chain_stands_after_an_erasure_and_commits_to_nothing_guessable(self):
        a = self.s.add(rec("same text"), "x")
        b = self.s.add(rec("same text two"), "x")
        self.s.erase(a, "asked", OP, **MAIN)
        self.assertEqual(self.s.verify(), [])
        self.assertTrue(all(e["reason"] is None for e in self.s.events(a, **MAIN)), "the reason goes with the record")
        rows = self.db().execute("SELECT count(*) FROM event_keys WHERE memory_id=?", (a,)).fetchone()[0]
        self.assertEqual(rows, 0)
        c = self.db().execute("SELECT commitment FROM events WHERE memory_id=?", (b,)).fetchone()[0]
        self.assertEqual(len(c), 64)

    def test_two_identical_events_have_different_commitments(self):
        one = Store(os.path.join(self.tmp.name, "one"), clock=lambda: "2026-01-01T00:00:00Z")
        two = Store(os.path.join(self.tmp.name, "two"), clock=lambda: "2026-01-01T00:00:00Z")
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        a = one.add(rec("same", memory_id="MEM-a"), "x", **MAIN)
        b = two.add(rec("same", memory_id="MEM-a"), "x", **MAIN)
        self.assertNotEqual(one.events(a, **MAIN)[0]["hash"], two.events(b, **MAIN)[0]["hash"])

    def test_an_uncited_row_of_the_same_bytes_goes_too(self):
        a = self.s.put_evidence(b"zqbytes twin", "zqsrc-a", "tool_output", W, span=[1])
        self.s.put_evidence(b"zqbytes twin", "zqsrc-b", "tool_output", W, span=[2])
        mid = self.s.add(rec("x", evidence_ids=[a], status="pending"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        data = self.every_byte()
        self.assertNotIn(b"zqbytes", data)
        self.assertNotIn(b"zqsrc-b", data)

    def test_a_leftover_temp_file_does_not_survive_the_next_open(self):
        path = os.path.join(self.root, "archive", ".tmpabc")
        with open(path, "wb") as f:
            f.write(b"zqtemp")
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertNotIn(b"zqtemp", self.every_byte())

    def test_an_orphan_blob_leaves_the_archive_on_the_next_open(self):
        path = os.path.join(self.root, "archive", "ab" * 32)
        with open(path, "wb") as f:
            f.write(b"zqorphan")
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertFalse(os.path.exists(path))      # it waits in quarantine for a limited time: test_memory_quarantine

    def test_a_bad_source_leaves_no_blob(self):
        with self.assertRaises(ValidationError):
            self.s.put_evidence(b"zqorphan2", "bad \ud800 source", "tool_output", W)
        self.assertNotIn(b"zqorphan2", self.every_byte())

    def test_a_leftover_index_journal_does_not_survive_an_erasure(self):
        mid = self.s.add(rec("x"), "x")
        for name in ("lexical_main.sqlite-journal", "lexical_worker.sqlite-journal"):
            with open(os.path.join(self.root, name), "wb") as f:
                f.write(b"zqjournal")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertNotIn(b"zqjournal", self.every_byte())

    def test_a_rebuild_removes_a_stale_journal_beside_the_index(self):
        path = os.path.join(self.root, "lexical_main.sqlite-journal")
        with open(path, "wb") as f:
            f.write(b"zqstale")
        self.s.rebuild()
        self.assertFalse(os.path.exists(path))

    def test_a_held_index_delays_an_erasure_and_it_then_finishes(self):
        mid = self.s.add(rec("zqheld words"), "x")
        con = sqlite3.connect(os.path.join(self.root, "lexical_main.sqlite"))
        cur = con.execute("SELECT * FROM lex")
        cur.fetchone()
        try:
            self.s.erase(mid, "asked", OP, **MAIN)
            other = Store(self.root, clock=self.clock)
            other.close()
        finally:
            cur.close()
            con.close()
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertEqual({r["state"] for r in self.s.erasure_ledger(mid, **MAIN)}, {"done"})
        self.assertNotIn(b"zqheld", self.every_byte())

    def test_a_put_during_the_erasure_keeps_its_blob(self):
        data = b"zqshared bytes"
        e = self.s.put_evidence(data, "a.md", "tool_output", W)
        mid = self.s.add(rec("x", evidence_ids=[e], status="pending"), "x")
        real = self.s._finish_erasures
        other = Store(self.root, clock=self.clock)
        self.addCleanup(other.close)

        def hook():
            other.put_evidence(data, "b.md", "tool_output", W)
            real()

        self.s._finish_erasures = hook
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s._finish_erasures = real
        self.assertEqual(self.s.verify(), [])


class Crashes(Attack):
    def run_child(self, body):
        code = textwrap.dedent("""
            import os, sys
            sys.path.insert(0, %r)
            sys.path.insert(0, %r)
            from herald.memory import Store
            from test_memory_core import rec, OP
            s = Store(%r)
        """) % (ROOT, os.path.dirname(__file__), self.root) + textwrap.dedent(body)
        p = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 7, p.stderr[-600:])

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, clock=self.clock)

    def test_death_before_commit_leaves_index_and_records_agreeing(self):
        mid = self.s.add(rec("zqold words"), "x")
        self.s.close()
        self.run_child("""
            real = s._index_and_confirm
            def die(doc, restore):
                real(doc, restore)
                os._exit(7)
            s._index_and_confirm = die
            s.correct(%r, 1, {"object": {"value": "zqnew words"}}, "r", OP)
        """ % mid)
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(self.s.search("zqold")["outcome"], "found")
        self.assertEqual(self.s.search("zqnew")["outcome"], "not_found")
        self.assertNotIn(b"zqnew", self.every_byte())

    def test_death_in_an_add_leaves_no_phantom_text(self):
        self.s.close()
        self.run_child("""
            real = s._index_and_confirm
            def die(doc, restore):
                real(doc, restore)
                os._exit(7)
            s._index_and_confirm = die
            s.add(rec("zqphantom words"), "x")
        """)
        self.s = Store(self.root, clock=self.clock)
        self.assertNotIn(b"zqphantom", self.every_byte())

    def test_a_failed_second_index_moves_neither(self):
        mid = self.s.add(rec("oldword text"), "x")
        real = self.s._lex

        def broken(name):
            if name == "lexical_main.sqlite":
                raise sqlite3.OperationalError("boom")
            return real(name)

        self.s._lex = broken
        with self.assertRaises(ReadAfterWriteFailed):
            self.s.correct(mid, 1, {"object": {"value": "newword text"}}, "r", OP)
        self.s._lex = real
        self.assertEqual(self.s.search("oldword")["outcome"], "found")
        self.assertEqual(self.s.search("newword")["outcome"], "not_found")

    def test_a_rebuild_lost_after_leaving_the_team_scope_happens_on_open(self):
        mid = self.s.add(rec("zqleaving words"), "x")
        real = self.s.rebuild

        def die():
            raise RuntimeError("lost power")

        self.s.rebuild = die
        try:
            self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, **MAIN)
        except RuntimeError:
            pass
        self.s.rebuild = real
        self.reopen()
        with open(os.path.join(self.root, "lexical_worker.sqlite"), "rb") as f:
            self.assertNotIn(b"zqleaving", f.read())

    def test_a_crashed_salt_write_does_not_brick_a_new_store(self):
        root = os.path.join(self.tmp.name, "fresh")
        os.makedirs(root)
        open(os.path.join(root, "tombstone.salt"), "w", encoding="ascii").close()      # created, nothing written yet
        Store(root, clock=self.clock).close()


class Races(Attack):
    def race(self, op):
        """Another process acts while this one is between reading the records and swapping the index."""
        real = self.s._write_index
        done = []

        def worker():
            other = Store(self.root, clock=self.clock)
            try:
                op(other)
            finally:
                other.close()

        def hook(name, docs):
            if not done:
                done.append(threading.Thread(target=worker))
                done[0].start()
                time.sleep(0.6)
            real(name, docs)

        self.s._write_index = hook
        self.s.rebuild()
        self.s._write_index = real
        done[0].join(40)

    def test_a_rebuild_cannot_write_an_erased_text_back(self):
        mid = self.s.add(rec("zqrace words"), "x")
        self.race(lambda other: other.erase(mid, "asked", OP, **MAIN))
        self.assertEqual({r["state"] for r in self.s.erasure_ledger(mid, **MAIN)}, {"done"})
        self.assertNotIn(b"zqrace", self.every_byte())

    def test_a_rebuild_cannot_lose_a_committed_record(self):
        self.s.add(rec("first words"), "x")
        self.race(lambda other: other.add(rec("zqarrived words"), "x"))
        self.assertEqual(self.s.search("zqarrived")["outcome"], "found")


class Integrity(Attack):
    def forge(self, *sql):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        for statement in sql:
            con.execute(statement)
        con.commit()
        con.close()

    def test_a_cut_tail_of_events_is_reported(self):
        self.s.add(rec("one"), "x")
        self.s.add(rec("two"), "x")
        self.assertEqual(self.s.verify(), [])
        last = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        top = last.execute("SELECT max(rowid) FROM events").fetchone()[0]
        last.close()
        self.forge("DROP TRIGGER events_no_delete", "DELETE FROM events WHERE rowid=%d" % top)
        self.assertTrue(any("events" in p for p in self.s.verify(**MAIN)))

    def test_an_edited_current_version_is_reported(self):
        mid = self.s.add(rec("honest text"), "x")
        self.forge("UPDATE versions SET doc=replace(doc,'honest','forged') WHERE memory_id='%s'" % mid)
        self.assertTrue(self.s.verify(**MAIN))

    def test_replace_cannot_rewrite_an_event(self):
        self.s.add(rec("one"), "x")
        con = self.db()
        columns = [c[1] for c in con.execute("PRAGMA table_info(events)")]
        row = con.execute("SELECT * FROM events").fetchone()
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("INSERT OR REPLACE INTO events (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(row))), row)


class Disk(Attack):
    def test_a_new_blob_reaches_the_disk(self):
        calls = []
        real = os.fsync

        def count(fd):
            calls.append(fd)
            return real(fd)

        os.fsync = count
        try:
            self.s.put_evidence(b"durable", "a.md", "tool_output", W)
        finally:
            os.fsync = real
        self.assertTrue(calls)

    def test_a_retroactive_correction_leaves_valid_documents(self):
        mid = self.s.add(rec("x", valid_time={"from": "2026-06-01", "to": None}), "x")
        self.s.correct(mid, 1, {"valid_time": {"from": "2026-01-01", "to": None}}, "r", OP)
        for doc in self.s.history(mid):
            valid = doc["valid_time"]
            if valid.get("to"):
                self.assertLessEqual(valid["from"][:10], valid["to"][:10])


if __name__ == "__main__":
    unittest.main()
