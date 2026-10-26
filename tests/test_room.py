import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import room  # noqa: E402


class Voice:
    """A fake participant: answers from a script, records what it was asked."""

    def __init__(self, name, lines, cost=0.01):
        self.name, self.lines, self.cost, self.prompts = name, list(lines), cost, []

    def speak(self, prompt):
        self.prompts.append(prompt)
        return (self.lines.pop(0) if self.lines else f"{self.name} has nothing to add."), self.cost


class Judge:
    """Says a reply is an attack when it contains 'idiot'."""

    def __init__(self):
        self.asked = []

    def attack(self, text, topic):
        self.asked.append(text)
        return 0.9 if "idiot" in text else 0.05


def seats(*voices, roles=None):
    roles = roles or ["ideas"] * len(voices)
    return [room.Seat(v, r) for v, r in zip(voices, roles)]


class Rounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_everyone_speaks_in_turn_in_their_own_name_and_sees_the_others(self):
        a, b = Voice("Raven", ["idea A"]), Voice("Magpie", ["idea B"])
        r = room.Room("name the bot", seats(a, b), fmt="brainstorm")
        r.round()
        self.assertEqual([m["speaker"] for m in r.messages], ["Raven", "Magpie"])
        self.assertIn("idea A", b.prompts[0])                  # the second speaker hears the first
        self.assertIn("Raven", b.prompts[0])
        self.assertIn("name the bot", a.prompts[0])

    def test_brainstorm_rounds_have_their_own_rules(self):
        a = Voice("Raven", ["i1", "i2", "i3"])
        r = room.Room("t", seats(a), fmt="brainstorm")
        for _ in range(3):
            r.round()
        self.assertIn("no evaluation", a.prompts[0].lower())
        self.assertIn("yes, and", a.prompts[1].lower())
        self.assertIn("vote", a.prompts[2].lower())

    def test_decide_rounds(self):
        a = Voice("Raven", ["o", "p", "r"])
        r = room.Room("t", seats(a), fmt="decide")
        for _ in range(3):
            r.round()
        self.assertIn("options", a.prompts[0].lower())
        self.assertIn("pros", a.prompts[1].lower())
        self.assertIn("recommend", a.prompts[2].lower())

    def test_after_the_last_round_next_says_done(self):
        a = Voice("Raven", ["1", "2", "3"])
        r = room.Room("t", seats(a), fmt="brainstorm")
        for _ in range(3):
            r.round()
        last = [e for e in r.events if e["type"] == "round"][-1]
        self.assertEqual(last["next"], "all rounds done")

    def test_the_role_is_in_the_prompt(self):
        a = Voice("Raven", ["x"])
        room.Room("t", seats(a, roles=["skeptic"]), fmt="brainstorm").round()
        self.assertIn("skeptic", a.prompts[0].lower())
        self.assertIn("idea, not the person", a.prompts[0].lower())   # the rule every role gets


class Moderator(unittest.TestCase):
    def test_an_attack_is_sent_back_once_and_the_rephrased_reply_shown(self):
        a = Voice("Raven", ["only an idiot would do that", "That plan risks the budget; here is a cheaper one."])
        judge = Judge()
        r = room.Room("t", seats(a), fmt="brainstorm", judge=judge)
        r.round()
        self.assertEqual(r.messages[-1]["text"], "That plan risks the budget; here is a cheaper one.")
        self.assertIn("idea, not the person", a.prompts[1].lower())
        self.assertTrue(any(e["type"] == "moderated" for e in r.events))

    def test_a_second_attack_is_held_back(self):
        a = Voice("Raven", ["idiot", "still an idiot"])
        r = room.Room("t", seats(a), fmt="brainstorm", judge=Judge())
        r.round()
        self.assertNotIn("idiot", json.dumps(r.messages))
        self.assertTrue(r.messages[-1]["held"])


    def test_the_rephrase_retry_stays_inside_the_room_budget(self):
        # an external review (25.10): reply_cap 0.60, price 0.60, budget 1.00, the judge sends the reply back ->
        # one step() made two replies and spent 1.20
        a = Voice("Raven", ["idiot", "a fine rephrase"], cost=0.60)
        a.reply_cap = 0.60
        r = room.Room("t", seats(a), fmt="brainstorm", judge=Judge(), budget_usd=1.00)
        r.step()
        self.assertLessEqual(r.spent, 1.00 + 1e-9)
        self.assertEqual(len(a.prompts), 1)                    # no paid retry it could not afford
        self.assertTrue(r.messages[-1]["held"])
        self.assertNotIn("idiot", json.dumps(r.messages))

    def test_no_paid_summary_when_the_budget_is_used_up_even_without_a_cap(self):
        # a second review: no reply_cap (reserve 0), budget 1.00, one reply at 1.00, then end() paid a summary
        a = Voice("Raven", ["the one idea"], cost=1.00)
        r = room.Room("t", seats(a), fmt="brainstorm", budget_usd=1.00)
        r.step()
        r.end()
        self.assertLessEqual(r.spent, 1.00 + 1e-9)

    def test_the_retry_still_happens_when_it_fits(self):
        a = Voice("Raven", ["idiot", "a fine rephrase"], cost=0.30)
        a.reply_cap = 0.30
        r = room.Room("t", seats(a), fmt="brainstorm", judge=Judge(), budget_usd=1.00)
        r.step()
        self.assertEqual(r.messages[-1]["text"], "a fine rephrase")


class Levers(unittest.TestCase):
    def test_owner_words_go_into_the_room(self):
        a = Voice("Raven", ["x", "y"])
        r = room.Room("t", seats(a), fmt="brainstorm")
        r.inject("think about the price")
        r.round()
        self.assertEqual(r.messages[0]["speaker"], "Owner")
        self.assertIn("think about the price", a.prompts[0])

    def test_next_speaker_and_deeper(self):
        a, b = Voice("Raven", ["a1", "a2"]), Voice("Magpie", ["b1", "b2"])
        r = room.Room("t", seats(a, b), fmt="brainstorm")
        r.next_speaker("Magpie")
        r.deeper("the second idea")
        r.step()
        self.assertEqual(r.messages[-1]["speaker"], "Magpie")
        self.assertIn("the second idea", b.prompts[0])

    def test_pause_stops_steps(self):
        a = Voice("Raven", ["x"])
        r = room.Room("t", seats(a), fmt="brainstorm")
        r.pause()
        self.assertFalse(r.step())
        self.assertEqual(a.prompts, [])
        r.resume()
        self.assertTrue(r.step())

    def test_budget_ends_the_discussion(self):
        a = Voice("Raven", ["x"] * 10, cost=0.04)
        r = room.Room("t", seats(a), fmt="brainstorm", budget_usd=0.1)
        while r.step():
            pass
        self.assertEqual(len(a.prompts), 3)                      # 0.04, 0.08, 0.12 -> stop
        self.assertTrue(any(e["type"] == "budget" for e in r.events))
        self.assertAlmostEqual(r.spent, 0.12)


class Ending(unittest.TestCase):
    def test_end_writes_a_summary_and_a_record(self):
        with tempfile.TemporaryDirectory() as d:
            a = Voice("Raven", ["idea A", "summary: pick A"])
            r = room.Room("t", seats(a), fmt="brainstorm", record_dir=d)
            r.round()
            out = r.end(summarizer=a)
            self.assertIn("pick A", out)
            files = os.listdir(d)
            self.assertEqual(len(files), 1)
            with open(os.path.join(d, files[0]), encoding="utf-8") as f:
                rec = json.load(f)
            self.assertEqual(rec["topic"], "t")
            self.assertEqual(rec["summary"], out)

    def test_events_are_streamed_to_listeners(self):
        got = []
        a = Voice("Raven", ["x"])
        r = room.Room("t", seats(a), fmt="brainstorm")
        r.listen(got.append)
        r.round()
        self.assertEqual([e["type"] for e in got if e["type"] == "message"], ["message"])


if __name__ == "__main__":
    unittest.main()
