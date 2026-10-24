import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import doctor  # noqa: E402
from herald.engine import TurnResult  # noqa: E402

TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"
HELP_OK = "  --restricted   Restricted mode...\n  --permission-prompts <target> ...\n"


def fake_turn(actions, text="done"):
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
        return TurnResult(True, out, "s", 0.001, ["Read", "Write", "Write"])
    return run


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
