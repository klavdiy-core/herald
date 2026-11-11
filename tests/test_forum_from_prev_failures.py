"""forum._from_prev when tasks.json is broken and tasks.json.prev is there, but something else fails:
(1) .prev is readable, writing it back fails (disk full) -> the recovered tasks are served from memory, audit.log says
    'could not be restored', never RECOVERED, never LOST (kills mutant restore_failure_returns_none);
(2) .prev cannot be opened (held by another process) -> the honest 'could not be opened' line and no false
    'TASK STORE LOST' line, a person reads audit.log to decide what was lost (kills mutant oserror_falls_through).
Tests written by the reviewer (test author), taken in unchanged except the import path. Run from tests/ like test_forum_store_lost."""
import json
import os
import tempfile
import unittest
from unittest import mock

import test_forum_tasks as T
from herald import forum


class RestoreFails(T.Base):
    def setUp(self):
        super().setUp()
        self.make()
        self.a = self.new_task("topic a")
        self.path = os.path.join(self.control, "tasks.json")
        self.new_task("topic b")                      # .prev now holds a generation with topic a

    def audit(self):
        p = os.path.join(self.control, "audit.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def test_recovered_tasks_are_served_when_the_restore_write_fails(self):
        with open(self.path, "w", encoding="utf-8"):
            pass                                      # tasks.json empty: a power cut
        forum_cls = type(self.make().forum)
        with mock.patch.object(forum_cls, "_put", side_effect=OSError("disk full")):
            tasks = self.h.forum._tasks()["tasks"]
        self.assertIn(str(self.a), tasks)             # the readable generation is served, not an empty store
        self.assertIn("could not be restored", self.audit())
        self.assertNotIn("TASK STORE RECOVERED", self.audit())
        self.assertNotIn("TASK STORE LOST", self.audit())


class HeldPrevIsNotLost(unittest.TestCase):
    def forum_at(self):
        control = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(control, ignore_errors=True))
        f = object.__new__(forum.Forum)
        f.control, f.path = control, os.path.join(control, "tasks.json")
        f.h = mock.MagicMock()
        return f

    def lines(self, f):
        return [str(c.args[0]) for c in f.h._log.call_args_list]

    def test_held_prev_logs_the_honest_line_and_no_lost_line(self):
        f = self.forum_at()
        with open(f.path + ".prev", "w", encoding="utf-8") as out:
            json.dump({"tasks": {"7": {"name": "a"}}, "done_topic": None}, out)
        with open(f.path, "w", encoding="utf-8") as out:
            out.write("{torn")
        real = open

        def held(path, *a, **k):
            if str(path) == f.path + ".prev":
                raise PermissionError(13, "held by another process")
            return real(path, *a, **k)

        with mock.patch("builtins.open", held):
            self.assertEqual(f._tasks()["tasks"], {})
        log = self.lines(f)
        self.assertTrue(any("could not be opened" in x for x in log), log)
        self.assertFalse(any("TASK STORE LOST" in x for x in log), log)


if __name__ == "__main__":
    unittest.main()
