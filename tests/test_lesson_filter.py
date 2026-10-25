"""A lesson is a cause, not a neighbour (main Klavdiy's task, 25.10, after real team output: "internal went away after
cd" and "Edit could not find the string -> use PowerShell"). Each test is red on 0.10.1."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import learning  # noqa: E402
from test_learning import T, Transcript  # noqa: E402

EDIT_MISS = "<tool_use_error>String to replace not found in file.</tool_use_error>"


def coincidence(t, k=0):
    """A CDP call fails, an unrelated cd succeeds, the same call works again: the cd did not fix anything."""
    t.tool("Bash", {"command": "powershell -NoProfile -File x_cdp.ps1"}, '{"ok": false,\n"error": "internal",}',
           T(k), True)
    t.tool("Bash", {"command": "cd /c/Users/me/data/x; ls"}, "a b", T(k + 1))
    t.tool("Bash", {"command": "powershell -NoProfile -File x_cdp.ps1"}, '{"ok": true}', T(k + 2))


def bypass(t, k=0):
    """Edit's check says the text is not there; the file is rewritten by PowerShell around the check."""
    t.tool("Edit", {"file_path": "C:/w/a.py", "old_string": "x", "new_string": "y"}, EDIT_MISS, T(k), True)
    t.tool("PowerShell", {"command": "cd C:\\w; (Get-Content a.py) -replace 'x','y' | Set-Content a.py"}, "", T(k + 1))


def gate_skip(t, k=0):
    t.tool("PowerShell", {"command": "tg_text.ps1 -File r.txt"}, "GATE_BLOCKED: freshness\nExit code 4", T(k), True)
    t.tool("PowerShell", {"command": "tg_text.ps1 -File r.txt -SkipGate"}, "sent", T(k + 1))


def honest(t, k=0):
    """python -c with quotes breaks in the shell; the same code from a file works: a real lesson."""
    t.tool("PowerShell", {"command": "python -c \"print('a b\""},
           "SyntaxError: unterminated string literal (detected at line 1)", T(k), True)
    t.tool("Write", {"file_path": "C:/tmp/probe.py", "content": "print('a b')"}, "File created", T(k + 1))
    t.tool("PowerShell", {"command": "python C:/tmp/probe.py"}, "a b", T(k + 2))


def write(d, name, *builders):
    t = Transcript()
    for i, b in enumerate(builders):
        b(t, 10 * i)
    p = os.path.join(d, f"{name}.jsonl")
    os.replace(t.write(d), p)
    return p


class Filter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.paths = [write(d, "s1", coincidence, bypass, gate_skip, honest),
                      write(d, "s2", coincidence, bypass, gate_skip, honest)]
        self.report = learning.analyze_many(self.paths)
        self.props = learning.propose(self.report, min_sessions=2)
        self.text = json.dumps(self.props, ensure_ascii=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_neighbouring_step_is_a_coincidence_not_a_lesson(self):
        self.assertNotIn("cd /c/Users", self.text)
        self.assertTrue(any(n["why"] == "coincidence" and "internal" in n["signature"]
                            for n in self.report["not_lessons"]), self.report["not_lessons"])

    def test_going_around_a_check_is_a_bypass_with_the_right_move(self):
        self.assertNotIn("Set-Content", self.text)
        self.assertNotIn("SkipGate", self.text)
        why = {n["why"] for n in self.report["not_lessons"]}
        self.assertIn("bypass", why)
        gate = next(n for n in self.report["not_lessons"] if "GATE_BLOCKED" in n["signature"])
        self.assertEqual(gate["why"], "bypass")                     # passing only with the check switched off
        edit = next(n for n in self.report["not_lessons"] if "String to replace" in n["signature"])
        self.assertIn("read", edit["right_move"].lower())

    def test_the_honest_lesson_stays(self):
        self.assertIn("python C:/tmp/probe.py", self.text)
        self.assertIn("unterminated string literal", self.text)

    def test_a_lesson_needs_two_different_sessions(self):
        one = learning.propose(learning.analyze_many(self.paths[:1]), min_sessions=2)
        self.assertNotIn("probe.py", json.dumps(one))

    def test_an_instead_lesson_also_needs_two_sessions(self):
        def heredoc(t, k=0):
            t.tool("PowerShell", {"command": "cat <<EOF > b.py"}, "The '<' operator is reserved for future use.",
                   T(k), True)
            t.tool("Write", {"file_path": "b.py", "content": "x"}, "File created", T(k + 1))
        one = write(self.tmp.name, "h1", heredoc)
        two = write(self.tmp.name, "h2", heredoc)
        self.assertNotIn("Write", json.dumps(learning.propose(learning.analyze_many([one]), min_sessions=2)))
        both = learning.propose(learning.analyze_many([one, two]), min_sessions=2)
        self.assertIn("(via Write)", json.dumps(both))                 # two sessions: now it is a lesson

    def test_not_lessons_are_shown_apart(self):
        md = learning.render_md(self.props, not_lessons=self.report["not_lessons"])
        self.assertIn("## Not lessons", md)
        self.assertIn("bypass", md)

    def test_team_shows_not_lessons_even_in_private_mode(self):
        from herald import team
        proj, out = os.path.join(self.tmp.name, "proj"), os.path.join(self.tmp.name, "out")
        os.makedirs(proj)
        for p in self.paths:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "custom-title", "customTitle": "Voron"}) + "\n")
            os.replace(p, os.path.join(proj, os.path.basename(p)))
        team.run(proj, out)                                       # private by default
        with open(os.path.join(out, "Voron.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("## Not lessons", md)
        self.assertIn("**coincidence**", md)
        self.assertIn("**bypass**", md)
        self.assertIn("python C:/tmp/probe.py", md)               # the honest lesson is a proposal

    def test_another_attempt_or_a_look_is_not_a_fix(self):
        # seen on a real session: a failing script, then "sleep and run something else of the same script", and a
        # failing python -c followed by a look at the same file with another tool
        t = Transcript()
        t.tool("PowerShell", {"command": "node tools\\x_cdp.mjs ui-check"}, "Error: target closed", T(0), True)
        t.tool("PowerShell", {"command": "Start-Sleep 20; node tools\\x_cdp.mjs read-timeline"}, "ok", T(1))
        t.tool("PowerShell", {"command": "node tools\\x_cdp.mjs ui-check"}, "ok", T(2))
        t.tool("Bash", {"command": "python -X utf8 -c \"import json; json.load(open('C:/t/data.json'))\""},
               "json.decoder.JSONDecodeError: Expecting value", T(5), True)
        t.tool("Read", {"file_path": "C:/t/data.json"}, "{oops", T(6))       # another tool, only a look
        rep = learning.analyze(t.write(self.tmp.name))
        self.assertEqual(rep["recoveries"], [], rep["recoveries"])
        self.assertEqual({n["why"] for n in rep["not_lessons"]}, {"coincidence"})

    def test_appending_to_the_file_from_the_shell_is_a_bypass_too(self):
        # the real case behind main Klavdiy's example used [IO.File]::AppendAllText, not Set-Content
        t = Transcript()
        t.tool("Edit", {"file_path": "C:/w/notes.md", "old_string": "x", "new_string": "y"}, EDIT_MISS, T(0), True)
        t.tool("PowerShell", {"command": "cd C:\\w; [IO.File]::AppendAllText('notes.md', \"- line\")"}, "", T(1))
        rep = learning.analyze(t.write(self.tmp.name))
        self.assertEqual([n["why"] for n in rep["not_lessons"]], ["bypass"])

    def test_looking_at_the_file_after_an_edit_miss_is_not_a_bypass(self):
        t = Transcript()
        t.tool("Edit", {"file_path": "C:/w/a.py", "old_string": "x", "new_string": "y"}, EDIT_MISS, T(0), True)
        t.tool("Bash", {"command": "grep -n 'x' a.py"}, "3: x = 1", T(1))
        rep = learning.analyze(t.write(self.tmp.name))
        self.assertNotIn("bypass", {n["why"] for n in rep["not_lessons"]})

    def test_the_classic_fixes_are_still_causes(self):
        # pip install pyyaml after "No module named 'yaml'"; Read before a second Edit of the same file
        t = Transcript()
        for k in (0, 10):
            t.tool("Bash", {"command": "python make.py"}, "ModuleNotFoundError: No module named 'yaml'", T(k), True)
            t.tool("Bash", {"command": "pip install pyyaml"}, "ok", T(k + 1))
            t.tool("Bash", {"command": "python make.py"}, "ok", T(k + 2))
            t.tool("Edit", {"file_path": "m.py", "old_string": "a", "new_string": "b"}, EDIT_MISS, T(k + 3), True)
            t.tool("Read", {"file_path": "m.py"}, "...", T(k + 4))
            t.tool("Edit", {"file_path": "m.py", "old_string": "c", "new_string": "b"}, "ok", T(k + 5))
        rep = learning.analyze(t.write(self.tmp.name))
        fixes = {r["fixed_by"] + ":" + r["fix_input"] for r in rep["recoveries"]}
        self.assertTrue(any("pip install pyyaml" in f for f in fixes), fixes)
        self.assertTrue(any(f.startswith("Read:") for f in fixes), fixes)


if __name__ == "__main__":
    unittest.main()
