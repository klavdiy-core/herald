"""Outside review, round 3 (25.10): a Read->Edit lesson named the file path, not the action; identical corrections
from three sessions became three proposals. Red on 0.15.0."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import learning  # noqa: E402

SIG = "String to replace not found in file."


def report(fixed_by, fix_input, corrections=()):
    return {
        "repeated_errors": [{"signature": SIG, "count": 3, "tools": ["Edit"], "first": "t1", "last": "t3",
                             "sessions": ["a", "b"]}],
        "recoveries": [{"signature": SIG, "fixed_by": fixed_by, "fix_input": fix_input, "failed_tool": "Edit",
                        "failed_input": "/w/app.ts", "session": "a"}],
        "corrections": list(corrections), "typed": [], "not_lessons": [],
    }


class Wording(unittest.TestCase):
    def draft(self, fixed_by, fix_input):
        return next(p["draft"] for p in learning.propose(report(fixed_by, fix_input)) if p["kind"] == "skill")

    def test_a_read_fix_says_read_it_again(self):
        d = self.draft("Read", "/w/app.ts")
        self.assertIn("reading `/w/app.ts` again", d)
        self.assertNotIn("after `/w/app.ts` (via Read)", d)

    def test_a_shell_fix_says_running(self):
        self.assertIn("running `pip install jinja2`", self.draft("Bash", "pip install jinja2"))

    def test_a_write_fix_says_writing(self):
        self.assertIn("writing `b.py` with Write", self.draft("Write", "b.py"))


class Corrections(unittest.TestCase):
    def corr(self, ts, text):
        return {"ts": ts, "claim": "Done, all tests pass.", "correction": text}

    def test_the_same_correction_from_three_sessions_is_one_proposal(self):
        cs = [self.corr(f"2026-10-2{i}T10:00:00Z", "You did not run the tests.") for i in range(3)]
        props = [p for p in learning.propose(report("Read", "/w/app.ts", cs)) if "corrected" in p["title"]]
        self.assertEqual(len(props), 1, props)
        self.assertIn("3", props[0]["title"])

    def test_private_corrections_are_one_proposal_too(self):
        cs = [self.corr("2026-10-20T10:00:00Z", "No, wrong file."), self.corr("2026-10-21T10:00:00Z", "Not that.")]
        props = [p for p in learning.propose(report("Read", "/w/app.ts", cs), private=True) if "corrected" in p["title"]]
        self.assertEqual(len(props), 1, props)

    def test_a_private_correction_still_says_what_was_claimed(self):
        # an outside review (25.10): in private mode the lesson was only a date and "re-read that exchange", which a
        # confined turn cannot do. The model's own claim is kept; any word the human typed is masked in it.
        rep = report("Read", "/w/app.ts", [{"ts": "2026-10-20T10:00:00Z", "claim": "Done, all tests pass for Ivanova.",
                                              "correction": "No, you never ran them, Ivanova is waiting"}])
        rep["typed"] = ["never", "ran", "them", "Ivanova", "waiting"]       # the words the human typed
        props = [p for p in learning.propose(rep, private=True) if "corrected" in p["title"]]
        self.assertIn("all tests pass", props[0]["draft"])
        self.assertNotIn("Ivanova", props[0]["draft"])
        self.assertNotIn("never ran", props[0]["draft"])

    def test_different_corrections_stay_apart(self):
        cs = [self.corr("2026-10-20T10:00:00Z", "You did not run the tests."),
              self.corr("2026-10-21T10:00:00Z", "That is the wrong branch.")]
        props = [p for p in learning.propose(report("Read", "/w/app.ts", cs)) if "corrected" in p["title"]]
        self.assertEqual(len(props), 2)


if __name__ == "__main__":
    unittest.main()
