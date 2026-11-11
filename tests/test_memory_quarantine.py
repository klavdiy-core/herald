"""A blob with no row is set aside for a while instead of being deleted, except what an erasure asked to delete."""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import Store  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
W = {"id": "w", "role": "w"}
DAY = 86400


class Q(Base):
    @property
    def archive(self):
        return os.path.join(self.root, "archive")

    @property
    def quarantine(self):
        return os.path.join(self.root, "quarantine")

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, clock=self.clock)

    def orphan(self, data=b"orphan bytes"):
        sha = hashlib.sha256(data).hexdigest()
        with open(os.path.join(self.archive, sha), "wb") as out:
            out.write(data)
        return sha

    def age(self, path, days):
        moment = time.time() - days * DAY
        os.utime(path, (moment, moment))

    def test_a_blob_with_no_row_is_set_aside_not_deleted(self):
        sha = self.orphan()
        self.reopen()
        self.assertNotIn(sha, os.listdir(self.archive))
        self.assertIn(sha, os.listdir(self.quarantine))

    def test_an_older_database_does_not_destroy_newer_blobs(self):
        self.s.close()
        copy = os.path.join(self.root, "old.copy")
        shutil.copy(os.path.join(self.root, "memory.sqlite"), copy)
        self.s = Store(self.root, clock=self.clock)
        self.s.put_evidence(b"newer", "n.md", "tool_output", W)
        self.s.close()
        shutil.copy(copy, os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)
        self.assertIn(hashlib.sha256(b"newer").hexdigest(), os.listdir(self.quarantine))

    def test_the_term_counts_from_the_move_not_from_the_blob(self):
        sha = self.orphan()
        self.age(os.path.join(self.archive, sha), 30)
        self.reopen()
        self.reopen()
        self.assertIn(sha, os.listdir(self.quarantine))

    def test_past_the_term_it_goes_and_within_it_stays(self):
        old, fresh = self.orphan(b"old"), self.orphan(b"fresh")
        self.reopen()
        self.age(os.path.join(self.quarantine, old), 8)
        self.age(os.path.join(self.quarantine, fresh), 6)
        self.reopen()
        self.assertEqual(os.listdir(self.quarantine), [fresh])

    def test_the_quarantine_counts_for_nothing(self):
        self.orphan()
        self.reopen()
        self.assertEqual(self.s.verify(**MAIN), [])
        self.assertEqual(os.listdir(self.archive), [])

    def test_an_interrupted_erasure_never_reaches_the_quarantine(self):
        e = self.s.put_evidence(b"zqerased", "m.md", "tool_output", OP, **MAIN)
        mid = self.s.add(rec("x", sensitivity="private", evidence_ids=[e], status="pending", authorship_channel="tool_output"), "x", **MAIN)
        with mock.patch.object(Store, "_finish_erasures"):
            self.s.erase(mid, "asked", OP, **MAIN)
        self.reopen()
        self.assertEqual(os.listdir(self.archive), [])
        self.assertFalse(os.path.isdir(self.quarantine) and os.listdir(self.quarantine))
        self.assertEqual({r["state"] for r in self.s.erasure_ledger(mid, **MAIN)}, {"done"})

    def test_an_erasure_cleans_both_places(self):
        sha = self.orphan(b"zqboth")
        self.reopen()
        self.assertIn(sha, os.listdir(self.quarantine))
        e = self.s.put_evidence(b"zqboth", "m.md", "tool_output", OP, **MAIN)
        mid = self.s.add(rec("x", sensitivity="private", evidence_ids=[e], status="pending", authorship_channel="tool_output"), "x", **MAIN)
        self.s.erase(mid, "asked", OP, **MAIN)
        self.assertNotIn(sha, os.listdir(self.archive))
        self.assertNotIn(sha, os.listdir(self.quarantine))

    def test_putting_the_same_bytes_again_works(self):
        sha = self.orphan(b"zqagain")
        self.reopen()
        e = self.s.put_evidence(b"zqagain", "a.md", "tool_output", W)
        self.assertIn(sha, os.listdir(self.archive))
        self.s.add(rec("x", evidence_ids=[e], status="pending", authorship_channel="tool_output"), "x")
        self.reopen()
        self.assertEqual(self.s.verify(**MAIN), [])

    def test_an_erasing_blob_is_never_set_aside(self):
        e = self.s.put_evidence(b"zqspy", "m.md", "tool_output", OP, **MAIN)
        mid = self.s.add(rec("x", sensitivity="private", evidence_ids=[e], status="pending", authorship_channel="tool_output"), "x", **MAIN)
        with mock.patch.object(Store, "_finish_erasures"):
            self.s.erase(mid, "asked", OP, **MAIN)
        self.s.close()
        with mock.patch.object(Store, "_set_aside") as spy:
            self.s = Store(self.root, clock=self.clock)
        spy.assert_not_called()

    def test_evidence_without_the_marker_still_refuses_a_lost_database(self):
        self.s.put_evidence(b"bytes", "a.md", "tool_output", W)
        self.s.close()
        os.remove(os.path.join(self.root, "store.created"))
        os.rename(os.path.join(self.root, "memory.sqlite"), os.path.join(self.root, "away"))
        with self.assertRaises(RuntimeError):
            Store(self.root, clock=self.clock)
        os.rename(os.path.join(self.root, "away"), os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)

    def test_a_first_open_that_fails_may_be_tried_again(self):
        root = tempfile.mkdtemp()
        with mock.patch("herald.memory.store.sqlite3.connect", side_effect=sqlite3.OperationalError("cut")):
            with self.assertRaises(sqlite3.OperationalError):
                Store(root)
        self.assertFalse(os.path.exists(os.path.join(root, "store.created")))
        Store(root).close()

    def test_a_partial_temp_file_is_still_removed(self):
        with open(os.path.join(self.archive, ".tmpabc"), "wb") as out:
            out.write(b"partial")
        self.reopen()
        self.assertEqual(os.listdir(self.archive), [])
        self.assertFalse(os.path.isdir(self.quarantine) and os.listdir(self.quarantine))

    def old_orphan(self):
        sha = self.orphan()
        self.age(os.path.join(self.archive, sha), 30)
        return sha

    def test_a_failed_stamp_does_not_cost_the_term(self):
        sha = self.old_orphan()
        with mock.patch("herald.memory.store.os.utime", side_effect=PermissionError("denied")):
            self.reopen()
        self.reopen()
        self.assertIn(sha, os.listdir(self.quarantine))

    def test_a_death_at_the_stamp_does_not_cost_the_term(self):
        sha = self.old_orphan()
        self.s.close()
        code = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.utime = lambda *a, **k: os._exit(9)\n"
            "from herald.memory import Store\n"
            "Store(sys.argv[2])\n"
            "os._exit(1)\n"
        )
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        self.assertEqual(subprocess.call([sys.executable, "-B", "-X", "utf8", "-c", code, repo, self.root]), 9)
        self.s = Store(self.root, clock=self.clock)
        self.assertIn(sha, os.listdir(self.quarantine))


if __name__ == "__main__":
    unittest.main()
