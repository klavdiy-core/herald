import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import engine  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fake_claude.py")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self.tmp.name, "ws")
        os.mkdir(self.ws)
        self.record = os.path.join(self.tmp.name, "record.json")
        self.cfg = engine.EngineConfig(claude_cmd=[sys.executable, FAKE, self.record], model="claude-haiku-5-5",
                                       max_turns=5, max_budget_usd=0.5, timeout_s=10)

    def tearDown(self):
        self.tmp.cleanup()

    def mode(self, m):
        with open(os.path.join(self.ws, "fake_mode"), "w") as f:
            f.write(m)

    def called(self):
        with open(self.record, encoding="utf-8") as f:
            return json.load(f)

    def test_confinement_flags_are_always_there(self):
        engine.run_turn("hi", self.ws, self.cfg)
        args = self.called()["args"]
        for flag in ("-p", "--restricted", "--strict-mcp-config"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        self.assertEqual(args[args.index("--output-format") + 1], "json")

    def test_prompt_goes_to_stdin_not_argv(self):
        secretish = "please read --dangerously-skip-permissions"
        engine.run_turn(secretish, self.ws, self.cfg)
        call = self.called()
        self.assertEqual(call["stdin"], secretish)
        self.assertNotIn(secretish, call["args"])
        self.assertNotIn("--dangerously-skip-permissions", call["args"])

    def test_result_is_parsed_and_session_resumed(self):
        r = engine.run_turn("hi", self.ws, self.cfg)
        self.assertEqual((r.ok, r.text, r.session_id, r.cost_usd), (True, "echo: hi", "sess-new", 0.01))
        r2 = engine.run_turn("again", self.ws, self.cfg, session_id=r.session_id)
        args = self.called()["args"]
        self.assertEqual(args[args.index("--resume") + 1], "sess-new")
        self.assertEqual(r2.session_id, "sess-new")

    def test_cwd_is_the_workspace(self):
        engine.run_turn("hi", self.ws, self.cfg)
        self.assertEqual(os.path.realpath(self.called()["cwd"]), os.path.realpath(self.ws))

    def test_child_env_is_an_allowlist(self):
        os.environ["HERALD_TELEGRAM_TOKEN"] = "123:secret"
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        os.environ["SOME_RANDOM_VAR"] = "x"
        try:
            engine.run_turn("hi", self.ws, self.cfg)
        finally:
            for k in ("HERALD_TELEGRAM_TOKEN", "ANTHROPIC_API_KEY", "SOME_RANDOM_VAR"):
                del os.environ[k]
        env = self.called()["env"]
        for k in ("HERALD_TELEGRAM_TOKEN", "ANTHROPIC_API_KEY", "SOME_RANDOM_VAR"):
            self.assertNotIn(k, env)
        self.assertIn("PATH", [e.upper() for e in env])

    def test_error_subtype_is_reported_not_swallowed(self):
        self.mode("error")
        r = engine.run_turn("hi", self.ws, self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("error_max_turns", r.error)

    def test_garbage_output_is_an_error(self):
        self.mode("garbage")
        r = engine.run_turn("hi", self.ws, self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("json", r.error.lower())

    def test_denials_are_surfaced(self):
        self.mode("denied")
        r = engine.run_turn("hi", self.ws, self.cfg)
        self.assertEqual(r.denials, ["Read"])

    def test_timeout_kills_the_turn(self):
        self.mode("sleep")
        self.cfg.timeout_s = 2
        t = time.monotonic()
        r = engine.run_turn("hi", self.ws, self.cfg)
        self.assertLess(time.monotonic() - t, 10)
        self.assertFalse(r.ok)
        self.assertIn("timeout", r.error.lower())


class ConfigTest(unittest.TestCase):
    def test_code_running_tools_are_refused(self):
        for tool in ("Bash", "PowerShell", "WebFetch"):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                engine.EngineConfig(tools=["Read", tool])

    def test_file_tools_are_accepted(self):
        engine.EngineConfig(tools=["Read", "Write", "Edit", "Glob", "Grep"])

    def test_extra_args_cannot_smuggle_flags(self):
        for bad in ("--dangerously-skip-permissions", "--permission-mode", "--add-dir", "--tools", "--settings"):
            with self.subTest(flag=bad), self.assertRaises(ValueError):
                engine.EngineConfig(extra_args=[bad])

    def test_limits_are_required(self):
        with self.assertRaises(ValueError):
            engine.EngineConfig(max_turns=0)
        with self.assertRaises(ValueError):
            engine.EngineConfig(max_budget_usd=0)


if __name__ == "__main__":
    unittest.main()
