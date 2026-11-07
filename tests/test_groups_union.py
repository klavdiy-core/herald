import json
import os
import tempfile
import unittest

from herald import groups


class UnionSchema(unittest.TestCase):
    def load(self, obj):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "groups.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(obj, f)
            return groups.load_groups(p)

    def test_members_and_bots_load_and_are_kept(self):
        her, out = self.load({"her_id": 5, "groups": [
            {"chat_id": -1001000000002, "owners": [11], "peer_bots": [900], "members": [21, 22], "bots": [901]}]})
        self.assertEqual(her, 5)
        self.assertEqual(out[0]["members"], [21, 22])
        self.assertEqual(out[0]["bots"], [901])

    def test_members_and_bots_default_to_empty(self):
        _, out = self.load({"groups": [{"chat_id": -1001000000002, "owners": [11]}]})
        self.assertEqual((out[0]["members"], out[0]["bots"]), ([], []))

    def test_members_must_be_numeric_ids(self):
        with self.assertRaises(ValueError):
            self.load({"groups": [{"chat_id": -1001000000002, "members": ["x"]}]})

    def test_unknown_key_still_refused(self):
        with self.assertRaises(ValueError):
            self.load({"groups": [{"chat_id": -1001000000002, "stranger": 1}]})


if __name__ == "__main__":
    unittest.main()
