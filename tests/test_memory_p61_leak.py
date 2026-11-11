"""P61 R9: a worker must not learn that a PRIVATE record was erased again (no counter, no event, no ledger row).
Guard test: green on 42da2cd (nothing reapplies there) and on the real code; proven RED by mutants that leak
(p61_matrix.py M6 counter in verify(worker), M7 worker-visible erase_reapplied event). test author, 11.11.2026."""
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
OP = {"id": "ENT-operator", "role": "operator"}
PUBLIC_ID = "MEM-r9publicrec0001"
PRIVATE_ID = "MEM-r9privaterec001"
PUBLIC_TEXT = "the public record text kiwi"
PRIVATE_TEXT = "the private record text mango"


def worker_view(store):
    """Everything a worker can ask, serialised; the private id is asked about too."""
    w = {"reader_scope": "worker"}
    view = {
        "get_public": store.get(PUBLIC_ID, **w),
        "history_public": store.history(PUBLIC_ID, **w),
        "events_public": store.events(PUBLIC_ID, **w),
        "search_public": store.search("kiwi", **w),
        "search_private": store.search("mango", **w),
        "verify": store.verify(**w),
        "ledger_public": store.erasure_ledger(PUBLIC_ID, **w),
        "get_private": store.get(PRIVATE_ID, **w),
        "events_private": store.events(PRIVATE_ID, **w),
        "ledger_private": store.erasure_ledger(PRIVATE_ID, **w),
    }
    return json.dumps(view, sort_keys=True, default=repr)


class Leak(unittest.TestCase):
    def test_r9_worker_answers_are_the_same_with_and_without_a_private_record_erased_again(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root_a = os.path.join(tmp.name, "a")
        clock_a = Clock()
        a = Store(root_a, clock=clock_a)
        a.add(rec(PUBLIC_TEXT, memory_id=PUBLIC_ID), "public", **MAIN)
        # the baseline is the SAME store before any private record existed: two stores have two salts, so their keyed
        # hashes differ by construction and could not be compared byte for byte
        before = worker_view(a)
        a.add(rec(PRIVATE_TEXT, memory_id=PRIVATE_ID, sensitivity="private", predicate="likes"), "private", **MAIN)
        a.close()
        snap = os.path.join(tmp.name, "a_copy")
        shutil.copytree(root_a, snap)                       # the copy holds the private record
        a = Store(root_a, clock=clock_a)
        a.erase(PRIVATE_ID, "asked", OP, **MAIN)
        a.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(root_a, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(root_a, "memory.sqlite"))
        a = Store(root_a, clock=clock_a)                    # this open erases the private record AGAIN
        self.addCleanup(lambda: getattr(a, "close", lambda: None)())
        self.assertIsNone(a.get(PRIVATE_ID, **MAIN), "stand: the private record must really be gone for main")
        self.assertEqual(worker_view(a), before, "a worker sees a trace of the private erase")


class LeakThroughTheChain(unittest.TestCase):
    def test_r9b_the_worker_chain_has_no_gap_after_a_private_record_is_erased_again(self):
        """R9b (author M-21448): a reapply event of a PRIVATE record must not enter the worker's hash chain. The door is
        the numbering: the next worker event would get n+2 and a prev_hash of an event the worker never saw.
        Kills M7. Green on 42da2cd (nothing reapplies) but only after the stand line, which is red there."""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "a")
        clock = Clock()
        s = Store(root, clock=clock)
        mid_a = s.add(rec("worker record A kiwi"), "a")                       # worker scope (internal label)
        s.add(rec(PRIVATE_TEXT, memory_id=PRIVATE_ID, sensitivity="private", predicate="likes"), "p", **MAIN)
        s.close()
        snap = os.path.join(tmp.name, "copy")
        shutil.copytree(root, snap)
        s = Store(root, clock=clock)
        s.erase(PRIVATE_ID, "asked", OP, **MAIN)
        s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(root, "memory.sqlite"))
        s = Store(root, clock=clock)                                           # reapplies the private erase
        self.addCleanup(lambda: getattr(s, "close", lambda: None)())
        self.assertIsNone(s.get(PRIVATE_ID, **MAIN), "stand: the private record must be gone again for main")
        last_a = s.events(mid_a, reader_scope="worker")[-1]
        mid_b = s.add(rec("worker record B kiwi two", predicate="owns"), "b")
        first_b = s.events(mid_b, reader_scope="worker")[0]
        self.assertEqual(first_b["n"], last_a["n"] + 1, "a hole in the worker chain: a private event took a number")
        self.assertEqual(first_b["prev_hash"], last_a["hash"], "prev_hash points at an event the worker never saw")


if __name__ == "__main__":
    unittest.main()
