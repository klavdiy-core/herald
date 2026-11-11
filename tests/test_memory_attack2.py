"""Confirmed defects from a second independent attack, on the rewritten memory core."""
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import PolicyRefused, Store, ValidationError  # noqa: E402
from herald.memory import store as store_module  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
W = {"id": "ENT-w", "role": "worker"}
MAIN = {"reader_scope": "main"}
TOOL = {"authorship_channel": "tool_output"}


class Attack(Base):
    def private(self, text="hidden words", **over):
        return self.s.add(rec(text, sensitivity="private", **over), "x", **MAIN)

    def main_ev(self, data=b"hidden bytes"):
        return self.s.put_evidence(data, "hidden.txt", "tool_output", OP, **MAIN)

    def worker_ev(self, data=b"worker bytes"):
        return self.s.put_evidence(data, "w.md", "tool_output", W)

    def forge(self, *sql):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        for statement in sql:
            con.execute(statement)
        con.commit()
        con.close()

    def hold_index(self, name="lexical_main.sqlite"):
        con = sqlite3.connect(os.path.join(self.root, name), check_same_thread=False)
        cur = con.execute("SELECT * FROM lex")
        cur.fetchone()

        def release():
            with contextlib.suppress(sqlite3.Error):    # the test may already have closed them
                cur.close()
                con.close()

        self.addCleanup(release)
        return con, cur


class HiddenEvidence(Attack):
    def test_new_sha_reveals_nothing_about_hidden_evidence(self):
        e = self.main_ev()
        mid = self.s.add(rec("note", evidence_ids=[e], status="pending"), "x", **MAIN)
        seen = self.s.get(mid)
        event = self.s.events(mid)[0]
        self.assertEqual(event["new_sha"], hashlib.sha256(store_module._dumps(seen).encode("utf-8")).hexdigest())
        self.assertEqual(seen["evidence_ids"], [])

    def test_a_worker_correction_event_hides_the_hidden_citation(self):
        e = self.main_ev()
        mid = self.s.add(rec("note", evidence_ids=[e], status="pending"), "x", **MAIN)
        before = self.s.get(mid)
        self.s.correct(mid, 1, {"confidence": 0.5}, "r", W)
        event = self.s.events(mid)[-1]
        doc = self.s.get(mid)
        self.assertEqual(event["new_sha"], hashlib.sha256(store_module._dumps(doc).encode("utf-8")).hexdigest())
        self.assertEqual(event["old_sha"], hashlib.sha256(store_module._dumps(before).encode("utf-8")).hexdigest())

    def test_a_worker_cannot_promote_on_evidence_it_cannot_see(self):
        e = self.main_ev()
        hidden = self.s.add(rec("note", evidence_ids=[e], status="pending", **TOOL), "x", **MAIN)
        plain = self.s.add(rec("plain note", status="pending", **TOOL), "x")
        for mid in (hidden, plain):
            with self.assertRaises(PolicyRefused):
                self.s.correct(mid, 1, {"status": "canonical"}, "r", W)


class EvidenceOwnership(Attack):
    def both(self, data):
        e = self.worker_ev(data)
        label = data.decode()
        team = self.s.add(rec("team " + label, evidence_ids=[e], status="pending", **TOOL), "x")
        private = self.s.add(rec("private " + label, evidence_ids=[e], status="pending", sensitivity="private", **TOOL), "x",
                             **MAIN)
        return e, team, private

    def test_erase_order_does_not_decide_whether_the_bytes_go(self):
        for first, second in ((0, 1), (1, 0)):
            data = b"zqboth order %d" % first
            _, team, private = self.both(data)
            ids = (team, private)
            self.s.erase(ids[first], "asked", OP, **MAIN)
            self.s.erase(ids[second], "asked", OP, **MAIN)
            self.assertNotIn(data, self.every_byte())

    def test_a_worker_cannot_tell_a_private_record_cites_its_evidence(self):
        a = self.worker_ev(b"alone bytes")
        lone = self.s.add(rec("team alone", evidence_ids=[a], status="pending", **TOOL), "x")
        e, team, _ = self.both(b"shared bytes")
        self.s.erase(lone, "asked", OP, **MAIN)
        self.s.erase(team, "asked", OP, **MAIN)
        for evidence_id in (a, e):
            with self.assertRaises(ValidationError):
                self.s.add(rec("cite again", evidence_ids=[evidence_id], status="pending", **TOOL), "x")


    def test_a_record_relabelled_private_gets_its_own_evidence_row(self):
        e = self.worker_ev(b"relabel bytes")
        mid = self.s.add(rec("relabel words", evidence_ids=[e], status="pending", **TOOL), "x")
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, **MAIN)
        doc = self.s.get(mid, **MAIN)
        self.assertNotIn(e, doc["evidence_ids"])
        self.assertEqual(self.s.evidence_sources(mid, **MAIN), ["w.md"])

    def test_a_relabelled_record_no_longer_keeps_a_workers_evidence_alive(self):
        e = self.worker_ev(b"shared by two")
        stays = self.s.add(rec("stays team", evidence_ids=[e], status="pending", **TOOL), "x")
        leaves = self.s.add(rec("goes private", evidence_ids=[e], status="pending", **TOOL), "x")
        self.s.correct(leaves, 1, {"sensitivity": "private"}, "r", OP, **MAIN)
        self.s.erase(stays, "asked", OP, **MAIN)
        with self.assertRaises(ValidationError):
            self.s.add(rec("cite again", evidence_ids=[e], status="pending", **TOOL), "x")


class NoTraceBesideWorkerEvidence(Attack):
    def test_an_erased_private_record_leaves_no_trace_next_to_a_workers_evidence(self):
        e = self.worker_ev(b"zqworker stays")
        mid = self.s.add(rec("zqprivate goes", evidence_ids=[e], status="pending", sensitivity="private", **TOOL), "x",
                         **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        data = self.every_byte()
        self.assertIn(b"zqworker stays", data, "the worker's own bytes are a documented limit")
        self.assertNotIn(b"zqprivate", data)
        self.assertNotIn(mid.encode(), self._evidence_tables())
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM evidence WHERE scope='main'").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT count(*) FROM evidence_refs").fetchone()[0], 0)

    def _evidence_tables(self):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        self.addCleanup(con.close)
        rows = list(con.execute("SELECT * FROM evidence")) + list(con.execute("SELECT * FROM evidence_refs"))
        return repr(rows).encode()


class ScopeLeaks(Attack):
    def test_a_record_gone_private_then_erased_does_not_block_a_worker(self):
        mid = self.s.add(rec("was team words"), "x")
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.add(rec("was team words"), "x")

    def test_verify_does_not_tell_a_worker_of_a_private_erasure(self):
        mid = self.private("zqpending")
        _, cur = self.hold_index()
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertEqual(self.s.verify(), [])
        self.assertTrue(any("pending" in p for p in self.s.verify(**MAIN)))
        cur.close()


class Input(Attack):
    def test_deep_nesting_is_refused_and_harms_nothing(self):
        deep = "x"
        for _ in range(989):
            deep = [deep]
        with self.assertRaises(ValidationError):
            self.s.add(rec("x", object={"value": deep}), "x")
        mid = self.private("still works")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(self.s.verify(**MAIN), [])

    def test_mistyped_identity_is_refused_not_coerced(self):
        for bad in (rec(actor={"id": 123, "role": "x"}), rec(actor={"id": "a", "role": 5})):
            with self.assertRaises(ValidationError):
                self.s.add(bad, "x")
        with self.assertRaises(ValidationError):
            self.s.add(rec(memory_id=123), "x", **MAIN)
        mid = self.s.add(rec("x"), "x")
        for version in (1.0, True, "1"):
            with self.assertRaises((ValidationError, TypeError)):
                self.s.correct(mid, version, {"confidence": 0.5}, "r", OP)
        self.assertEqual(self.s.verify(**MAIN), [])

    def test_every_field_is_checked_for_its_type(self):
        for over in ({"aliases": "x"}, {"aliases": 3}, {"aliases": [1]}, {"conflicts": "x"}, {"confidence": "high"},
                     {"confidence": float("nan")}, {"confidence": 99}, {"subject_id": None}, {"authority": 3},
                     {"retention_policy": 1}, {"resurfacing_policy": None}, {"index_state": 5},
                     {"valid_time": {"from": 20260101, "to": None}}, {"valid_time": {"from": "2026-01-01", "x": 1}},
                     {"object": None}):
            with self.subTest(over):
                with self.assertRaises(ValidationError):
                    self.s.add(rec("x", **over), "x")

    def test_text_that_cannot_be_encoded_is_refused(self):
        with self.assertRaises(ValidationError):
            self.s.add(rec("bad \ud800 text"), "x")
        with self.assertRaises(ValidationError):
            self.s.add(rec("x"), "bad \ud800 reason")

    def test_a_null_source_leaves_no_blob(self):
        with self.assertRaises(ValidationError):
            self.s.put_evidence(b"zqnull", None, "tool_output", W)
        self.assertNotIn(b"zqnull", self.every_byte())

    def test_a_predicate_needs_a_letter_or_a_digit(self):
        with self.assertRaises(ValidationError):
            self.s.add(rec("x", predicate="́"), "x")

    def test_private_use_characters_can_be_written(self):
        mid = self.s.add(rec("zqgluedicon"), "x")
        self.assertEqual(self.s.search("zqgluedicon")["hits"][0]["memory_id"], mid)


class Verify(Attack):
    def test_a_removed_commitment_on_a_live_record_is_reported(self):
        mid = self.s.add(rec("honest"), "x")
        self.forge("DELETE FROM event_keys WHERE memory_id='%s'" % mid)
        self.assertTrue(self.s.verify(**MAIN))

    def test_an_edited_old_version_is_reported(self):
        mid = self.s.add(rec("honest words"), "x")
        self.s.correct(mid, 1, {"object": {"value": "second"}}, "r", OP)
        self.assertEqual(self.s.verify(**MAIN), [])
        self.forge("UPDATE versions SET doc=replace(doc,'honest','forged') WHERE version=1")
        self.assertTrue(self.s.verify(**MAIN))

    def test_an_edited_closing_is_reported(self):
        mid = self.s.add(rec("honest words"), "x")
        self.s.correct(mid, 1, {"object": {"value": "second"}}, "r", OP)
        self.forge("UPDATE versions SET closed_at='1999-01-01T00:00:00Z' WHERE version=1")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_closed_version_can_be_read_with_its_closing(self):
        mid = self.s.add(rec("first words"), "x")
        self.s.correct(mid, 1, {"object": {"value": "second"}}, "r", OP)
        old = self.s.history(mid)[0]
        self.assertIsNotNone(old["system_time"]["superseded_at"])
        self.assertIsNotNone(old["valid_time"]["to"])

    def test_a_live_record_hidden_removed_or_rescoped_is_reported(self):
        for sql in ("UPDATE records SET tombstoned=1", "DELETE FROM records", "UPDATE records SET scope='main'"):
            with self.subTest(sql):
                self.s.add(rec("one " + sql), "x")
                self.forge(sql)
                self.assertTrue(self.s.verify(**MAIN), sql)
                self.tearDown_store()

    def tearDown_store(self):
        self.s.close()
        import tempfile
        self.root = tempfile.mkdtemp(dir=self.tmp.name)
        self.s = Store(self.root, clock=self.clock)

    def test_an_event_with_an_unknown_scope_cannot_enter(self):
        self.s.add(rec("x"), "x")
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        self.addCleanup(con.close)
        columns = [c[1] for c in con.execute("PRAGMA table_info(events)") if c[1] != "seq"]
        row = list(con.execute("SELECT %s FROM events" % ",".join(columns)).fetchone())
        row[columns.index("scope")] = "zz"
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("INSERT INTO events (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(row))), row)

    def test_corrupt_documents_are_reported_not_raised(self):
        for sql in ("UPDATE versions SET doc='{bad'", "UPDATE event_keys SET payload='{bad'"):
            with self.subTest(sql):
                self.s.add(rec("doc " + sql), "x")
                self.forge(sql)
                self.assertTrue(self.s.verify(**MAIN))
                self.tearDown_store()

    tearDown_store = tearDown_store


class Semantics(Attack):
    def test_a_correction_leaves_validity_alone_unless_the_content_changed(self):
        mid = self.s.add(rec("x", valid_time={"from": "2020-01-01", "to": "2021-01-01"}), "x")
        self.s.correct(mid, 1, {"confidence": 0.5}, "r", OP)
        self.assertEqual(self.s.get(mid)["valid_time"], {"from": "2020-01-01", "to": "2021-01-01"})

    def test_the_tombstone_does_not_care_about_unicode_form(self):
        text = "Андрей лечит"
        mid = self.s.add(rec(text), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        with self.assertRaises(PolicyRefused):
            self.s.add(rec(unicodedata.normalize("NFD", text)), "x")

    def test_a_tombstone_does_not_block_unrelated_fixes_to_a_twin(self):
        a = self.s.add(rec("same text"), "x")
        b = self.s.add(rec("same text", subject_id="ENT-b"), "x")
        self.s.erase(a, "asked", OP, **MAIN)
        self.s.correct(b, 1, {"confidence": 0.5}, "r", OP)

    def test_a_correction_still_cannot_bring_erased_text_back(self):
        a = self.s.add(rec("gone text"), "x")
        self.s.erase(a, "asked", OP, **MAIN)
        b = self.s.add(rec("other"), "x")
        with self.assertRaises(PolicyRefused):
            self.s.correct(b, 1, {"object": {"value": "gone text"}}, "r", OP)


class Durability(Attack):
    def test_a_correction_that_committed_does_not_raise_for_a_held_index(self):
        mid = self.s.add(rec("zqheld2"), "x")
        con, cur = self.hold_index("lexical_worker.sqlite")
        self.assertEqual(self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, **MAIN), 2)
        cur.close()
        con.close()
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        with open(os.path.join(self.root, "lexical_worker.sqlite"), "rb") as f:
            self.assertNotIn(b"zqheld2", f.read())

    def test_a_brief_reader_does_not_fail_a_rebuild(self):
        con, cur = self.hold_index()

        def release():
            time.sleep(0.2)
            cur.close()
            con.close()

        thread = threading.Thread(target=release)
        thread.start()
        self.s.add(rec("x"), "x")
        self.s.rebuild()
        thread.join()

    def test_a_missing_salt_stops_a_store_that_exists(self):
        self.s.close()
        os.remove(os.path.join(self.root, "tombstone.salt"))
        with self.assertRaises(RuntimeError):
            Store(self.root, clock=self.clock)
        with open(os.path.join(self.root, "tombstone.salt"), "w", encoding="ascii") as f:
            f.write("a" * 64)
        self.s = Store(self.root, clock=self.clock)

    def test_a_temp_file_is_swept_by_an_erasure_too(self):
        mid = self.s.add(rec("x"), "x")
        with open(os.path.join(self.root, "archive", ".tmpzz"), "wb") as f:
            f.write(b"zqtemp2")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertNotIn(b"zqtemp2", self.every_byte())

    def test_a_generation_is_never_reused_after_a_crash(self):
        mid = self.s.add(rec("zqold words"), "x")
        code = textwrap.dedent("""
            import os, sys
            sys.path.insert(0, %r)
            sys.path.insert(0, %r)
            from herald.memory import Store
            from test_memory_core import OP
            s = Store(%r)
            real = s._index_and_confirm
            def die(doc, generation):
                real(doc, generation)
                os._exit(7)
            s._index_and_confirm = die
            s.correct(%r, 1, {"object": {"value": "zqghost words"}}, "r", OP)
        """) % (ROOT, os.path.dirname(__file__), self.root, mid)
        p = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 7, p.stderr[-500:])
        self.s.add(rec("next write"), "x")            # this store stayed open across the other's death
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(self.s.search("zqghost")["outcome"], "not_found")
        self.assertEqual(self.s.search("zqold")["outcome"], "found")
        self.assertNotIn(b"zqghost", self.every_byte())

    def test_generations_are_not_a_counter(self):
        self.s.add(rec("x"), "x")
        value = json.dumps(self.s._meta("generation"))
        self.assertGreater(len(value), 10)


if __name__ == "__main__":
    unittest.main()
