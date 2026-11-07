"""Power-cut branch 'TASK STORE LOST': tasks.json unreadable and no tasks.json.prev -> an empty store and a loud LOST line in audit.log;
and the control: with a readable .prev the store rises and the line says RECOVERED."""
import os
import unittest

import test_forum_tasks as T


class Lost(T.Base):
    def setUp(self):
        super().setUp()
        self.make()
        self.a = self.new_task("topic a")
        self.path = os.path.join(self.control, "tasks.json")

    def audit(self):
        p = os.path.join(self.control, "audit.log")
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_L1_unreadable_store_and_no_prev_gives_empty_and_a_loud_line(self):
        for p in (self.path + ".prev",):
            if os.path.exists(p):
                os.remove(p)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{cut")
        self.make()
        self.assertEqual(self.h.forum._tasks()["tasks"], {})
        self.assertIn("TASK STORE LOST", self.audit())
        self.assertNotIn("RECOVERED", self.audit())

    def test_L2_control_a_readable_prev_rises_and_says_recovered(self):
        self.new_task("topic b")              # second save: .prev now holds the generation with topic a
        with open(self.path, "w", encoding="utf-8"):
            pass
        self.make()
        self.assertIn(str(self.a), self.h.forum._tasks()["tasks"])
        self.assertIn("TASK STORE RECOVERED", self.audit())
        self.assertNotIn("TASK STORE LOST", self.audit())

    def test_L3_a_broken_prev_is_not_used(self):
        self.new_task("topic b")
        with open(self.path + ".prev", "w", encoding="utf-8") as f:
            f.write("{cut")
        with open(self.path, "w", encoding="utf-8"):
            pass
        self.make()
        self.assertEqual(self.h.forum._tasks()["tasks"], {})
        self.assertIn("TASK STORE LOST", self.audit())


if __name__ == "__main__":
    unittest.main(verbosity=1)
