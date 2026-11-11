"""Confirmed defects from a third independent attack on the memory core."""
import datetime
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import PolicyRefused, Store, ValidationError  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
W = {"id": "ENT-w", "role": "worker"}
MAIN = {"reader_scope": "main"}
TOOL = {"authorship_channel": "tool_output"}
THINKING = "\U0001f914"


class Attack(Base):
    def forge(self, *sql):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        for statement in sql:
            con.execute(statement)
        con.commit()
        con.close()

    def worker_ev(self, data=b"worker bytes", source="w.md"):
        return self.s.put_evidence(data, source, "tool_output", W)

    def relabel_round_trip(self, mid):
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, **MAIN)
        self.s.correct(mid, 2, {"sensitivity": "internal"}, "r", OP, **MAIN)


class Tokens(Attack):
    def test_an_emoji_glued_to_a_word_can_be_written_and_found(self):
        text = "zqalpha" + THINKING + " tail"
        mid = self.s.add(rec(text), "x")
        self.assertEqual(self.s.search("zqalpha" + THINKING)["hits"][0]["memory_id"], mid)

    def test_a_word_past_the_first_eight_is_still_found(self):
        mid = self.s.add(rec("a" + THINKING + " b c d e f g h zqlate"), "x")
        self.assertEqual(self.s.search("zqlate")["hits"][0]["memory_id"], mid)

    def test_a_quote_inside_a_word_is_only_text(self):
        mid = self.s.add(rec('zq"quoted words'), "x")
        self.assertEqual(self.s.search('zq"quoted')["hits"][0]["memory_id"], mid)

    def test_a_control_character_in_a_query_only_separates(self):
        mid = self.s.add(rec("zqalpha zqbeta"), "x")
        self.assertEqual(self.s.search("zqalpha\x00zqbeta")["hits"][0]["memory_id"], mid)

    def test_symbols_beside_a_word_change_nothing(self):
        mid = self.s.add(rec("zqwords here"), "x")
        self.assertEqual(self.s.search("zqwords !!")["hits"][0]["memory_id"], mid)

    def test_a_text_that_opens_with_symbols_can_be_written(self):
        mid = self.s.add(rec("! ? ! ? ! ? ! ? ! zqafter"), "x")
        self.assertEqual(self.s.search("zqafter")["hits"][0]["memory_id"], mid)

    def test_only_symbols_is_no_query(self):
        self.s.add(rec("words"), "x")
        self.assertEqual(self.s.search(THINKING + " !!")["outcome"], "not_found")


class Verify(Attack):
    def two_versions(self):
        mid = self.s.add(rec("first words"), "x")
        self.s.correct(mid, 1, {"object": {"value": "second words"}}, "r", OP)
        return mid

    def test_a_forged_closing_on_the_current_version_is_reported(self):
        self.two_versions()
        self.forge("UPDATE versions SET closed_at='1999-01-01T00:00:00Z' WHERE version=2")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_deleted_old_version_is_reported(self):
        self.two_versions()
        self.forge("DELETE FROM versions WHERE version=1")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_deleted_middle_version_is_reported(self):
        mid = self.two_versions()
        self.s.correct(mid, 2, {"object": {"value": "third words"}}, "r", OP)
        self.forge("DELETE FROM versions WHERE version=2")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_lost_evidence_row_is_reported_and_its_blob_survives_the_open(self):
        e = self.worker_ev()
        self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x")
        self.forge("DELETE FROM evidence WHERE id='%s'" % e)
        self.assertTrue(self.s.verify(**MAIN))
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(len(os.listdir(os.path.join(self.root, "archive"))), 1, "damage must not be swept away")

    def test_a_rewritten_evidence_row_is_reported(self):
        e = self.worker_ev()
        self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x")
        self.forge("UPDATE evidence SET source='forged.md' WHERE id='%s'" % e)
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_lost_evidence_row_is_reported_to_a_worker_too(self):
        e = self.worker_ev()
        self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x")
        self.forge("DELETE FROM evidence WHERE id='%s'" % e)
        self.assertTrue(self.s.verify())

    def test_a_rewritten_tombstone_is_reported(self):
        mid = self.s.add(rec("gone"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.forge("UPDATE tombstones SET at='1999-01-01'")
        self.assertTrue(self.s.verify(**MAIN))

    def test_lost_tombstones_are_reported(self):
        mid = self.s.add(rec("gone"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertEqual(self.s.verify(**MAIN), [])
        self.forge("DELETE FROM tombstones")
        self.assertTrue(self.s.verify(**MAIN))

    def test_verify_reads_one_snapshot(self):
        self.s.add(rec("x"), "x")
        seen = []
        real = self.s._verify_records

        def spy(scope):
            seen.append(self.s.db.in_transaction)
            return real(scope)

        self.s._verify_records = spy
        self.s.verify(**MAIN)
        self.assertEqual(seen, [True])
        self.assertFalse(self.s.db.in_transaction)


class Validity(Attack):
    def test_adding_an_end_date_does_not_collapse_the_old_version(self):
        mid = self.s.add(rec("x", valid_time={"from": "2026-01-01", "to": None}), "x")
        self.s.correct(mid, 1, {"valid_time": {"from": "2026-01-01", "to": "2026-06-01"}}, "r", OP)
        self.assertIsNone(self.s.history(mid)[0]["valid_time"]["to"])

    def test_respelling_the_same_instant_changes_nothing(self):
        mid = self.s.add(rec("x", valid_time={"from": "2026-01-01T12:00:00+00:00", "to": None}), "x")
        self.s.correct(mid, 1, {"valid_time": {"from": "2026-01-01T14:00:00+02:00", "to": None}}, "r", OP)
        self.assertIsNone(self.s.history(mid)[0]["valid_time"]["to"])

    def test_as_of_does_not_show_a_later_closing(self):
        mid = self.s.add(rec("first"), "x")
        then = self.clock()
        self.s.correct(mid, 1, {"object": {"value": "second"}}, "r", OP)
        view = self.s.get(mid, as_of=then)
        self.assertIsNone(view["system_time"]["superseded_at"])
        self.assertIsNone(view["valid_time"]["to"])


class RoundTrip(Attack):
    def test_a_relabel_round_trip_keeps_the_workers_citation_and_edits(self):
        e = self.worker_ev()
        mid = self.s.add(rec("canon", evidence_ids=[e], **TOOL), "x")
        self.relabel_round_trip(mid)
        self.assertEqual(self.s.get(mid)["evidence_ids"], [e])
        self.assertEqual(self.s.evidence_sources(mid), ["w.md"])
        self.s.correct(mid, 3, {"confidence": 0.5}, "r", W)


class Malformed(Attack):
    def test_every_malformed_add_is_a_validation_error(self):
        for bad in (rec(record_type=["x"]), rec(status={"a": 1}), rec(sensitivity=[]), rec(authorship_channel={}),
                    rec("x", object={"value": b"bytes"}), rec("x", object={"value": {1, 2}}), rec(object={1: 2, "a": 3}),
                    "not a dict", None):
            with self.subTest(repr(bad)[:50]):
                with self.assertRaises(ValidationError):
                    self.s.add(bad, "x")

    def test_malformed_changes_are_a_validation_error(self):
        mid = self.s.add(rec("x"), "x")
        for bad in (None, "x", {"status": ["a"]}):
            with self.subTest(repr(bad)):
                with self.assertRaises(ValidationError):
                    self.s.correct(mid, 1, bad, "r", OP)

    def test_a_bad_actor_is_a_validation_error_and_leaves_no_blob(self):
        bad = {"id": "bad \ud800", "role": "x"}
        with self.assertRaises(ValidationError):
            self.s.put_evidence(b"zqorphan3", "a.md", "tool_output", bad)
        self.assertNotIn(b"zqorphan3", self.every_byte())
        mid = self.s.add(rec("x"), "x")
        with self.assertRaises(ValidationError):
            self.s.correct(mid, 1, {"confidence": 0.5}, "r", bad)
        with self.assertRaises(ValidationError):
            self.s.erase(mid, "asked", bad, **MAIN)

    def test_nesting_past_the_limit_is_refused(self):
        deep = "x"
        for _ in range(70):
            deep = [deep]
        with self.assertRaises(ValidationError):
            self.s.add(rec("x", object={"value": deep}), "x")

    def test_values_that_are_not_data_are_refused(self):
        for bad in (rec(valid_time={"from": "2026-01-01", "to": ""}), rec("x", object={"value": math.nan}),
                    rec("x", object={"value": math.inf})):
            with self.subTest(repr(bad)[:50]):
                with self.assertRaises(ValidationError):
                    self.s.add(bad, "x")


class Tombstones(Attack):
    def test_true_is_not_one(self):
        mid = self.s.add(rec("x", object={"value": 1}), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        other = self.s.add(rec("x", object={"value": True}), "x")
        with self.assertRaises(PolicyRefused):
            self.s.correct(other, 1, {"object": {"value": 1}}, "r", OP)

    def test_an_unchanged_correction_is_not_a_return(self):
        team = self.s.add(rec("same words"), "x")
        hidden = self.s.add(rec("same words", sensitivity="private"), "x", **MAIN)
        self.s.erase(hidden, "asked", OP, **MAIN)
        self.s.correct(team, 1, {"confidence": 0.5}, "r", OP)
        kinds = [e["kind"] for e in self.s.events(hidden, **MAIN)]
        self.assertNotIn("erased_returned", kinds)


class Crash(Attack):
    def test_a_rebuild_never_sees_the_write_it_is_catching_up_for(self):
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
            s.correct(%r, 1, {"object": {"value": "zqghost one"}}, "r", OP)
        """) % (ROOT, os.path.dirname(__file__), self.root, mid)
        p = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 7, p.stderr[-500:])
        real = self.s._next_generation
        seen = []

        def spy():
            seen.append(self.s.db.execute("SELECT count(*) FROM versions").fetchone()[0])
            return real()

        self.s._next_generation = spy
        rebuilds = []
        rebuild = self.s._rebuild_locked

        def spy_rebuild():
            rebuilds.append(self.s.db.execute("SELECT count(*) FROM versions").fetchone()[0])
            return rebuild()

        self.s._rebuild_locked = spy_rebuild
        self.s.correct(mid, 1, {"object": {"value": "zqreal words"}}, "r", OP)
        self.assertTrue(rebuilds, "the indexes were ahead, so a catch-up had to happen")
        self.assertEqual(rebuilds[0], 1, "the catch-up ran before this transaction wrote anything")


if __name__ == "__main__":
    unittest.main()
