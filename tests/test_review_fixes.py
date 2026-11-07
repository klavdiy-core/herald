"""Review fixes for several groups: red first, each one for its own reason.
5: a group's turn is routed by the group's own settings, not by the private chat's /model
6: the lead's /go in a group is /resume, not a paid turn
7: a group without owners is refused when the file is read"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_herald_multigroup as mg  # noqa: E402
from herald import groups  # noqa: E402

SMALL = "claude-haiku-5-5"
BIG = "claude-opus-5-5"


class SimpleJudge:
    def simple(self, prompt):
        return 0.99


class Item7(unittest.TestCase):
    def test_group_without_owners_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"groups": [{"chat_id": -1001000000002, "owners": []}]}, f)
            with self.assertRaises(ValueError) as cm:
                groups.load_groups(p)
            self.assertIn("owners", str(cm.exception))

    def test_group_with_owners_still_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"groups": [{"chat_id": -1001000000002, "owners": [11]}]}, f)
            self.assertEqual(groups.load_groups(p)[1][0]["owners"], [11])


class Routed(mg.World):
    def make(self):
        super().make()
        self.h.jev_judge, self.h.route_simple = SimpleJudge(), SMALL
        return self.h

    def test_item5_group_is_routed_even_when_private_chat_has_a_model(self):
        self.h._save(model=BIG)                              # the private chat's /model
        a = self.task(mg.GA, "alpha")
        self.lead(mg.GA, "work", a)
        self.assertEqual(self.engine.calls[-1]["model"], SMALL,
                         "item 5: the private chat's /model switched off the group's routing")

    def test_item5_group_own_model_still_wins(self):
        a = self.task(mg.GA, "alpha")
        self.lead(mg.GA, "/model " + BIG)
        self.lead(mg.GA, "work", a)
        self.assertEqual(self.engine.calls[-1]["model"], BIG, "item 5 control: the group's own /model lost to routing")


class Item6(mg.World):
    def setUp(self):
        super().setUp()
        self.a = self.task(mg.GA, "alpha")
        self.engine.calls.clear()

    def test_go_alone_is_not_a_turn(self):
        self.lead(mg.GA, "/go", self.a)
        self.assertEqual(self.turns(), 0, "item 6: the lead's /go became a paid turn")

    def test_go_lifts_the_leads_own_stop(self):
        self.lead(mg.GA, "/stop", self.a)
        self.lead(mg.GA, "/go", self.a)
        self.assertEqual(self.turns(), 0, "item 6: /go started a turn")
        self.lead(mg.GA, "work", self.a)
        self.assertEqual(self.turns(), 1, "item 6: /go did not lift the lead's stop like /resume")


if __name__ == "__main__":
    unittest.main()
