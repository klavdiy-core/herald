import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import learning, notebook, run, speak_first  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_learning import planted  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class Engine:
    """Records the system prompt each turn really gets."""

    def __init__(self, num_turns=1):
        self.systems, self.num_turns = [], num_turns

    def __call__(self, prompt, workspace, cfg, session_id=None):
        f = cfg.append_system_prompt_file
        with open(f, encoding="utf-8") as fh:
            self.systems.append(fh.read())
        return TurnResult(True, "ok", "sess-1", 0.01, num_turns=self.num_turns)


class Loop(unittest.TestCase):
    min_sessions = 1                # these fixtures have one session; the strict default is tested below

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.control, self.workspace = os.path.join(d, "control"), os.path.join(d, "workspace")
        os.makedirs(self.control)
        os.makedirs(self.workspace)
        self.persona = os.path.join(d, "persona.md")
        with open(self.persona, "w", encoding="utf-8") as f:
            f.write("You are a herald.")
        # Claude Code keeps the session where it keeps it: <root>/projects/<mangled cwd>/<session>.jsonl
        self.root = os.path.join(d, "claude_home")
        proj = os.path.join(self.root, "projects", notebook.mangle(self.workspace))
        os.makedirs(proj)
        planted().write(proj)
        os.replace(os.path.join(proj, "s.jsonl"), os.path.join(proj, "sess-1.jsonl"))
        self.bot = FakeBot()
        self.engine = Engine()
        self.h = self.make()

    def make(self):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "speak.json"),
                                os.path.join(self.control, "STOP"))
        return run.Herald(bot=self.bot, run_turn=self.engine,
                          engine_cfg=EngineConfig(append_system_prompt_file=self.persona), gate=gate, jobs=[],
                          control=self.control, workspace=self.workspace, owners={OWNER},
                          transcript_roots=[self.root], learn_min_sessions=self.min_sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def last(self):
        return self.bot.texts[-1][1]

    def test_learn_lists_numbered_proposals_from_the_current_session(self):
        self.h.handle(text("build the report"))
        self.h.handle(text("/learn"))
        self.assertIn("1.", self.last())
        self.assertIn("/approve", self.last())
        self.assertIn("bash-pitfalls", self.last())

    def test_an_approved_skill_reaches_the_next_turn_and_the_persona_stays(self):
        self.h.handle(text("build the report"))
        self.h.handle(text("/learn"))
        n = next(l.split(".")[0] for l in self.last().splitlines() if "bash-pitfalls" in l)
        self.h.handle(text(f"/approve {n}"))
        self.assertTrue(os.path.exists(os.path.join(self.control, "learned", "skills", "bash-pitfalls", "SKILL.md")))
        self.h.handle(text("again please"))
        system = self.engine.systems[-1]
        self.assertIn("You are a herald.", system)
        self.assertIn("pip install pyyaml", system)

    def test_approved_memory_reaches_the_next_turn_and_forget_removes_it(self):
        self.h.handle(text("x"))
        self.h.handle(text("/learn"))
        n = next(l.split(".")[0] for l in self.last().splitlines() if l[:1].isdigit() and "[memory]" in l)
        self.h.handle(text(f"/approve {n}"))
        self.h.handle(text("/memory"))
        self.assertIn("1.", self.last())
        self.h.handle(text("y"))
        self.assertIn("What you learned", self.engine.systems[-1])
        self.h.handle(text("/forget 1"))
        self.h.handle(text("z"))
        self.assertNotIn("§", self.engine.systems[-1].split("What you learned")[-1] if "What you learned" in
                         self.engine.systems[-1] else "")

    def test_drop_and_a_wrong_number(self):
        self.h.handle(text("x"))
        self.h.handle(text("/learn"))
        self.h.handle(text("/approve 99"))
        self.assertIn("no proposal 99", self.last())
        self.h.handle(text("/drop 1"))
        self.assertIn("dropped", self.last().lower())
        self.h.handle(text("/approve 1"))                # a dropped or approved one is not taken twice
        self.assertIn("no proposal 1", self.last())

    def test_learn_with_no_session_yet(self):
        self.h.handle(text("/learn"))
        self.assertIn("nothing to learn", self.last().lower())

    def test_the_learned_store_is_in_control_where_the_model_cannot_write(self):
        nb = notebook.Notebook(os.path.join(self.control, "learned"))
        self.assertTrue(os.path.realpath(nb.dir).startswith(os.path.realpath(self.control)))

    def test_a_long_turn_gets_one_nudge_then_not_for_five_turns(self):
        self.engine.num_turns = 12
        for i in range(6):
            self.h.handle(text(f"task {i}"))
        nudged = [t for _, t in self.bot.texts if "/learn" in t]
        self.assertEqual(len(nudged), 2)            # turn 1 and turn 6
        self.engine.num_turns = 1
        self.h.handle(text("short"))
        self.assertNotIn("/learn", self.last())


class TwoSessions(Loop):
    """The default: a lesson must happen in two different sessions of this workspace."""
    min_sessions = 2

    def test_one_session_teaches_nothing_yet(self):
        self.h.handle(text("build the report"))
        self.h.handle(text("/learn"))
        self.assertNotIn("bash-pitfalls", self.last())

    def test_the_same_mistake_in_a_second_session_becomes_a_lesson(self):
        proj = os.path.join(self.root, "projects", notebook.mangle(self.workspace))
        planted().write(proj)
        os.replace(os.path.join(proj, "s.jsonl"), os.path.join(proj, "sess-0.jsonl"))
        self.h.handle(text("build the report"))
        self.h.handle(text("/learn"))
        self.assertIn("bash-pitfalls", self.last())

    # the inherited single-session tests do not apply under this threshold
    test_learn_lists_numbered_proposals_from_the_current_session = None
    test_an_approved_skill_reaches_the_next_turn_and_the_persona_stays = None
    test_approved_memory_reaches_the_next_turn_and_forget_removes_it = None
    test_drop_and_a_wrong_number = None


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.nb = notebook.Notebook(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_memory_has_a_cap_and_refuses_beyond_it(self):
        self.nb.add_memory("a" * 2000)
        with self.assertRaises(notebook.Full):
            self.nb.add_memory("b" * 500)
        self.assertEqual(len(self.nb.memory()), 1)

    def test_a_bad_skill_name_is_refused(self):
        with self.assertRaises(ValueError):
            self.nb.add_skill("---\nname: ../evil\ndescription: x\n---\nbody")

    def test_an_umbrella_skill_gains_new_sections_and_keeps_old_ones(self):
        self.nb.add_skill("---\nname: bash-pitfalls\ndescription: d\n---\n\n# Bash\n\n## `err A`\n\nfix A\n")
        self.nb.add_skill("---\nname: bash-pitfalls\ndescription: d\n---\n\n# Bash\n\n## `err A`\n\nfix A\n"
                          "\n## `err B`\n\nfix B\n")
        body = self.nb.skills()["bash-pitfalls"]
        self.assertEqual(body.count("## `err A`"), 1)
        self.assertIn("fix B", body)

    def test_mangle_matches_claude_code(self):
        # seen live 24.10 on claude 2.1.310: every character that is not a letter or digit becomes '-'
        self.assertEqual(notebook.mangle(r"C:\Users\me\AppData\Local\Temp\x_y.z"),
                         "C--Users-me-AppData-Local-Temp-x-y-z")


class Cli(unittest.TestCase):
    def test_learn_from_the_command_line_writes_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            src = planted().write(d)
            out = os.path.join(d, "p.md")
            r = subprocess.run([sys.executable, "-m", "herald", "learn", src, "--private", "--out", out],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(out, encoding="utf-8") as f:
                md = f.read()
            self.assertIn("# Learning proposals", md)
            self.assertNotIn("PowerShell 5.1 ломает", md)

    def test_ignore_takes_signal_exits_out(self):
        with tempfile.TemporaryDirectory() as d:
            rep = learning.analyze(planted().write(d), ignore=r"yaml")
            self.assertFalse(any("yaml" in g["signature"] for g in rep["repeated_errors"]))


if __name__ == "__main__":
    unittest.main()
