# Extra control to test_forum_bad_lock: PermissionError on READ.
# A valid tasks.json that cannot be opened (another process holds it) AND cannot be moved: it must not be overwritten by a fresh store,
# the loud line must be in audit.log, and the bot must not crash the whole update on the refusal.
import builtins, os, re, sys, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_forum_tasks as T  # noqa: E402

GOOD = '{"tasks": {"42": {"name": "precious", "state": "open"}}, "done_topic": null}'
LOUD = re.compile(r"cannot|could not|failed|not kept|unable|not writing", re.I)


class ReadDenied(T.Base):
    def setUp(self):
        super().setUp()
        self.make()
        self.path = os.path.join(self.control, "tasks.json")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(GOOD)

    def _run_denied(self):
        real_open, real_replace = builtins.open, os.replace
        base = os.path.basename(self.path)

        def deny_open(p, *a, **k):
            if isinstance(p, str) and os.path.basename(p) == base:
                raise PermissionError(13, "denied", p)
            return real_open(p, *a, **k)

        def deny_replace(s, d, *a, **k):
            if isinstance(s, str) and os.path.basename(s).startswith(base) and not s.endswith(".tmp"):
                raise PermissionError(13, "denied", s)
            return real_replace(s, d, *a, **k)

        with mock.patch.object(builtins, "open", deny_open), mock.patch.object(os, "replace", deny_replace):
            try:
                self.owner(0, "/task fresh")
            except OSError:                       # the designed refusal of _put; run.py catches Exception per message (lines 450, 893)
                pass

    def test_unreadable_unmovable_store_is_not_overwritten(self):
        self._run_denied()
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), GOOD, "the denied tasks.json was overwritten by a fresh store")

    def test_unreadable_unmovable_store_is_said_loudly(self):
        self._run_denied()
        p = os.path.join(self.control, "audit.log")
        log = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        self.assertTrue([l for l in log.splitlines() if "tasks.json" in l and LOUD.search(l)], "no loud tasks.json line: %r" % log[-300:])


if __name__ == "__main__":
    unittest.main()
