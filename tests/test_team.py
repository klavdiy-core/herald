import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import team  # noqa: E402
from test_learning import T, Transcript, planted  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EDIT_MISS = "<tool_use_error>String to replace not found in file.</tool_use_error>"


def edit_misses(t, start):
    for k in range(2):
        t.tool("Edit", {"file_path": f"f{start}{k}.py", "old_string": "a", "new_string": "b"}, EDIT_MISS, T(start + k), True)


def session(d, name, title, t):
    p = t.write(d)
    dst = os.path.join(d, f"{name}.jsonl")
    os.replace(p, dst)
    if title:   # Claude Code writes the session name as its own record
        with open(dst, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": name}) + "\n")
    return dst


class Team(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.d)
        a = planted()
        edit_misses(a, 50)
        session(self.d, "s1", "Builder", a)
        b = Transcript()
        b.user("post it", T(0))
        edit_misses(b, 10)
        session(self.d, "s2", "Writer", b)
        c = Transcript()
        c.user("again", T(0))
        edit_misses(c, 20)
        session(self.d, "s3", "Builder", c)                  # a second session of the same role
        session(self.d, "s4", None, Transcript())            # unnamed sessions are skipped
        self.out = os.path.join(self.tmp.name, "shared")
        self.res = team.run(self.d, self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read()

    def test_one_file_per_role_and_an_index(self):
        self.assertEqual(sorted(os.listdir(self.out)), ["Builder.md", "INDEX.md", "Writer.md"])

    def test_sessions_of_one_role_are_counted_together(self):
        self.assertEqual(self.res["roles"]["Builder"]["sessions"], 2)
        builder = self.read("Builder.md")
        self.assertIn("String to replace not found", builder)    # in both Builder sessions: a lesson
        self.assertNotIn("yaml", builder)                         # in one session only: not yet

    def test_an_error_two_roles_share_is_a_team_lesson(self):
        idx = self.read("INDEX.md")
        self.assertIn("String to replace not found", idx)
        self.assertIn("Builder", idx)
        self.assertIn("Writer", idx)
        self.assertNotIn("yaml", idx.split("## Shared")[1])    # only Builder had yaml

    def test_private_is_the_default(self):
        self.assertNotIn("PowerShell 5.1 ломает", self.read("Builder.md"))

    def test_since_skips_old_sessions(self):
        old = time.time() - 3 * 86400
        os.utime(os.path.join(self.d, "s2.jsonl"), (old, old))
        res = team.run(self.d, os.path.join(self.tmp.name, "shared2"), since_hours=24)
        self.assertNotIn("Writer", res["roles"])

    def test_private_never_carries_an_output_line_that_is_not_an_error(self):
        # a tool that exits 1 while printing someone's message (seen live: an inbox reader) must not leak it
        d = os.path.join(self.tmp.name, "leak")
        os.makedirs(d)
        t = Transcript()
        for k in range(3):
            t.tool("Bash", {"command": "python read_inbox.py"}, "meet me at the old bridge at nine\nExit code 1",
                   T(k), True)
        session(d, "s9", "Reader", t)
        out = os.path.join(self.tmp.name, "leak_out")
        team.run(d, out)
        for name in os.listdir(out):
            with open(os.path.join(out, name), encoding="utf-8") as f:
                self.assertNotIn("old bridge", f.read(), name)

    def test_an_error_once_in_each_of_two_sessions_of_a_role_is_a_repeat(self):
        d = os.path.join(self.tmp.name, "two")
        os.makedirs(d)
        for n in ("a", "b"):
            t = Transcript()
            t.tool("Bash", {"command": "make"}, "make: *** No rule to make target 'all'. Stop. (error 2)", T(1), True)
            session(d, n, "Maker", t)
        out = os.path.join(self.tmp.name, "two_out")
        team.run(d, out)
        with open(os.path.join(out, "Maker.md"), encoding="utf-8") as f:
            self.assertIn("No rule to make target", f.read())

    def test_cli(self):
        out = os.path.join(self.tmp.name, "cli")
        r = subprocess.run([sys.executable, "-m", "herald", "team", self.d, "--out", out],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(out, "INDEX.md")))


if __name__ == "__main__":
    unittest.main()
