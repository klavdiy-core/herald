"""p61 F6 smoke with real processes (reviewer M-21480): several processes open a brand-new store at the same moment, each adds
and erases its own record. The journal must end as one whole chain with one line per erasure: no opener truncated it."""
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

WORKERS = 4
OP = {"id": "ENT-operator", "role": "operator"}


def _one(root, start, index, out):
    sys.path.insert(0, os.path.join(HERE, ".."))
    sys.path.insert(0, HERE)
    from herald.memory import Store
    from test_memory_core import rec
    while time.time() < start:
        time.sleep(0.001)
    try:
        s = Store(root)
        mid = s.add(rec("fact of process %d" % index), "x")
        s.erase(mid, "asked", OP, reader_scope="main")
        s.close()
        out.put(("ok", index))
    except Exception as err:        # noqa: BLE001  reported to the parent, which fails on it
        out.put(("error", "%d: %s: %s" % (index, type(err).__name__, err)))


class Processes(unittest.TestCase):
    def test_new_store_opened_by_several_processes_at_once_keeps_every_erasure_line(self):
        from herald.memory import Store
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "mem")
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        start = time.time() + 3.0       # spawn is slow on Windows: everyone waits for the same moment
        procs = [ctx.Process(target=_one, args=(root, start, i, out)) for i in range(WORKERS)]
        for p in procs:
            p.start()
        results = [out.get(timeout=120) for _ in procs]
        for p in procs:
            p.join(60)
        errors = [r[1] for r in results if r[0] != "ok"]
        self.assertEqual(errors, [], errors)
        with open(root + ".erasures.jsonl", "rb") as f:
            lines = [l for l in f.read().split(b"\n") if l]
        self.assertEqual([json.loads(l)["seq"] for l in lines], list(range(1, WORKERS + 1)))
        s = Store(root)
        self.addCleanup(s.close)
        self.assertEqual(s.verify_journal(reader_scope="main"), [])
        self.assertTrue(os.path.exists(os.path.join(root, "erasures.born")))


if __name__ == "__main__":
    unittest.main()
