"""P61 F6 smoke test with TWO REAL PROCESSES (reviewer M-21479): both open a NEW store at the same moment; one of them
then adds and erases a record. The journal must keep that erase line. A race is not guaranteed to show in every round,
so the test runs many rounds; green here is evidence, not proof. test author, 11.11.2026."""
import multiprocessing as mp
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

ROUNDS = int(os.environ.get("P61_RACE_ROUNDS", "25"))
OP = {"id": "ENT-operator", "role": "operator"}


def _record(text, memory_id):
    from test_memory_core import rec
    return rec(text, memory_id=memory_id)


def opener(root, barrier, erase, out):
    """Open the store when the barrier falls; the erasing process then adds and erases one record."""
    try:
        from herald.memory import Store
        barrier.wait(timeout=30)
        store = Store(root)
        if erase:
            store.add(_record("race record zebra", "MEM-racerecord000001"), "x", reader_scope="main")
            store.erase("MEM-racerecord000001", "asked", OP, reader_scope="main")
        store.close()
        out.put(("ok", erase))
    except BaseException as exc:      # a refusal to open is allowed; the test looks at the journal afterwards
        out.put(("exc", erase, type(exc).__name__))


class Race(unittest.TestCase):
    def test_two_processes_opening_a_new_store_never_wipe_an_erase_line(self):
        ctx = mp.get_context("spawn")
        wiped, refused, rounds = [], 0, 0
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            for i in range(ROUNDS):
                root = os.path.join(tmp, "r%d" % i)
                barrier = ctx.Barrier(2)
                out = ctx.Queue()
                procs = [ctx.Process(target=opener, args=(root, barrier, flag, out)) for flag in (True, False)]
                for p in procs:
                    p.start()
                for p in procs:
                    p.join(60)
                results = [out.get(timeout=5) for _ in range(2)]
                rounds += 1
                refused += sum(1 for r in results if r[0] == "exc")
                erased_ok = any(r[0] == "ok" and r[1] for r in results)    # the erasing process finished its erase
                journal = root + ".erasures.jsonl"
                lines = []
                if os.path.exists(journal):
                    with open(journal, "rb") as f:
                        lines = [ln for ln in f.read().split(b"\n") if ln.strip()]
                if erased_ok and not lines:
                    wiped.append(i)
        self.assertEqual(wiped, [], "rounds where the erase line was wiped from the journal: %s of %d (refusals: %d)"
                         % (wiped, rounds, refused))


if __name__ == "__main__":
    unittest.main()
