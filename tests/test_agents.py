import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import agents, jev  # noqa: E402

HERE = os.path.dirname(__file__)
FAKE_CLAUDE = os.path.join(HERE, "fake_claude.py")


class ClaudeRole(unittest.TestCase):
    def test_a_role_speaks_through_the_confined_engine_with_its_own_character(self):
        with tempfile.TemporaryDirectory() as d:
            rec = os.path.join(d, "rec.json")
            a = agents.from_config({"name": "Raven", "kind": "claude", "model": "claude-haiku-5-5",
                                    "character": "Sharp and bold."}, d, claude_cmd=["python", FAKE_CLAUDE, rec])
            text, cost = a.speak("hello room")
            self.assertEqual(text, "echo: hello room")
            self.assertEqual(cost, 0.01)
            with open(rec, encoding="utf-8") as f:
                called = json.load(f)
            args = called["args"]
            self.assertIn("--restricted", args)                                   # same confinement as herald
            sp = args[args.index("--append-system-prompt-file") + 1]
            with open(sp, encoding="utf-8") as f:
                self.assertIn("Sharp and bold.", f.read())
            self.assertNotIn("--resume", args)                                     # each reply is one fresh turn


    def test_a_failed_turn_says_so_in_the_reply(self):
        with tempfile.TemporaryDirectory() as d:
            a = agents.from_config({"name": "Raven", "kind": "claude"}, d,
                                   claude_cmd=["python", FAKE_CLAUDE, os.path.join(d, "rec.json")])
            with open(os.path.join(a.ws, "fake_mode"), "w") as f:
                f.write("error")
            self.assertIn("Raven could not answer", a.speak("hi")[0])


class Command(unittest.TestCase):
    def test_an_external_model_is_any_command_that_reads_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "echo.py")
            with open(script, "w") as f:
                f.write("import sys; print('gem: ' + sys.stdin.read().strip()[-5:])")
            a = agents.from_config({"name": "Gemini", "kind": "command", "command": ["python", script]}, d)
            self.assertEqual(a.speak("say hello")[0], "gem: hello")

    def test_a_cli_that_takes_the_question_as_an_argument_runs_in_its_own_empty_folder(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "arg.py")
            with open(script, "w") as f:
                f.write("import os, sys; print(sys.argv[-1][-5:] + ' in ' + os.path.basename(os.getcwd()) "
                        "+ ' ' + str(len(os.listdir('.'))))")
            a = agents.from_config({"name": "Gemini", "kind": "command", "command": ["python", script],
                                    "prompt_arg": True}, d)
            self.assertEqual(a.speak("say hello")[0], "hello in cwd 0")

    def test_a_failing_command_says_so_instead_of_crashing_the_room(self):
        with tempfile.TemporaryDirectory() as d:
            a = agents.from_config({"name": "Gone", "kind": "command", "command": ["no-such-program-xyz"]}, d)
            text, cost = a.speak("hi")
            self.assertIn("could not answer", text)
            self.assertEqual(cost, 0.0)

    def test_bad_entries_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"kind": "claude"}, {"name": "x", "kind": "magic"}, {"name": "x", "kind": "command"}):
                with self.assertRaises(ValueError):
                    agents.from_config(bad, d)


class Jev(unittest.TestCase):
    def test_the_judge_asks_one_narrow_question_and_reads_the_probability(self):
        sent = {}

        def transport(url, body, headers):
            sent.update(url=url, body=json.loads(body), headers=headers)
            return json.dumps({"answers": {"attack": {"type": "noul", "noul": 0.83}}}).encode()

        j = jev.Judge("k-123", transport=transport)
        self.assertAlmostEqual(j.attack("only an idiot would", "naming"), 0.83)
        self.assertEqual(sent["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer k-123")
        q = sent["body"]["questions"]["attack"]
        self.assertEqual(q["type"], "noul")
        self.assertIn("only an idiot would", json.dumps(sent["body"]["state"]))

    def test_when_jev_fails_the_reply_is_let_through(self):
        def broken(url, body, headers):
            raise OSError("network down")
        self.assertEqual(jev.Judge("k", transport=broken).attack("hello", "t"), 0.0)

    def test_the_key_comes_only_from_the_environment(self):
        os.environ.pop("TYPESAFE_API_KEY", None)
        self.assertIsNone(jev.from_env())
        os.environ["TYPESAFE_API_KEY"] = "k"
        try:
            self.assertIsInstance(jev.from_env(), jev.Judge)
        finally:
            os.environ.pop("TYPESAFE_API_KEY")


if __name__ == "__main__":
    unittest.main()
