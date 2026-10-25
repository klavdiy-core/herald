"""Jev's two opt-in jobs besides the room (her word 25.10: «добавь еще jev в herald»): the /learn sieve and the model
router. Red before herald 0.22.0: Judge had no lesson()/simple(), learning had no jev_sift, Herald no route."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import jev, learning, run, speak_first  # noqa: E402
from herald.engine import EngineConfig  # noqa: E402
from test_economy import Engine  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402


class FakeJudge:
    def __init__(self, lesson=None, simple=None):
        self.lessons, self.simples, self.asked = lesson or {}, simple, []

    def lesson(self, title, draft):
        self.asked.append(title)
        return self.lessons.get(title)

    def simple(self, message):
        self.asked.append(message)
        return self.simple_p(message) if callable(self.simple_p) else self.simple_p

    @property
    def simple_p(self):
        return self.simples


class Questions(unittest.TestCase):
    def test_lesson_and_simple_each_ask_one_narrow_question_and_fail_to_none(self):
        sent = []

        def transport(url, body, headers):
            b = json.loads(body)
            sent.append(b)
            name = next(iter(b["questions"]))
            return json.dumps({"answers": {name: {"type": "noul", "noul": 0.2}}}).encode()

        j = jev.Judge("k", transport=transport)
        self.assertAlmostEqual(j.lesson("Use Read before Edit", "- re-read the file"), 0.2)
        self.assertAlmostEqual(j.simple("hi, how are you?"), 0.2)
        self.assertEqual([next(iter(b["questions"])) for b in sent], ["lesson", "simple"])
        self.assertIn("re-read the file", json.dumps(sent[0]["state"]))

        def broken(url, body, headers):
            raise OSError("down")
        b = jev.Judge("k", transport=broken)
        self.assertIsNone(b.lesson("t", "d"))
        self.assertIsNone(b.simple("m"))
        self.assertEqual(b.attack("x", "t"), 0.0)                 # the room still lets a reply through


class Sieve(unittest.TestCase):
    def test_noise_moves_to_its_own_section_and_an_unreachable_jev_keeps_everything(self):
        props = [{"kind": "memory", "title": "Use Read before Edit", "evidence": "e", "draft": "d1"},
                 {"kind": "memory", "title": "cd fixed it", "evidence": "e", "draft": "d2"},
                 {"kind": "memory", "title": "no answer", "evidence": "e", "draft": "d3"}]
        kept, noise = learning.jev_sift(props, FakeJudge({"Use Read before Edit": 0.9, "cd fixed it": 0.1}))
        self.assertEqual([p["title"] for p in kept], ["Use Read before Edit", "no answer"])
        self.assertEqual([(p["title"], p["jev"]) for p in noise], [("cd fixed it", 0.1)])
        md = learning.render_md(kept, noise=noise)
        self.assertIn("## Jev: noise, not lessons", md)
        self.assertIn("cd fixed it — lesson probability 0.10", md)
        self.assertEqual(learning.jev_sift(props, None)[1], [])


class Route(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control, self.ws = os.path.join(self.tmp.name, "c"), os.path.join(self.tmp.name, "w")
        os.makedirs(self.control)
        os.makedirs(self.ws)
        self.bot, self.engine = FakeBot(), Engine()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, judge, route="claude-haiku-5-5"):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "speak.json"),
                                os.path.join(self.control, "STOP"))
        return run.Herald(bot=self.bot, run_turn=self.engine, engine_cfg=EngineConfig(model="claude-sonnet-5-5"),
                          gate=gate, jobs=[], control=self.control, workspace=self.ws, owners={OWNER},
                          transcript_roots=[], jev_judge=judge, route_simple=route)

    def test_a_simple_message_goes_to_the_small_model_and_a_hard_one_does_not(self):
        h = self.make(FakeJudge(simple=lambda m: 0.9 if m.startswith("hi") else 0.2))
        h.handle(text("hi there"))
        h.handle(text("refactor the parser and fix the failing tests"))
        self.assertEqual(self.engine.models, ["claude-haiku-5-5", "claude-sonnet-5-5"])

    def test_the_owners_model_always_wins_and_jev_down_means_the_configured_model(self):
        h = self.make(FakeJudge(simple=0.95))
        h.handle(text("/model claude-opus-5-5"))
        h.handle(text("hi"))
        self.assertEqual(self.engine.models[-1], "claude-opus-5-5")
        h2 = self.make(FakeJudge(simple=None))
        h2.handle(text("/model default"))
        h2.handle(text("hi"))
        self.assertEqual(self.engine.models[-1], "claude-sonnet-5-5")

    def test_status_names_the_routed_turn(self):
        h = self.make(FakeJudge(simple=0.9))
        h.handle(text("hi"))
        h.handle(text("/status"))
        self.assertIn("last turn: claude-haiku-5-5, Jev 0.90", self.bot.texts[-1][1])

    def test_no_route_without_a_judge_or_a_small_model(self):
        self.make(None).handle(text("hi"))
        self.make(FakeJudge(simple=0.99), route=None).handle(text("hi"))
        self.assertEqual(self.engine.models, ["claude-sonnet-5-5", "claude-sonnet-5-5"])


class Config(unittest.TestCase):
    def cfg(self, jev_block):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "c.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"owners": [1], "workspace": os.path.join(d, "w"), "control": os.path.join(d, "c"),
                       "jev": jev_block}, f)
        return p

    def test_the_jev_block_is_checked(self):
        run.load_config(self.cfg({"learn": True, "route_simple": "claude-haiku-5-5", "route_bar": 0.8}))
        for bad in ({"route_simple": "gpt-4"}, {"learn": "yes"}, {"route_bar": 2}, {"routing": True}):
            with self.assertRaises(ValueError, msg=bad):
                run.load_config(self.cfg(bad))


if __name__ == "__main__":
    unittest.main()
