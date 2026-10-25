"""Findings of an independent re-score of learning, 25.10. Each test is red on 0.5.1."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import learning, notebook, run, speak_first, team  # noqa: E402
from herald.engine import EngineConfig  # noqa: E402
from test_learning import T, Transcript  # noqa: E402
from test_notebook import Engine  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402

SECRET = "PURPLE-OTTER-7731"


def session_a():
    t = Transcript()
    t.user(f"build the report; my bank PIN hint is {SECRET}", T(0))
    for k in (1, 10, 20):
        t.tool("Bash", {"command": "python report.py"},
               "Traceback (most recent call last):\nModuleNotFoundError: No module named 'jinja2'", T(k), True)
        t.tool("Bash", {"command": "python -m pip install jinja2"}, "Successfully installed jinja2-3.1", T(k + 1))
        t.tool("Bash", {"command": "python report.py"}, "ok", T(k + 2))
    for k in (30, 31):
        t.tool("Bash", {"command": f"git checkout {SECRET}"}, f"error: pathspec '{SECRET}' did not match", T(k), True)
    t.say("Fixed: report now built, 118 rows.", T(40))
    t.recs.append({"type": "attachment", "timestamp": T(41),
                   "attachment": {"type": "queued_command", "prompt": "stop, don't commit yet"}})
    return t


class Private(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = session_a().write(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_never_carries_words_the_human_typed(self):
        md = learning.render_md(learning.propose(learning.analyze(self.path), private=True))
        self.assertNotIn("PURPLE", md)

    def test_names_with_digits_survive(self):
        md = learning.render_md(learning.propose(learning.analyze(self.path)))
        self.assertIn("jinja2", md)
        self.assertNotIn("jinjaN", md)

    def test_private_keeps_a_useful_fix(self):
        md = learning.render_md(learning.propose(learning.analyze(self.path), private=True))
        self.assertIn("pip install jinja2", md)

    def test_an_instruction_is_not_a_correction(self):
        self.assertEqual(learning.analyze(self.path)["corrections"], [])


class NotTooPrivate(unittest.TestCase):
    """On a real session the first fix masked every word the human ever wrote, error lines turned to <typed>."""

    def test_plain_words_and_a_compaction_summary_do_not_garble_error_lines(self):
        t = Transcript()
        t.user("the string to replace was not found again, ask Ivana about it", T(0))
        t.recs.append({"type": "user", "isCompactSummary": True, "timestamp": T(1),
                       "message": {"role": "user", "content": "Summary: Edit String Replace Found File Tool Error"}})
        for k in (2, 6, 10):
            t.tool("Edit", {"file_path": "m.py", "old_string": "a", "new_string": "b"},
                   "<tool_use_error>String to replace not found in file.</tool_use_error>", T(k), True)
            t.tool("Read", {"file_path": "m.py"}, "...", T(k + 1))
            t.tool("Edit", {"file_path": "m.py", "old_string": "x", "new_string": "b"}, "ok", T(k + 2))
        with tempfile.TemporaryDirectory() as d:
            rep = learning.analyze(t.write(d))
        self.assertIn("ivana", rep["typed"])                   # a name inside a sentence is masked
        t2 = Transcript()                                      # tool names written by a human are not private
        t2.user("then Read the File again and Edit it in PowerShell", T(0))
        with tempfile.TemporaryDirectory() as d2:
            typed = learning.analyze(t2.write(d2))["typed"]
        for w in ("read", "file", "edit", "powershell"):
            self.assertNotIn(w, typed)
        md = learning.render_md(learning.propose(rep, private=True))
        self.assertIn("String to replace not found", md)


class TeamFix(unittest.TestCase):
    def test_a_fix_found_by_one_role_is_offered_to_the_other(self):
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "p")
            os.makedirs(proj)
            a = session_a()
            a.recs.append({"type": "custom-title", "customTitle": "builder"})
            b = Transcript()
            for k in (1, 5):
                b.tool("Bash", {"command": "python check.py"},
                       "ModuleNotFoundError: No module named 'jinja2'", T(k), True)
            b.recs.append({"type": "custom-title", "customTitle": "reviewer"})
            for name, t in (("a", a), ("b", b)):
                os.replace(t.write(d), os.path.join(proj, name + ".jsonl"))
            team.run(proj, os.path.join(d, "out"))
            with open(os.path.join(d, "out", "INDEX.md"), encoding="utf-8") as f:
                idx = f.read()
            line = next(l for l in idx.splitlines() if "jinja2" in l)
            self.assertIn("pip install jinja2", line)
            for name in os.listdir(os.path.join(d, "out")):     # team is private: a typed secret goes nowhere
                with open(os.path.join(d, "out", name), encoding="utf-8") as f:
                    self.assertNotIn("PURPLE", f.read(), name)


class Telegram(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.control, self.workspace = os.path.join(d, "control"), os.path.join(d, "ws")
        os.makedirs(self.control)
        os.makedirs(self.workspace)
        self.root = os.path.join(d, "home")
        proj = os.path.join(self.root, "projects", notebook.mangle(self.workspace))
        os.makedirs(proj)
        os.replace(session_a().write(d), os.path.join(proj, "sess-1.jsonl"))
        self.bot = FakeBot()
        persona = os.path.join(d, "persona.md")
        with open(persona, "w", encoding="utf-8") as fh:
            fh.write("You are a herald.")
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        self.h = run.Herald(self.bot, Engine(), EngineConfig(append_system_prompt_file=persona), gate, [], self.control, self.workspace, {OWNER},
                            transcript_roots=[self.root], learn_min_sessions=1)

    def tearDown(self):
        self.tmp.cleanup()

    def last(self):
        return self.bot.texts[-1][1]

    def test_learn_from_telegram_is_private(self):
        # outside review round 3 (25.10): /learn copied a person's name and phone number into the proposals
        seen = {}
        real = run.learning.propose

        def spy(report, private=False, **k):
            seen["private"] = private
            return real(report, private=private, **k)
        run.learning.propose = spy
        try:
            self.h.handle(text("go"))
            self.h.handle(text("/learn"))
        finally:
            run.learning.propose = real
        self.assertTrue(seen.get("private"), seen)

    def test_learn_shows_what_would_be_kept(self):
        self.h.handle(text("go"))
        self.h.handle(text("/learn"))
        self.assertIn("pip install jinja2", self.last())        # the draft itself, not only a title

    def test_approving_twice_keeps_one_line_and_learn_does_not_offer_it_again(self):
        self.h.handle(text("go"))
        self.h.handle(text("/learn"))
        n = next(l.split(".")[0] for l in self.last().splitlines() if "[memory]" in l)
        self.h.handle(text(f"/approve {n}"))
        self.h.handle(text("/learn"))
        self.assertNotIn(self.h.nb.memory()[0][:40], self.last())   # not offered again
        self.h.nb.add_memory(self.h.nb.memory()[0])                    # and approving it again keeps one line
        self.assertEqual(len(self.h.nb.memory()), 1)

    def test_an_approved_skill_can_be_forgotten_and_is_archived(self):
        self.h.handle(text("go"))
        self.h.handle(text("/learn"))
        n = next(l.split(".")[0] for l in self.last().splitlines() if "bash-pitfalls" in l)
        self.h.handle(text(f"/approve {n}"))
        self.h.handle(text("/forget bash-pitfalls"))
        self.assertNotIn("bash-pitfalls", self.h.nb.skills())
        self.assertTrue(os.path.isdir(os.path.join(self.control, "learned", "archive")))

    def test_the_previous_session_can_be_learned_after_new(self):
        self.h.handle(text("go"))
        self.h.handle(text("/new"))
        self.h.handle(text("/learn"))
        self.assertNotIn("Nothing to learn", self.last())


if __name__ == "__main__":
    unittest.main()
