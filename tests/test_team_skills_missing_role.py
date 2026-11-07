"""A role without roles/<Role>.md must produce a WARNING, not silent emptiness.
for_role() used to return ([], []) for it ("simply has no team skills"), and setup() logs only the warnings it gets, so a worker with a
mistyped role started with NO team skills and nothing in any log said why.
Contract: (1) for_role of a role with no file -> a warning that names the role and `roles/<Role>.md`;
(2) the same warning reaches setup()'s log line `team skills: ...`; (3) a missing repo gives a warning too;
controls: a role WITH a file gets no such warning; a bad role name keeps its own warning; setup without repo/role still returns "" quietly.
Known limit: the mutant «remove the whole missing-file branch» goes red as an ERROR, not as a clean assertion failure - it is caught,
but not for the stated reason. Mutants that keep the branch and weaken it (no role name, always warns) fail by assertion.
Run from the repo root: python -X utf8 -m unittest tests.test_team_skills_missing_role"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from herald import team_skills  # noqa: E402


def make_repo(d):
    r = os.path.join(d, "team-skills")
    os.makedirs(os.path.join(r, "roles"))
    os.makedirs(os.path.join(r, "skills", "edit-fresh-read"))
    with open(os.path.join(r, "skills", "edit-fresh-read", "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: edit-fresh-read\ndescription: Read again before editing.\n---\n\nbody\n")
    with open(os.path.join(r, "roles", "Writer.md"), "w", encoding="utf-8") as f:
        f.write("# Writer\n\n- `edit-fresh-read` - why\n")
    return r


class MissingRole(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_role_file_warns_and_names_role_and_file(self):
        skills, warnings = team_skills.for_role(self.repo, "Nobody")
        self.assertEqual(skills, [])
        joined = " | ".join(warnings)
        self.assertTrue(warnings, "silent empty: no warning for a role without a file")
        self.assertIn("Nobody", joined)
        self.assertIn("roles/Nobody.md", joined.replace("\\", "/"))

    def test_the_warning_reaches_the_setup_log(self):
        lines = []
        ws = os.path.join(self.tmp.name, "ws")
        os.makedirs(ws)
        # patch only for this call and restore it: a permanent patch broke a neighbouring test when this file ran first
        with mock.patch.object(team_skills, "pull", lambda repo: (True, "")):       # no git in the fixture
            toc = team_skills.setup(self.repo, "Nobody", ws, log=lines.append)
        self.assertEqual(toc, "")
        self.assertTrue(any(l.startswith("team skills:") and "Nobody" in l for l in lines), "log lines: %r" % (lines,))

    def test_missing_repo_warns_too(self):
        _, warnings = team_skills.for_role(os.path.join(self.tmp.name, "no-repo"), "Writer")
        self.assertTrue(warnings, "a role looked up in a repo that does not exist gave no warning")

    # --- controls: green must not mean 'always warns' ---
    def test_control_role_with_file_has_no_missing_file_warning(self):
        skills, warnings = team_skills.for_role(self.repo, "Writer")
        self.assertEqual([s["name"] for s in skills], ["edit-fresh-read"])
        self.assertFalse(any("roles/Writer.md" in w.replace("\\", "/") for w in warnings), warnings)
        self.assertEqual(warnings, [])

    def test_control_bad_role_name_keeps_its_own_warning(self):
        _, warnings = team_skills.for_role(self.repo, "../README")
        self.assertTrue(any("not a plain name" in w for w in warnings), warnings)

    def test_control_setup_without_repo_or_role_is_quiet(self):
        lines = []
        self.assertEqual(team_skills.setup("", "Writer", self.tmp.name, log=lines.append), "")
        self.assertEqual(team_skills.setup(self.repo, "", self.tmp.name, log=lines.append), "")
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
