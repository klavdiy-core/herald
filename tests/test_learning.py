import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import learning  # noqa: E402


def T(n, h=10):
    return f"2026-10-24T{h:02d}:{n:02d}:00Z"


class Transcript:
    """Writes a Claude Code-shaped transcript."""

    def __init__(self):
        self.recs, self.n = [], 0

    def user(self, text, ts):
        self.recs.append({"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}})

    def say(self, text, ts):
        self.recs.append({"type": "assistant", "timestamp": ts, "message": {"role": "assistant",
                                                                             "content": [{"type": "text", "text": text}]}})

    def tool(self, name, inp, out, ts, error=False):
        self.n += 1
        tid = f"t{self.n}"
        self.recs.append({"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        block = {"type": "tool_result", "tool_use_id": tid, "content": out}
        if error:
            block["is_error"] = True
        self.recs.append({"type": "user", "timestamp": ts, "message": {"role": "user", "content": [block]}})

    def write(self, d):
        p = os.path.join(d, "s.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in self.recs) + "\n")
        return p


def planted():
    t = Transcript()
    t.user("build the report", T(0))
    # the same mistake, three times, with different line numbers and paths
    t.tool("Bash", {"command": "python make.py"}, "Traceback (most recent call last):\n  File \"C:\\a\\make.py\", line 3\nModuleNotFoundError: No module named 'yaml'", T(1), True)
    t.tool("Bash", {"command": "pip install pyyaml"}, "Successfully installed pyyaml-6.0", T(2))
    t.tool("Bash", {"command": "python make.py"}, "ok", T(3))
    t.tool("Bash", {"command": "python other.py"}, "Traceback (most recent call last):\n  File \"C:\\b\\other.py\", line 9\nModuleNotFoundError: No module named 'yaml'", T(10), True)
    t.tool("Bash", {"command": "python tool3.py"}, "ModuleNotFoundError: No module named 'yaml'", T(20), True)
    # a one-off error must not become a proposal
    t.tool("Read", {"file_path": "x.md"}, "File does not exist.", T(25), True)
    # a fix found after failures: PowerShell heredoc fails, Write tool works
    t.tool("PowerShell", {"command": "cat <<EOF > a.py"}, "The '<' operator is reserved for future use.", T(30), True)
    t.tool("PowerShell", {"command": "cat <<EOF > b.py"}, "The '<' operator is reserved for future use.", T(31), True)
    t.tool("Write", {"file_path": "b.py", "content": "x"}, "File created", T(32))
    # the human corrects the assistant
    t.say("Done, the script is saved with UTF-8.", T(40))
    t.user("нет, опять без BOM — PowerShell 5.1 ломает кириллицу", T(41))
    t.say("Fixed: saved with a BOM.", T(42))
    t.user("thanks", T(43))
    return t


def realistic():
    """What a real session looks like (found by running on a real transcript, 24.10): intentional red test runs,
    skill text injected as a user record, long instructions containing 'again', unrelated next commands."""
    t = Transcript()
    t.user("fix the parser", T(0))
    for i in range(4):   # TDD: red runs on purpose, they are not mistakes
        t.tool("PowerShell", {"command": "python -m unittest", "description": "Red run for the new test"},
               "FAILED (errors=1)\nExit code 1", T(1 + i), True)
    t.recs.append({"type": "user", "isMeta": True, "timestamp": T(6),
                   "message": {"role": "user", "content": "# /loop\nRun the prompt again. Stop when done. You forgot nothing."}})
    t.tool("PowerShell", {"command": "git status"}, "Exit code 1\nfatal: not a git repository", T(7), True)
    t.tool("PowerShell", {"command": "ls"}, "a.py", T(8))                       # unrelated: not a fix
    # Edit fails, Read, Edit works: the lesson is "read before editing"
    for k, m in ((10, 10), (20, 20), (30, 30)):
        t.tool("Edit", {"file_path": f"src/m{k}.py", "old_string": "x", "new_string": "y"},
               "<tool_use_error>String to replace not found in file.</tool_use_error>", T(m), True)
        t.tool("Read", {"file_path": f"src/m{k}.py"}, "...", T(m + 1))
        t.tool("Edit", {"file_path": f"src/m{k}.py", "old_string": "x2", "new_string": "y"}, "ok", T(m + 2))
    long = "Here is the plan again. " * 30 + "No changes to the API."
    t.say("Plan written.", T(40))
    t.user(long, T(41))
    # ordinary development: a test fails, the code is edited, the test passes. That is the job, not a lesson.
    t.tool("PowerShell", {"command": "python -m pytest -q"}, "1 failed\nAssertionError: 2 != 3", T(45), True)
    t.tool("Edit", {"file_path": "src/calc.py", "old_string": "a", "new_string": "b"}, "ok", T(46))
    t.tool("PowerShell", {"command": "python -m pytest -q"}, "3 passed", T(47))
    # a real correction worded as a plain observation
    t.say("The agents are running in the background.", T(50))
    t.user("тесты не запустились", T(51))
    # a message sent mid-turn is stored by Claude Code as an attachment, not as a user record (seen on a real session)
    t.say("Published, the page is live.", T(55))
    t.recs.append({"type": "attachment", "timestamp": T(56),
                   "attachment": {"type": "queued_command", "prompt": "страница не открылась"}})
    # a system notification arrives the same way and is not a human
    t.say("Waiting for the agents.", T(58))
    t.recs.append({"type": "attachment", "timestamp": T(59), "attachment": {"type": "queued_command",
                   "prompt": "<task-notification> agent did not run again </task-notification>"}})
    return t


class Realistic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.report = learning.analyze(realistic().write(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_intentional_red_runs_are_not_mistakes(self):
        self.assertFalse(any("FAILED" in g["signature"] for g in self.report["repeated_errors"]))

    def test_exit_code_lines_are_not_the_signature(self):
        self.assertFalse(any(g["signature"].startswith("Exit code") for g in self.report["repeated_errors"]))

    def test_injected_skill_text_and_long_instructions_are_not_corrections(self):
        self.assertEqual([c["claim"] for c in self.report["corrections"]],
                         ["The agents are running in the background.", "Published, the page is live."])

    def test_a_mid_turn_message_is_heard(self):
        self.assertIn("не открылась", self.report["corrections"][-1]["correction"])

    def test_a_plain_it_did_not_run_is_a_correction(self):
        self.assertIn("не запустились", self.report["corrections"][0]["correction"])

    def test_a_failing_test_fixed_by_editing_code_is_the_job_not_a_lesson(self):
        self.assertFalse(any("pytest" in r["failed_input"] for r in self.report["recoveries"]), self.report["recoveries"])

    def test_read_before_edit_is_learned(self):
        rep = [g for g in self.report["repeated_errors"] if "String to replace not found" in g["signature"]]
        self.assertEqual(rep[0]["count"], 3)
        fix = [r for r in self.report["recoveries"] if "String to replace" in r["signature"]]
        self.assertEqual(fix[0]["fixed_by"], "Read")

    def test_editing_the_code_between_a_failing_and_a_passing_run_is_the_job(self):
        t = Transcript()
        for k in (0, 10):
            t.tool("Bash", {"command": "python build.py"}, "build error: missing field", T(k), True)
            t.tool("Edit", {"file_path": "build.py", "old_string": "a", "new_string": "b"}, "ok", T(k + 1))
            t.tool("Bash", {"command": "python build.py"}, "built", T(k + 2))
        with tempfile.TemporaryDirectory() as d:
            props = learning.propose(learning.analyze(t.write(d)), private=True)
        self.assertFalse(any("instead of Bash" in p["title"] for p in props), props)
        self.assertFalse(any("build.py" in p["draft"] and p["kind"] == "skill" for p in props), props)

    def test_editing_a_file_after_its_run_failed_is_not_instead(self):
        t = Transcript()
        t.tool("PowerShell", {"command": "python a.py 2>&1 | Select -Last 3"}, "NameError: name 'x' is not defined",
               T(0), True)
        t.tool("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "ok", T(1))
        with tempfile.TemporaryDirectory() as d:
            rep = learning.analyze(t.write(d))
        self.assertEqual(rep["recoveries"], [])

    def test_private_drops_withheld_outputs_from_skills(self):
        t = Transcript()
        for k in (0, 10):
            t.tool("Bash", {"command": "python inbox.py"}, "see you at nine", T(k), True)
            t.tool("Read", {"file_path": "inbox.py"}, "...", T(k + 1))
            t.tool("Bash", {"command": "python inbox.py"}, "ok", T(k + 2))
        with tempfile.TemporaryDirectory() as d:
            props = learning.propose(learning.analyze(t.write(d)), private=True)
        self.assertFalse(any("without an error line" in p["draft"] for p in props if p["kind"] == "skill"), props)

    def test_an_unrelated_next_command_is_not_a_fix(self):
        self.assertFalse(any(r["fix_input"] == "ls" for r in self.report["recoveries"]))


class Learning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = planted().write(self.tmp.name)
        self.report = learning.analyze(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_repeated_error_is_found_across_different_paths_and_lines(self):
        rep = [r for r in self.report["repeated_errors"] if "yaml" in r["signature"]]
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["count"], 3)

    def test_a_one_off_error_is_not_a_pattern(self):
        self.assertFalse(any("does not exist" in r["signature"] for r in self.report["repeated_errors"]))

    def test_a_fix_after_failures_is_found(self):
        fixes = self.report["recoveries"]
        self.assertTrue(any(f["failed_tool"] == "PowerShell" and f["fixed_by"] == "Write" for f in fixes), fixes)
        self.assertTrue(any("pip install pyyaml" in f["fix_input"] for f in fixes), fixes)

    def test_a_human_correction_is_found_with_what_was_claimed(self):
        c = self.report["corrections"]
        self.assertEqual(len(c), 1)
        self.assertIn("UTF-8", c[0]["claim"])
        self.assertIn("BOM", c[0]["correction"])

    def test_proposals_cover_skill_and_memory(self):
        props = learning.propose(self.report)
        kinds = {p["kind"] for p in props}
        self.assertIn("skill", kinds)
        self.assertIn("memory", kinds)
        # class-level umbrella per tool (Hermes: never a skill named after one error string)
        skill = next(p for p in props if p["kind"] == "skill" and "yaml" in p["draft"])
        self.assertIn("bash-pitfalls", skill["draft"])
        self.assertTrue(skill["draft"].startswith("---\nname: "))
        self.assertIn("description:", skill["draft"])
        self.assertIn("pip install pyyaml", skill["draft"])          # the fix that worked is in the skill
        self.assertIn("3", skill["evidence"])

    def test_private_mode_keeps_mechanics_and_drops_human_words(self):
        props = learning.propose(self.report, private=True)
        text = json.dumps(props, ensure_ascii=False)
        self.assertNotIn("PowerShell 5.1 ломает", text)              # the human's own words are not copied
        self.assertNotIn("saved with UTF-8", text)                   # nor the claim, which may quote them
        self.assertTrue(any(p["kind"] == "memory" for p in props))  # but the correction still yields a proposal

    def test_markdown_render(self):
        md = learning.render_md(learning.propose(self.report))
        self.assertIn("# Learning proposals", md)
        self.assertIn("```", md)

    def test_a_broken_line_and_a_missing_file(self):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        self.assertEqual(learning.analyze(self.path)["repeated_errors"][0]["count"], 3)
        with self.assertRaises(FileNotFoundError):
            learning.analyze(os.path.join(self.tmp.name, "nope.jsonl"))


if __name__ == "__main__":
    unittest.main()
