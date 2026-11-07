"""tasks.json that cannot be moved to .bad must not be lost, and must not be silent.
forum.py _tasks(): a broken tasks.json is moved to tasks.json.bad; `except OSError: pass` swallows a failed os.replace, returns the EMPTY store,
and the next _put() writes the new store over the broken (still unmoved) file -> every task of every topic is gone, no log line about it.
The lock is made REAL, not mocked: a non-empty DIRECTORY named tasks.json.bad (os.replace of a file onto it fails with OSError on Windows).
Contract after the failure:
  L1  the broken file's exact bytes still exist somewhere in control/ after the next `/task` (kept under another name, or the task creation refused and the file untouched)
  L2  control/audit.log has a LOUD line: it names tasks.json and says it could not keep/move it (cannot|could not|failed|not kept)
  L3  control: the ordinary case (no lock) still moves the broken file to .bad (so green is not 'never touches the file')
Run: python -X utf8 -m unittest tests.test_forum_bad_lock"""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_forum_tasks as T  # noqa: E402

BROKEN = '{"tasks": {"42": {"name": "precious", "state": "open"'    # cut off: invalid JSON, recognisable
LOUD = re.compile(r"cannot|could not|failed|not kept|unable", re.I)


class BadLock(T.Base):
    def setUp(self):
        super().setUp()
        self.make()
        self.path = os.path.join(self.control, "tasks.json")

    def _audit(self):
        p = os.path.join(self.control, "audit.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def _lock(self):
        os.makedirs(os.path.join(self.path + ".bad", "keep"))     # a non-empty directory: replace() onto it raises

    def _bytes_somewhere(self):
        for dp, ds, fs in os.walk(self.control):
            for f in fs:
                try:
                    if BROKEN in open(os.path.join(dp, f), encoding="utf-8", errors="replace").read():
                        return os.path.join(dp, f)
                except OSError:
                    pass
        return None

    def _fail_scene(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(BROKEN)
        self._lock()
        self.owner(0, "/task fresh")                       # reads the store (fails to move it), then writes a new one

    def test_L1_a_locked_bad_does_not_lose_the_file(self):
        self._fail_scene()
        self.assertIsNotNone(self._bytes_somewhere(), "L1: the broken tasks.json bytes are gone from control/ (overwritten)")

    def test_L2_a_locked_bad_is_said_loudly_in_audit_log(self):
        self._fail_scene()
        log = self._audit()
        loud = [l for l in log.splitlines() if "tasks.json" in l and LOUD.search(l)]
        self.assertTrue(loud, "L2: no audit.log line says tasks.json could not be kept; log: %r" % log[-300:])

    def test_L3_control_without_a_lock_the_broken_file_goes_to_bad(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(BROKEN)
        self.owner(0, "/tasks")
        self.assertTrue(os.path.isfile(self.path + ".bad"), "no lock: the broken file must be moved to .bad")
        with open(self.path + ".bad", encoding="utf-8") as f:
            self.assertEqual(f.read(), BROKEN)


if __name__ == "__main__":
    unittest.main(verbosity=1)
