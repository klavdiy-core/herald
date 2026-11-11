"""P61 negative control: a restored copy with NO erasures after it must reapply nothing.
Green on 42da2cd by design (nothing reapplies there); it must stay green on the real code and go RED under M5
(greedy reapply of everything in the journal without the anchor check). test author, 11.11.2026."""
import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.memory import Store  # noqa: E402
from test_memory_core import Base, rec  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}


def view(store):
    """Everything main can see, serialised: records, events (the order matters)."""
    ids = sorted(store.all_events(reader_scope="main"), key=repr)
    return json.dumps(
        {"events": store.all_events(reader_scope="main"), "n": len(ids)}, sort_keys=True, default=repr
    )


class Control(Base):
    def test_copy_with_no_erasure_after_it_reapplies_nothing(self):
        a = self.s.add(rec("first kept record"), "a")
        b = self.s.add(rec("second kept record", predicate="likes"), "b")
        gone = self.s.add(rec("erased before the copy"), "g")
        self.s.erase(gone, "asked", OP, **MAIN)
        self.s.close()
        snap = os.path.join(self.tmp.name, "control_copy")
        shutil.copytree(self.root, snap)
        self.s = Store(self.root, clock=self.clock)
        self.s.add(rec("added after the copy", predicate="owns"), "c")  # no erase after the copy
        self.s.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = os.path.join(self.root, "memory.sqlite" + suffix)
            if os.path.exists(p):
                os.remove(p)
        shutil.copy2(os.path.join(snap, "memory.sqlite"), os.path.join(self.root, "memory.sqlite"))
        self.s = Store(self.root, clock=self.clock)
        after_open = view(self.s)
        self.assertEqual(getattr(self.s, "reapplied", 0), 0, "nothing was erased after the copy: nothing to reapply")
        self.assertNotIn("erase_reapplied", after_open)
        for kept in (a, b):
            self.assertIsNotNone(self.s.get(kept, **MAIN), "a kept record must stay")
        self.assertIsNone(self.s.get(gone, **MAIN), "the record erased BEFORE the copy stays erased")
        # byte-for-byte: a second open changes nothing either
        self.s.close()
        self.s = Store(self.root, clock=self.clock)
        self.assertEqual(view(self.s), after_open)


if __name__ == "__main__":
    unittest.main()
