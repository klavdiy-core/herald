"""The boundary audit of 25.10 (three read-only reviews: files, secrets, processes). One test per confirmed finding,
each red on 0.22.1."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import agents, engine, hearing, room, run, voice  # noqa: E402
from test_room import Voice, seats  # noqa: E402


def cfg_file(d, **over):
    c = {"owners": [1], "workspace": os.path.join(d, "ws"), "control": os.path.join(d, "control")}
    c.update(over)
    p = os.path.join(d, "c.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(c, f)
    return p


class Files(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    @unittest.skipUnless(sys.platform == "win32", "case-insensitive paths")
    def test_F1_control_inside_the_workspace_is_refused_whatever_the_case(self):
        with self.assertRaises(ValueError):
            run.load_config(cfg_file(self.d, workspace=os.path.join(self.d, "WS"),
                                     control=os.path.join(self.d, "ws", "control")))

    def test_F3_persona_or_agents_dir_inside_the_workspace_is_refused(self):
        ws = os.path.join(self.d, "ws")
        for k, v in (("persona_file", os.path.join(ws, "persona.md")), ("agents_dir", os.path.join(ws, "room"))):
            with self.assertRaises(ValueError, msg=k):
                run.load_config(cfg_file(self.d, **{k: v}))
        run.load_config(cfg_file(self.d, persona_file=os.path.join(self.d, "persona.md")))   # outside: fine


class Secrets(unittest.TestCase):
    def test_F4_speech_and_voice_programs_get_no_bot_token_and_no_jev_key(self):
        os.environ["HERALD_TELEGRAM_TOKEN"], os.environ["TYPESAFE_API_KEY"] = "tok-123", "jev-456"
        seen = {}
        real = hearing.subprocess.run

        def fake(argv, **kw):
            seen[argv[0]] = kw.get("env")
            return hearing.subprocess.CompletedProcess(argv, 0, stdout=b"hello", stderr=b"")

        hearing.subprocess.run = voice.subprocess.run = fake
        try:
            hearing.transcribe("a.ogg", hearing.HearConfig(stt_cmd=["stt-prog"]))
            voice._run(["piper-prog"], 5, "piper")
        finally:
            hearing.subprocess.run = voice.subprocess.run = real
            os.environ.pop("HERALD_TELEGRAM_TOKEN", None)
            os.environ.pop("TYPESAFE_API_KEY", None)
        for prog in ("stt-prog", "piper-prog"):
            env = seen[prog]
            self.assertIsNotNone(env, prog)
            self.assertNotIn("HERALD_TELEGRAM_TOKEN", env)
            self.assertNotIn("TYPESAFE_API_KEY", env)
            self.assertIn("PATH", {k.upper() for k in env})


class Processes(unittest.TestCase):
    def test_F5_a_stop_during_the_last_reply_means_no_paid_summary(self):
        stop = {"on": False}

        class Stopper(Voice):
            def speak(self, prompt):
                stop["on"] = True                        # /stop arrives while this reply runs
                return super().speak(prompt)

        a, b = Voice("A", ["a1"]), Stopper("B", ["b1"])
        r = room.Room("t", seats(a, b), should_stop=lambda: "stop" if stop["on"] else None)
        r.step()
        r.step()
        asked_before = len(a.prompts) + len(b.prompts)
        r.end()
        self.assertEqual(len(a.prompts) + len(b.prompts), asked_before)   # nobody was asked to write a summary
        self.assertIn("stopped", [e for e in r.events if e["type"] == "summary"][-1]["text"])

    def test_F6_a_room_claude_reply_gets_the_stop_check(self):
        got = {}
        real = engine.run_turn

        def fake(prompt, ws, cfg, session_id=None, should_abort=None, check_s=1.0):
            got["abort"] = should_abort
            return engine.TurnResult(True, "ok", None, 0.01)

        d = tempfile.mkdtemp()
        engine.run_turn = fake
        try:
            role = agents.ClaudeRole("A", "a role", "claude-haiku-5-5", d)
            r = room.Room("t", [room.Seat(role, "ideas")], should_stop=lambda: "stop")
            role.speak("hi")
        finally:
            engine.run_turn = real
        self.assertIsNotNone(got["abort"])
        self.assertTrue(got["abort"]())
        self.assertIs(r.seats[0].agent, role)


class Learn(unittest.TestCase):
    def test_F2_a_proposal_cut_in_learn_cannot_be_approved_before_show(self):
        from test_run import OWNER, FakeBot, text
        from herald import learning, speak_first
        from herald.engine import EngineConfig
        from test_economy import Engine
        d = tempfile.mkdtemp()
        ctl, ws = os.path.join(d, "c"), os.path.join(d, "w")
        os.makedirs(ctl)
        os.makedirs(ws)
        bot = FakeBot()
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(ctl, "s.json"), os.path.join(ctl, "STOP"))
        h = run.Herald(bot=bot, run_turn=Engine(), engine_cfg=EngineConfig(), gate=gate, jobs=[], control=ctl,
                       workspace=ws, owners={OWNER}, transcript_roots=[])
        long_draft = "- When X fails: " + "do the careful thing and then " * 40 + "HIDDEN-TAIL"
        props = [{"kind": "memory", "title": "long one", "evidence": "e", "draft": long_draft, "seen": False}]
        h.nb.set_pending(props)
        h.handle(text("/approve 1"))
        self.assertIn("/show 1", bot.texts[-1][1])
        self.assertNotIn("HIDDEN-TAIL", h.nb.compose(""))
        h.handle(text("/show 1"))
        self.assertTrue(any("HIDDEN-TAIL" in t for _, t in bot.texts))
        h.handle(text("/approve 1"))
        self.assertIn("Approved 1", bot.texts[-1][1])


if __name__ == "__main__":
    unittest.main()
