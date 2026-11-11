import copy
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald.memory import PolicyRefused, ReadAfterWriteFailed, Store, ValidationError, VersionConflict  # noqa: E402

OP = {"id": "ENT-operator", "role": "operator"}
MODEL = {"id": "ENT-model", "role": "assistant"}


def rec(text="prefers tea in the morning", **over):
    r = {
        "record_type": "preference",
        "subject_id": "ENT-operator",
        "predicate": "prefers",
        "object": {"value": text},
        "status": "canonical",
        "authority": "operator_assertion",
        "authorship_channel": "user_message",
        "actor": dict(OP),
        "valid_time": {"from": "2026-05-24", "to": None},
        "evidence_ids": [],
        "sensitivity": "internal",
        "retention_policy": "until_corrected_or_erased",
        "resurfacing_policy": "when_relevant",
    }
    r.update(over)
    return r


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return "2026-11-08T10:%02d:%02dZ" % (self.n // 60, self.n % 60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "mem")
        self.clock = Clock()
        self.s = Store(self.root, clock=self.clock)
        self.addCleanup(lambda: getattr(self.s, "close", lambda: None)())

    def every_byte(self):
        out = b""
        for d, _, names in os.walk(self.root):
            for n in names:
                with open(os.path.join(d, n), "rb") as f:
                    out += f.read()
        return out


class Schema(Base):
    def test_valid_record_is_stored(self):
        mid = self.s.add(rec(), "first")
        got = self.s.get(mid)
        self.assertEqual(got["version"], 1)
        self.assertEqual(got["object"], {"value": "prefers tea in the morning"})
        self.assertIsNotNone(got["system_time"]["created_at"])

    def assertRefused(self, r):
        with self.assertRaises(ValidationError):
            self.s.add(r, "x")

    def test_authorship_is_mandatory(self):
        for key in ("authorship_channel", "actor"):
            r = rec()
            del r[key]
            self.assertRefused(r)
        self.assertRefused(rec(actor={"role": "operator"}))
        self.assertRefused(rec(actor={"id": "ENT-operator"}))

    def test_closed_vocabularies(self):
        self.assertRefused(rec(record_type="gossip"))
        self.assertRefused(rec(status="rejected"))
        self.assertRefused(rec(authorship_channel="vibes"))
        self.assertRefused(rec(sensitivity="kinda"))

    def test_every_canon_vocabulary_value_is_accepted(self):
        for t in ("episodic", "semantic", "procedural", "preference", "identity", "relationship", "project",
                  "decision", "task/promise", "failure", "source/archive", "governance", "working"):
            self.s.add(rec("t " + t, record_type=t), "x")
        for c in ("user_asserted", "jointly_confirmed", "system_observed", "externally_sourced", "user_message",
                  "user_document", "tool_output", "external_source", "system_event"):
            self.s.add(rec("c " + c, authorship_channel=c, status="pending"), "x")
        for v in ("public", "internal", "private", "highly_private", "third_party_restricted"):
            self.s.add(rec("v " + v, sensitivity=v), "x", reader_scope="main")

    def test_the_store_owns_its_own_fields_and_statuses(self):
        self.assertRefused(rec(version=9))
        self.assertRefused(rec(system_time={"created_at": "x"}))
        self.assertRefused(rec(status="tombstoned"))
        self.assertRefused(rec(status="superseded"))
        self.assertRefused(rec(nickname="typo field"))

    def test_valid_time_needs_a_start_and_an_order(self):
        self.assertRefused(rec(valid_time={"to": None}))
        self.assertRefused(rec(valid_time={"from": "2026-06-02", "to": "2026-06-01"}))

    def test_secrets_are_not_memory(self):
        for scope in ("worker", "main"):
            with self.assertRaises(PolicyRefused):
                self.s.add(rec(sensitivity="secret"), "x", reader_scope=scope)

    def test_model_claim_is_never_promoted_directly(self):
        for ch in ("model_inference", "assistant_asserted", "derived"):
            with self.assertRaises(PolicyRefused):
                self.s.add(rec(authorship_channel=ch, actor=dict(MODEL)), "x")
            e = self.s.put_evidence(b"some log", "run.log", "tool_output", {"id": "ENT-tool", "role": "tool"})
            with self.assertRaises(PolicyRefused, msg="evidence does not make a model's claim canonical"):
                self.s.add(rec("with proof " + ch, authorship_channel=ch, actor=dict(MODEL), evidence_ids=[e]), "x")
            mid = self.s.add(rec("guess " + ch, authorship_channel=ch, actor=dict(MODEL), status="hypothesis"), "x")
            self.assertEqual(self.s.get(mid)["status"], "hypothesis")

    def test_canonical_needs_evidence_or_a_direct_assertion(self):
        with self.assertRaises(PolicyRefused):
            self.s.add(rec(authorship_channel="tool_output", evidence_ids=[]), "x")
        with self.assertRaises(ValidationError):
            self.s.add(rec(authorship_channel="tool_output", evidence_ids=["EVD-missing"]), "x")
        e = self.s.put_evidence(b"log line", "run.log", "tool_output", {"id": "ENT-tool", "role": "tool"})
        self.s.add(rec(authorship_channel="tool_output", evidence_ids=[e]), "x")


class Archive(Base):
    def test_content_addressed_and_deduplicated(self):
        data = "первая строка".encode("utf-8")
        a = self.s.put_evidence(data, "a.md", "user_document", OP)
        b = self.s.put_evidence(data, "b.md", "user_document", OP)
        sha = hashlib.sha256(data).hexdigest()
        path = os.path.join(self.root, "archive", sha)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertNotEqual(a, b, "two sources are two pieces of evidence over one blob")
        self.assertEqual(len(os.listdir(os.path.join(self.root, "archive"))), 1)

    def test_evidence_must_name_channel_and_actor(self):
        with self.assertRaises(ValidationError):
            self.s.put_evidence(b"x", "a.md", "vibes", OP)
        with self.assertRaises(ValidationError):
            self.s.put_evidence(b"x", "a.md", "user_document", {"id": "x"})

    def test_sources_of_a_record(self):
        e = self.s.put_evidence(b"text", "notes/a.md", "user_document", OP)
        mid = self.s.add(rec("x", evidence_ids=[e]), "x")
        self.assertEqual(self.s.evidence_sources(mid), ["notes/a.md"])

    def test_sources_of_a_private_record_answer_like_a_missing_id(self):
        e = self.s.put_evidence(b"text", "secret.md", "user_document", OP, reader_scope="main")
        mid = self.s.add(rec("x", evidence_ids=[e], sensitivity="private"), "x", reader_scope="main")
        self.assertEqual(self.s.evidence_sources(mid), self.s.evidence_sources("MEM-none"))
        self.assertEqual(self.s.evidence_sources(mid), [])
        self.assertEqual(self.s.evidence_sources(mid, reader_scope="main"), ["secret.md"])
        with self.assertRaises(PolicyRefused):
            self.s.evidence_sources(mid, reader_scope="root")

    def test_a_changed_blob_is_reported(self):
        data = b"untouched"
        self.s.put_evidence(data, "a.md", "user_document", OP)
        self.assertEqual(self.s.verify(), [])
        with open(os.path.join(self.root, "archive", hashlib.sha256(data).hexdigest()), "ab") as f:
            f.write(b"!")
        self.assertTrue(any("archive" in p for p in self.s.verify()))


class Corrections(Base):
    def test_correction_keeps_both_versions(self):
        mid = self.s.add(rec("likes tea"), "first")
        v = self.s.correct(mid, 1, {"object": {"value": "likes coffee"}}, "she said so", OP)
        self.assertEqual(v, 2)
        old, new = self.s.history(mid)
        self.assertEqual(old["object"], {"value": "likes tea"})
        self.assertEqual(new["object"], {"value": "likes coffee"})
        self.assertIsNotNone(old["system_time"]["superseded_at"])
        self.assertIsNone(new["system_time"]["superseded_at"])
        self.assertEqual(new["supersedes"], 1)
        self.assertIsNotNone(old["valid_time"]["to"])
        self.assertEqual(self.s.get(mid)["version"], 2)

    def test_stale_expected_version_changes_nothing(self):
        mid = self.s.add(rec("likes tea"), "first")
        self.s.correct(mid, 1, {"object": {"value": "likes coffee"}}, "r", OP)
        before = len(self.s.events(mid))
        with self.assertRaises(VersionConflict):
            self.s.correct(mid, 1, {"object": {"value": "likes water"}}, "r", OP)
        self.assertEqual(self.s.get(mid)["object"], {"value": "likes coffee"})
        self.assertEqual(len(self.s.history(mid)), 2)
        self.assertEqual(len(self.s.events(mid)), before)

    def test_a_correction_cannot_smuggle_a_bad_record(self):
        mid = self.s.add(rec("likes tea"), "first")
        with self.assertRaises(ValidationError):
            self.s.correct(mid, 1, {"record_type": "gossip"}, "r", OP)
        with self.assertRaises(PolicyRefused):
            self.s.correct(mid, 1, {"authorship_channel": "model_inference", "actor": dict(MODEL)}, "r", OP)
        self.assertEqual(self.s.get(mid)["version"], 1)

    def test_what_the_system_knew_at_a_moment(self):
        mid = self.s.add(rec("likes tea"), "first")
        t1 = self.s.get(mid)["system_time"]["created_at"]
        self.s.correct(mid, 1, {"object": {"value": "likes coffee"}}, "r", OP)
        self.assertEqual(self.s.get(mid, as_of=t1)["object"], {"value": "likes tea"})
        self.assertEqual(self.s.get(mid)["object"], {"value": "likes coffee"})
        self.assertIsNone(self.s.get(mid, as_of="2026-01-01T00:00:00Z"))


class Events(Base):
    def test_each_mutation_is_a_full_event(self):
        mid = self.s.add(rec("likes tea"), "first")
        self.s.correct(mid, 1, {"object": {"value": "likes coffee"}}, "she said so", OP)
        ev = self.s.events(mid)
        self.assertEqual([e["kind"] for e in ev], ["add", "correct"])
        e = ev[1]
        for key in ("old_sha", "new_sha", "reason", "actor_id", "actor_role", "schema_version", "rollback_version",
                    "at", "prev_hash", "hash"):
            self.assertIn(key, e)
        self.assertEqual(e["rollback_version"], 1)
        self.assertEqual(e["prev_hash"], ev[0]["hash"])

    def test_events_hold_no_content(self):
        mid = self.s.add(rec("marker-zebra-content"), "first")
        self.assertNotIn("marker-zebra-content", repr(self.s.events(mid)))

    def test_the_database_refuses_to_rewrite_events(self):
        self.s.add(rec(), "first")
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        self.addCleanup(con.close)
        for sql in ("UPDATE events SET kind='x'", "DELETE FROM events"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                con.execute(sql)

    def test_a_broken_chain_is_reported(self):
        self.s.add(rec("one"), "first")
        self.s.add(rec("two"), "second")
        self.assertEqual(self.s.verify(), [])
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        self.addCleanup(con.close)
        con.execute("DROP TRIGGER events_no_update")
        con.execute("UPDATE events SET kind='forged' WHERE seq=1")
        con.commit()
        con.close()
        self.assertTrue(any("events" in p for p in self.s.verify()))

    def test_status_is_computable_from_the_events(self):
        mid = self.s.add(rec("h", status="hypothesis"), "first")
        self.assertEqual(self.s.status_from_events(mid), "hypothesis")
        self.s.correct(mid, 1, {"status": "canonical"}, "confirmed", OP)
        self.assertEqual(self.s.status_from_events(mid), "canonical")


class Search(Base):
    def test_finds_russian_and_english(self):
        self.s.add(rec("кота зовут Пуговица"), "x")
        self.s.add(rec("the database is SQLite"), "x")
        self.assertEqual(self.s.search("Пуговица")["outcome"], "found")
        self.assertEqual(self.s.search("sqlite")["outcome"], "found")
        self.assertEqual(self.s.search("отсутствует")["outcome"], "not_found")

    def test_only_the_current_version_is_found(self):
        mid = self.s.add(rec("likes oolong"), "x")
        self.s.correct(mid, 1, {"object": {"value": "likes espresso"}}, "r", OP)
        self.assertEqual(self.s.search("oolong")["outcome"], "not_found")
        self.assertEqual(self.s.search("espresso")["hits"][0]["memory_id"], mid)

    def test_a_query_with_fts_syntax_is_only_words(self):
        self.s.add(rec("alpha beta"), "x")
        for q in ('"', "alpha AND", "NEAR(", "a*b:c", "-"):
            self.assertIn(self.s.search(q)["outcome"], ("found", "not_found"), q)


class Privacy(Base):
    def test_a_worker_index_never_holds_private_text(self):
        for label in ("private", "highly_private", "third_party_restricted"):
            self.s.add(rec("marker-otter-" + label.replace("_", ""), sensitivity=label), "x", reader_scope="main")
        with open(os.path.join(self.root, "lexical_worker.sqlite"), "rb") as f:
            self.assertNotIn(b"marker-otter", f.read())
        self.assertEqual(self.s.search("marker otter", reader_scope="worker")["outcome"], "not_found")
        self.assertEqual(self.s.search("marker-otter-private", reader_scope="main")["outcome"], "found")

    def test_the_default_reader_is_the_narrow_one(self):
        mid = self.s.add(rec("marker-mole", sensitivity="private"), "x", reader_scope="main")
        self.assertEqual(self.s.search("marker-mole")["outcome"], "not_found")
        self.assertIsNone(self.s.get(mid))
        self.assertEqual(self.s.history(mid), [])

    def test_a_worker_cannot_learn_that_a_private_match_exists(self):
        before = self.s.search("marker-heron", reader_scope="worker")
        self.s.add(rec("marker-heron", sensitivity="private"), "x", reader_scope="main")
        self.assertEqual(self.s.search("marker-heron", reader_scope="worker"), before)
        self.assertEqual(before["outcome"], "not_found")
        self.assertEqual(before["scope"], "excludes_private")

    def test_a_private_id_answers_like_an_id_that_never_existed(self):
        mid = self.s.add(rec("x", sensitivity="private"), "x", reader_scope="main")
        self.assertEqual(self.s.get(mid, reader_scope="worker"), self.s.get("MEM-none", reader_scope="worker"))
        self.assertEqual(self.s.history(mid, reader_scope="worker"), self.s.history("MEM-none", reader_scope="worker"))
        self.assertIsNone(self.s.get(mid, reader_scope="worker"))
        self.assertEqual(self.s.get(mid, reader_scope="main")["version"], 1)

    def test_an_unknown_reader_is_refused_not_trusted(self):
        with self.assertRaises(PolicyRefused):
            self.s.search("x", reader_scope="root")

    def test_raising_sensitivity_leaves_the_worker_index(self):
        mid = self.s.add(rec("marker-lynx", sensitivity="internal"), "x", reader_scope="main")
        self.assertEqual(self.s.search("marker-lynx", reader_scope="worker")["outcome"], "found")
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, reader_scope="main")
        self.assertEqual(self.s.search("marker-lynx", reader_scope="worker")["outcome"], "not_found")
        self.assertEqual(self.s.search("marker-lynx", reader_scope="main")["outcome"], "found")
        with open(os.path.join(self.root, "lexical_worker.sqlite"), "rb") as f:
            self.assertNotIn(b"marker-lynx", f.read(), "a deleted FTS row must not stay in its segment")

    def test_a_private_record_keeps_its_old_versions_from_a_worker(self):
        mid = self.s.add(rec("likes tea"), "x")
        t1 = self.s.get(mid)["system_time"]["created_at"]
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, reader_scope="main")
        self.assertIsNone(self.s.get(mid, reader_scope="worker", as_of=t1))
        self.assertEqual(self.s.history(mid, reader_scope="worker"), [])


class Rebuild(Base):
    def snapshot(self):
        return [(q, r, sorted(h["memory_id"] for h in self.s.search(q, reader_scope=r)["hits"]))
                for q in ("tea", "кофе", "marker") for r in ("worker", "main")]

    def fill(self):
        self.s.add(rec("tea at noon"), "x")
        self.s.add(rec("кофе утром"), "x")
        self.s.add(rec("marker of tea", sensitivity="private"), "x", reader_scope="main")

    def test_indexes_come_back_from_durable_records(self):
        self.fill()
        want = self.snapshot()
        for name in ("lexical_worker.sqlite", "lexical_main.sqlite"):
            os.remove(os.path.join(self.root, name))
        self.s.rebuild()
        self.assertEqual(self.snapshot(), want)
        self.assertTrue(any(h for _, _, h in want))

    def test_a_missing_index_answers_unknown_never_not_found(self):
        self.fill()
        os.remove(os.path.join(self.root, "lexical_worker.sqlite"))
        self.assertEqual(self.s.search("tea", reader_scope="worker")["outcome"], "unknown")
        self.assertFalse(os.path.exists(os.path.join(self.root, "lexical_worker.sqlite")), "a search must not create one")

    def test_a_garbage_index_answers_unknown(self):
        self.fill()
        with open(os.path.join(self.root, "lexical_worker.sqlite"), "wb") as f:
            f.write(b"not a database")
        self.assertEqual(self.s.search("tea", reader_scope="worker")["outcome"], "unknown")

    def test_a_write_restores_a_missing_index_from_the_records(self):
        self.fill()
        os.remove(os.path.join(self.root, "lexical_main.sqlite"))
        self.s.add(rec("marker-new"), "x")
        self.assertEqual(self.s.search("marker-new", reader_scope="main")["outcome"], "found")
        self.assertEqual(self.s.search("tea", reader_scope="main")["outcome"], "found")

    def test_a_garbage_index_is_replaced(self):
        self.fill()
        want = self.snapshot()
        with open(os.path.join(self.root, "lexical_worker.sqlite"), "wb") as f:
            f.write(b"not a database")
        self.s.rebuild()
        self.assertEqual(self.snapshot(), want)


class ReadAfterWrite(Base):
    def test_a_canonical_record_the_index_cannot_find_is_not_written(self):
        real = self.s._index
        self.s._index = lambda *a, **k: None
        with self.assertRaises(ReadAfterWriteFailed):
            self.s.add(rec("marker-gone"), "x")
        self.s._index = real
        self.assertEqual(self.s.search("marker-gone", reader_scope="main")["outcome"], "not_found")
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT count(*) FROM events").fetchone()[0], 0)


    def lex_rows(self):
        total = 0
        for name in ("lexical_worker.sqlite", "lexical_main.sqlite"):
            con = sqlite3.connect(os.path.join(self.root, name))
            total += con.execute("SELECT count(*) FROM lex").fetchone()[0]
            con.close()
        return total

    def test_a_failed_add_leaves_no_index_row(self):
        real = self.s._found_by_search
        self.s._found_by_search = lambda doc: False
        with self.assertRaises(ReadAfterWriteFailed):
            self.s.add(rec("marker-ghost"), "x")
        self.s._found_by_search = real
        self.assertEqual(self.lex_rows(), 0)

    def test_a_failed_correction_puts_the_old_text_back(self):
        mid = self.s.add(rec("likes oolong"), "x")
        real = self.s._found_by_search
        self.s._found_by_search = lambda doc: False
        with self.assertRaises(ReadAfterWriteFailed):
            self.s.correct(mid, 1, {"object": {"value": "likes espresso"}}, "r", OP)
        self.s._found_by_search = real
        self.assertEqual(self.s.search("espresso")["outcome"], "not_found")
        self.assertEqual(self.s.search("oolong")["hits"][0]["version"], 1)


class Erasure(Base):
    def test_no_byte_of_the_erased_text_is_left(self):
        e = self.s.put_evidence("marker-walrus source".encode(), "a.md", "user_document", OP)
        mid = self.s.add(rec("marker-walrus one", evidence_ids=[e]), "x")
        self.s.correct(mid, 1, {"object": {"value": "marker-walrus two"}}, "r", OP)
        self.assertIn(b"marker-walrus", self.every_byte())
        self.s.erase(mid, "asked", OP, reader_scope="main")
        self.assertNotIn(b"marker-walrus", self.every_byte())
        self.assertEqual(self.s.search("marker-walrus", reader_scope="main")["outcome"], "not_found")
        self.assertEqual(self.s.status_from_events(mid), "tombstoned")
        self.assertEqual(self.s.verify(), [], "the event chain survives an erasure")

    def test_an_interrupted_erasure_finishes_on_the_next_open(self):
        mid = self.s.add(rec("marker-puffin"), "x")
        real = self.s._finish_erasures
        self.s._finish_erasures = lambda: None
        self.s.erase(mid, "asked", OP, reader_scope="main")
        self.assertTrue(any("erasure" in p for p in self.s.verify()))
        self.s._finish_erasures = real
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertNotIn(b"marker-puffin", self.every_byte())
        self.assertEqual(self.s.verify(), [])

    def test_one_blob_under_two_sources_outlives_one_erasure(self):
        a = self.s.put_evidence(b"same bytes twice", "a.md", "user_document", OP)
        b = self.s.put_evidence(b"same bytes twice", "b.md", "user_document", OP)
        first = self.s.add(rec("one", evidence_ids=[a]), "x")
        self.s.add(rec("two", evidence_ids=[b]), "x")
        self.s.erase(first, "asked", OP, reader_scope="main")
        self.assertEqual(self.s.verify(), [])
        self.assertIn(b"same bytes twice", self.every_byte())

    def test_shared_evidence_outlives_one_erasure(self):
        e = self.s.put_evidence(b"shared source", "a.md", "user_document", OP)
        a = self.s.add(rec("one", evidence_ids=[e]), "x")
        self.s.add(rec("two", evidence_ids=[e]), "x")
        self.s.erase(a, "asked", OP, reader_scope="main")
        self.assertIn(b"shared source", self.every_byte())

    def test_the_tombstone_blocks_resurrection(self):
        mid = self.s.add(rec("forget this"), "x")
        self.s.erase(mid, "asked", OP, reader_scope="main")
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("forget this"), "extracted again")
        self.assertIsNone(self.s.get(mid))

    def test_ledger_has_one_row_per_class_and_none_pending(self):
        mid = self.s.add(rec("x"), "x")
        self.s.erase(mid, "asked", OP, reader_scope="main")
        rows = self.s.erasure_ledger(mid)
        self.assertEqual({r["class"] for r in rows},
                         {"records", "versions", "archive", "lexical_worker", "lexical_main"})
        self.assertEqual({r["state"] for r in rows}, {"done"})

    def test_erased_record_cannot_be_corrected(self):
        mid = self.s.add(rec("x"), "x")
        self.s.erase(mid, "asked", OP, reader_scope="main")
        with self.assertRaises(PolicyRefused):
            self.s.correct(mid, 1, {"object": {"value": "back"}}, "r", OP)


class Oracles(Base):
    """A worker must get the same answer for a private id as for an id that never existed, everywhere."""

    def private(self, text="marker-newt"):
        return self.s.add(rec(text, sensitivity="private"), "x", reader_scope="main")

    def test_the_audit_readers_do_not_confirm_a_private_id(self):
        mid = self.private()
        self.assertEqual(self.s.events(mid, reader_scope="worker"), self.s.events("MEM-none", reader_scope="worker"))
        self.assertIsNone(self.s.status_from_events(mid, reader_scope="worker"))
        self.assertEqual(self.s.erasure_ledger(mid, reader_scope="worker"), [])
        self.assertEqual(len(self.s.events(mid, reader_scope="main")), 1)
        self.assertEqual(self.s.status_from_events(mid, reader_scope="main"), "canonical")

    def test_an_erased_private_record_stays_hidden_from_the_audit_readers(self):
        mid = self.private()
        self.s.erase(mid, "asked", OP, reader_scope="main")
        self.assertEqual(self.s.events(mid, reader_scope="worker"), [])
        self.assertIsNone(self.s.status_from_events(mid, reader_scope="worker"))
        self.assertEqual(self.s.erasure_ledger(mid, reader_scope="worker"), [])
        self.assertEqual(self.s.status_from_events(mid, reader_scope="main"), "tombstoned")

    def test_a_worker_cannot_choose_an_id(self):
        mid = self.private()
        for chosen in (mid, "MEM-free"):
            with self.assertRaises(PolicyRefused):
                self.s.add(rec("y", memory_id=chosen), "x")

    def test_ids_do_not_count_the_private_records(self):
        first = self.s.add(rec("one"), "x")
        for i in range(3):
            self.private("hidden number %d" % i)
        second = self.s.add(rec("two"), "x")
        for memory_id in (first, second):
            self.assertRegex(memory_id, r"\AMEM-[0-9a-f]{16}\Z")
        self.assertNotEqual(first, second)

    def test_correct_on_a_private_id_is_a_missing_id(self):
        mid = self.private()
        for version in (1, 99):
            with self.assertRaises(KeyError):
                self.s.correct(mid, version, {"object": {"value": "z"}}, "r", OP)
            with self.assertRaises(KeyError):
                self.s.correct("MEM-none", version, {"object": {"value": "z"}}, "r", OP)
        self.assertEqual(self.s.get(mid, reader_scope="main")["version"], 1)

    def test_correct_on_an_erased_private_id_is_a_missing_id(self):
        mid = self.private()
        self.s.erase(mid, "asked", OP, reader_scope="main")
        with self.assertRaises(KeyError):
            self.s.correct(mid, 1, {"object": {"value": "z"}}, "r", OP)

    def test_a_worker_writes_nothing_private(self):
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("x", sensitivity="private"), "x")
        mid = self.s.add(rec("visible"), "x")
        with self.assertRaises(PolicyRefused):
            self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP)
        self.assertEqual(self.s.get(mid)["version"], 1)

    def test_only_main_erases_and_the_refusal_does_not_depend_on_the_id(self):
        visible = self.s.add(rec("visible"), "x")
        for target in (visible, self.private(), "MEM-none"):
            with self.assertRaises(PolicyRefused):
                self.s.erase(target, "asked", OP)
        self.assertEqual(self.s.get(visible)["version"], 1)

    def test_a_private_tombstone_says_nothing_to_a_worker(self):
        mid = self.private("once existed")
        self.s.erase(mid, "asked", OP, reader_scope="main")
        self.s.add(rec("once existed"), "a worker wrote the same words")
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("once existed", sensitivity="private"), "x", reader_scope="main")

    def test_main_sees_erased_text_come_back_and_the_worker_sees_nothing(self):
        old = self.private("marker-tern returned")
        self.s.erase(old, "asked", OP, reader_scope="main")
        new = self.s.add(rec("marker-tern returned"), "worker wrote it")
        ordinary = self.s.add(rec("another text entirely"), "worker wrote it")
        self.assertEqual([e["kind"] for e in self.s.events(new)], ["add"])
        self.assertEqual(self.s.status_from_events(new), self.s.status_from_events(ordinary))
        self.assertEqual(self.s.events(old), [])
        kinds = [e for e in self.s.events(old, reader_scope="main") if e["kind"] == "erased_returned"]
        self.assertEqual(len(kinds), 1)
        self.assertIn(new, kinds[0]["reason"])
        self.assertNotIn("marker-tern", repr(self.s.events(old, reader_scope="main")))
        self.assertEqual(self.s.status_from_events(old, reader_scope="main"), "tombstoned")
        self.assertEqual(self.s.verify(), [])

    def test_a_correction_that_brings_erased_text_back_is_seen_too(self):
        old = self.private("marker-skua returned")
        self.s.erase(old, "asked", OP, reader_scope="main")
        mid = self.s.add(rec("harmless"), "x")
        self.s.correct(mid, 1, {"object": {"value": "marker-skua returned"}}, "r", OP)
        self.assertIn("erased_returned", [e["kind"] for e in self.s.events(old, reader_scope="main")])

    def test_a_main_write_of_ordinary_text_raises_no_event(self):
        old = self.private("marker-gull one")
        self.s.erase(old, "asked", OP, reader_scope="main")
        self.s.add(rec("marker-gull two"), "x")
        self.assertNotIn("erased_returned", [e["kind"] for e in self.s.events(old, reader_scope="main")])

    def test_a_worker_tombstone_still_blocks_a_worker(self):
        mid = self.s.add(rec("visible words"), "x")
        self.s.erase(mid, "asked", OP, reader_scope="main")
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("visible words"), "x")

    def test_the_tombstone_is_not_a_plain_hash_of_the_text(self):
        mid = self.s.add(rec("yes"), "x")
        self.s.erase(mid, "asked", OP, reader_scope="main")
        plain = hashlib.sha256('{"value":"yes"}'.encode()).hexdigest().encode()
        self.assertNotIn(plain, self.every_byte())

    def test_the_salt_is_not_in_the_database(self):
        with open(os.path.join(self.root, "tombstone.salt"), encoding="ascii") as f:
            salt = f.read().strip()
        self.assertEqual(len(salt), 64)
        with open(os.path.join(self.root, "memory.sqlite"), "rb") as f:
            self.assertNotIn(salt.encode(), f.read())

    def test_a_damaged_salt_stops_the_store_instead_of_forgetting(self):
        self.s.close()
        path = os.path.join(self.root, "tombstone.salt")
        with open(path, encoding="ascii") as f:
            good = f.read()
        with open(path, "w", encoding="ascii") as f:
            f.write("short")
        with self.assertRaises(RuntimeError):
            Store(self.root, clock=self.clock)
        with open(path, "w", encoding="ascii") as f:
            f.write(good)
        self.s = Store(self.root, clock=self.clock)

    def test_the_audit_follows_a_label_that_changed(self):
        mid = self.s.add(rec("x"), "x")
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, reader_scope="main")
        self.assertIsNone(self.s.status_from_events(mid))
        self.assertEqual(self.s.events(mid), [])
        with self.assertRaises(KeyError):
            self.s.correct(mid, 2, {"status": "pending"}, "r", OP)
        self.s.correct(mid, 2, {"sensitivity": "internal"}, "r", OP, reader_scope="main")
        self.assertEqual(self.s.status_from_events(mid), "canonical")

    def test_the_tombstone_salt_survives_a_reopen(self):
        mid = self.s.add(rec("still gone"), "x")
        self.s.erase(mid, "asked", OP, reader_scope="main")
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        with self.assertRaises(PolicyRefused):
            self.s.add(rec("still gone"), "x")

    def test_a_worker_cannot_probe_private_evidence_by_its_id(self):
        data = b"guessed private bytes"
        mine = self.s.put_evidence(data, "a.md", "user_document", OP, reader_scope="main")
        self.assertNotEqual(self.s.put_evidence(data, "a.md", "user_document", OP), mine)
        with self.assertRaises(ValidationError):
            self.s.add(rec("x", authorship_channel="tool_output", evidence_ids=[mine]), "x")
        with self.assertRaises(ValidationError):
            self.s.add(rec("x", authorship_channel="tool_output", evidence_ids=["EVD-missing"]), "x")
        self.s.add(rec("y", authorship_channel="tool_output", evidence_ids=[mine], status="pending"), "x",
                   reader_scope="main")

    def test_an_unknown_scope_is_refused_on_every_door(self):
        mid = self.s.add(rec("x"), "x")
        for door in (lambda: self.s.add(rec("y"), "x", reader_scope="root"),
                     lambda: self.s.correct(mid, 1, {"status": "pending"}, "r", OP, reader_scope="root"),
                     lambda: self.s.erase(mid, "asked", OP, reader_scope="root"),
                     lambda: self.s.events(mid, reader_scope="root"),
                     lambda: self.s.status_from_events(mid, reader_scope="root"),
                     lambda: self.s.erasure_ledger(mid, reader_scope="root"),
                     lambda: self.s.put_evidence(b"x", "a", "tool_output", OP, reader_scope="root")):
            with self.assertRaises(PolicyRefused):
                door()


class Durability(Base):
    def test_commits_reach_the_disk(self):
        self.assertEqual(self.s.db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_everything_survives_a_reopen(self):
        r = copy.deepcopy(rec("likes tea"))
        mid = self.s.add(r, "x")
        self.s.correct(mid, 1, {"object": {"value": "likes coffee"}}, "r", OP)
        getattr(self.s, "close", lambda: None)()
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(self.s.get(mid)["version"], 2)
        self.assertEqual(len(self.s.history(mid)), 2)
        self.assertEqual(self.s.search("coffee")["outcome"], "found")

    def test_add_does_not_touch_the_callers_dict(self):
        r = rec("x")
        before = copy.deepcopy(r)
        self.s.add(r, "x")
        self.assertEqual(r, before)


if __name__ == "__main__":
    unittest.main()
