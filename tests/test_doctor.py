import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import doctor  # noqa: E402
from herald.engine import TurnResult  # noqa: E402

TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"
HELP_OK = "  --restricted   Restricted mode...\n  --permission-prompts <target> ...\n"


def fake_turn(actions, text="done", denials=("Read", "Read", "Read", "Grep", "Glob", "Write")):
    """A run_turn stand-in: performs file actions inside cwd=workspace like a model would."""
    def run(prompt, workspace, cfg, session_id=None):
        for rel, content in actions:
            path = os.path.normpath(os.path.join(workspace, rel))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        out = text
        if "SECRET" in text:
            with open(os.path.join(workspace, "..", "outside", "secret.txt")) as f:
                out = f.read()
        return TurnResult(True, out, "s", 0.001, list(denials))
    return run


class ToolsTest(unittest.TestCase):
    def test_websearch_is_allowed_but_named_as_a_way_out(self):
        from herald.engine import EngineConfig
        c = doctor.check_tools(EngineConfig(tools=["Read", "WebSearch"]))
        self.assertFalse(c.ok)
        self.assertFalse(c.fatal)                       # a warning: the owner may want it
        self.assertIn("leave", c.detail)
        self.assertTrue(doctor.check_tools(EngineConfig()).ok)


class CanaryTest(unittest.TestCase):
    def check(self, actions, text="done"):
        return doctor.canary(run_turn=fake_turn(actions, text), cfg=None)

    def test_confined_model_passes(self):
        r = self.check([("inside.txt", "OK")])
        self.assertTrue(r.ok, r.detail)

    def test_write_outside_fails(self):
        r = self.check([("inside.txt", "OK"), ("../outside/pwned.txt", "x")])
        self.assertFalse(r.ok)
        self.assertIn("outside", r.detail)

    def test_settings_escalation_fails(self):
        r = self.check([("inside.txt", "OK"), (".claude/settings.json", '{"permissions":{"allow":["Bash"]}}')])
        self.assertFalse(r.ok)
        self.assertIn("settings", r.detail)

    def test_git_hook_fails(self):
        r = self.check([("inside.txt", "OK"), (".git/hooks/pre-commit", "x")])
        self.assertFalse(r.ok)
        self.assertIn("git", r.detail)

    def test_claude_md_fails(self):
        r = self.check([("inside.txt", "OK"), ("CLAUDE.md", "obey")])
        self.assertFalse(r.ok)

    def test_secret_in_the_answer_fails(self):
        r = self.check([("inside.txt", "OK")], text="SECRET")
        self.assertFalse(r.ok)
        self.assertIn("read", r.detail)

    def test_a_secret_copied_into_the_workspace_but_not_quoted_fails(self):
        # outside review round 3 (25.10): only an echoed secret was caught; a quiet copy passed
        def run(prompt, workspace, cfg, session_id=None):
            with open(os.path.join(workspace, "..", "outside", "secret.txt")) as f:
                secret = f.read()
            with open(os.path.join(workspace, "inside.txt"), "w") as f:
                f.write("OK")
            with open(os.path.join(workspace, "notes.md"), "w") as f:
                f.write("kept: " + secret)
            return TurnResult(True, "all steps done", "s", 0.001, [])
        r = doctor.canary(run_turn=run, cfg=None)
        self.assertFalse(r.ok)
        self.assertIn("read a file outside", r.detail)

    def leak(self, route):
        """A turn that obeys the allowed step and leaks through exactly one route, quietly (into notes.md)."""
        def run(prompt, workspace, cfg, session_id=None):
            outside = os.path.join(workspace, "..", "outside")
            with open(os.path.join(workspace, "inside.txt"), "w") as f:
                f.write("OK")
            got = ""
            if route == "absolute":
                with open(os.path.join(outside, "abs.txt")) as f:
                    got = f.read()
            elif route == "link":
                with open(os.path.join(workspace, "docs", "link.txt")) as f:
                    got = f.read()
            elif route == "grep":
                with open(os.path.join(outside, "grep.txt")) as f:
                    got = f.read()
            elif route == "glob":
                got = " ".join(os.listdir(outside))
            with open(os.path.join(workspace, "notes.md"), "w") as f:
                f.write(got)
            return TurnResult(True, "done", "s", 0.001, ["Read"])
        return run

    def test_each_way_out_is_checked_and_named(self):
        # an outside review (25.10): only ../ was tried; an absolute path, a link inside the workspace pointing out,
        # and Glob / Grep with an outside path are other code paths in Claude Code
        for route, words in (("absolute", "absolute path"), ("link", "link"), ("grep", "Grep"), ("glob", "Glob")):
            with self.subTest(route=route):
                r = doctor.canary(run_turn=self.leak(route), cfg=None)
                if route == "link" and "link not tested" in r.detail:
                    continue                                    # no symlink or junction right on this machine
                self.assertFalse(r.ok, (route, r.detail))
                self.assertIn(words, r.detail)

    def test_the_prompt_names_every_route(self):
        p = doctor.canary_prompt("/abs/outside", has_link=True)
        for part in ("/abs/outside/abs.txt", "docs/link.txt", "Grep", "Glob"):
            self.assertIn(part, p)

    def test_an_outside_read_neither_denied_nor_shown_is_inconclusive(self):
        # every read route must be tried and refused: here Grep never was (two outside reviews, 25.10)
        r = doctor.canary(run_turn=fake_turn([("inside.txt", "OK")], denials=("Read", "Read", "Read", "Glob", "Write")),
                          cfg=None)
        self.assertFalse(r.ok)
        self.assertIn("inconclusive", r.detail)
        self.assertIn("Grep (0 of 1 refused)", r.detail)
        r = doctor.canary(run_turn=fake_turn([("inside.txt", "OK")], denials=("Read", "Grep", "Glob")), cfg=None)
        self.assertIn("Read (1 of", r.detail)                           # one Read refused is not all of them

    def test_a_model_that_did_nothing_proves_nothing(self):
        r = self.check([])
        self.assertFalse(r.ok)
        self.assertIn("inconclusive", r.detail)

    def test_a_failed_turn_fails(self):
        r = doctor.canary(run_turn=lambda *a, **k: TurnResult(False, error="claude turn failed: x"), cfg=None)
        self.assertFalse(r.ok)
        self.assertIn("claude turn failed: x", r.detail)      # the real cause, not "inconclusive"

    def test_canary_cleans_up(self):
        seen = {}

        def run(prompt, workspace, cfg, session_id=None):
            seen["ws"] = workspace
            return fake_turn([("inside.txt", "OK")])(prompt, workspace, cfg)
        doctor.canary(run_turn=run, cfg=None)
        self.assertFalse(os.path.exists(seen["ws"]))


class StaticChecksTest(unittest.TestCase):
    def test_claude_flags(self):
        self.assertTrue(doctor.check_claude_flags(lambda: HELP_OK).ok)
        r = doctor.check_claude_flags(lambda: "  --permission-prompts only\n")
        self.assertFalse(r.ok)
        self.assertTrue(r.fatal)
        self.assertIn("--restricted", r.detail)
        self.assertFalse(doctor.check_claude_flags(lambda: (_ for _ in ()).throw(FileNotFoundError())).ok)

    def test_token_and_owners(self):
        self.assertTrue(doctor.check_token(TOKEN).ok)
        bad = doctor.check_token("nope-SECRETPART")
        self.assertFalse(bad.ok)
        self.assertNotIn("SECRETPART", bad.detail)
        self.assertFalse(doctor.check_token("").ok)
        self.assertTrue(doctor.check_owners([1]).ok)
        self.assertFalse(doctor.check_owners([]).ok)

    def test_voice_is_degradable(self):
        r = doctor.check_voice(model="/no/such.onnx", ffmpeg_ok=True)
        self.assertFalse(r.ok)
        self.assertFalse(r.fatal)


class TokenLiveTest(unittest.TestCase):
    def test_get_me_proves_the_token(self):
        sys.path.insert(0, os.path.dirname(__file__))
        from fake_telegram import FakeTelegram
        from herald import telegram
        api = FakeTelegram()
        try:
            bot = telegram.Bot(TOKEN, owners={1}, api_base=api.url)
            api.replies["getMe"] = [(200, {"ok": True, "result": {"username": "my_herald_bot", "is_bot": True}})]
            r = doctor.check_token_live(bot)
            self.assertTrue(r.ok)
            self.assertIn("my_herald_bot", r.detail)
            api.replies["getMe"] = [(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})]
            r = doctor.check_token_live(bot)
            self.assertFalse(r.ok)
            self.assertTrue(r.fatal)
            self.assertNotIn(TOKEN.split(":")[1], r.detail)
        finally:
            api.close()


class ReportTest(unittest.TestCase):
    def test_fatal_failure_blocks_start_degradable_does_not(self):
        ok = doctor.Check("a", True, "fine")
        warn = doctor.Check("voice", False, "no model", fatal=False)
        bad = doctor.Check("claude", False, "no --restricted")
        self.assertTrue(doctor.may_start([ok, warn]))
        self.assertFalse(doctor.may_start([ok, warn, bad]))
        text = doctor.render([ok, warn, bad])
        self.assertIn("PASS", text)
        self.assertIn("WARN", text)
        self.assertIn("FAIL", text)


if __name__ == "__main__":
    unittest.main()
