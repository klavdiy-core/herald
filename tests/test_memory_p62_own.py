"""p62: the self-healing check of a lexical index looked only at its generation table, so an index whose lex table was
dropped passed as current and search answered "unknown" until someone called rebuild (reviewer M-21999). author 11.11.2026."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import Store  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}


class LexSelfHealing(Base):
    def drop_lex(self, name):
        con = sqlite3.connect(os.path.join(self.root, name))
        con.execute("DROP TABLE lex")
        con.commit()
        con.close()

    def test_an_index_without_its_lex_table_is_rebuilt_on_open(self):
        x = self.s.add(rec("heron ladder"), "x", **MAIN)
        self.s.close()
        self.drop_lex("lexical_main.sqlite")
        self.drop_lex("lexical_worker.sqlite")
        self.s = Store(self.root, clock=self.clock)
        for scope in ("main", "worker"):
            answer = self.s.search("heron ladder", reader_scope=scope, limit=5)
            self.assertEqual(answer["outcome"], "found", scope)
            self.assertEqual([h["memory_id"] for h in answer["hits"]], [x], scope)
        self.assertEqual(self.s.verify(**MAIN), [])


class MissingKeptCount(Base):
    """reviewer M-22195 (2): a kept count that is gone is a problem verify names; an open never fills it in, and a later write
    does not turn it back into a count that matches (that would hide rows lost before it)."""

    def drop_count(self, key):
        self.s.close()
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        con.execute("DELETE FROM meta WHERE key=?", (key,))
        con.commit()
        con.close()
        self.s = Store(self.root, clock=self.clock)

    def meta(self, key):
        row = self.s.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def test_a_new_store_holds_both_counts_from_its_birth(self):
        self.assertEqual((self.meta("evidence_count"), self.meta("tombstone_count")), ("0", "0"))
        self.assertEqual(self.s.verify(**MAIN), [])

    def test_a_missing_evidence_count_is_named_and_not_filled_in_by_an_open(self):
        e = self.s.put_evidence(b"notes", "chat", "user_message", OP)
        self.s.add(rec("heron ladder", evidence_ids=[e]), "x", **MAIN)
        self.drop_count("evidence_count")
        self.assertIsNone(self.meta("evidence_count"), "the open filled the missing count in")
        self.assertTrue([p for p in self.s.verify(**MAIN) if "rows are missing" in p])

    def test_a_missing_count_stays_reported_after_a_write_that_changes_it(self):
        self.drop_count("evidence_count")
        self.s.put_evidence(b"more notes", "chat", "user_message", OP)
        self.assertTrue([p for p in self.s.verify(**MAIN) if "rows are missing" in p])


if __name__ == "__main__":
    unittest.main()
