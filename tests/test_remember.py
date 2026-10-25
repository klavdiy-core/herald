"""The model may propose a memory mid-turn with [[remember: ...]]; nothing is kept until the owner taps Keep.
(Round 3 next to Hermes, 25.10: self-learning 6 / 9 - Hermes writes memory itself during a turn; herald keeps
the owner's say.) Red on 0.17.0."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import run  # noqa: E402
from test_buttons import tap  # noqa: E402
from test_run import Base, text  # noqa: E402


class Remember(Base):
    def answer(self, t):
        self.engine.answer = t
        self.h.handle(text("hi"))

    def test_a_proposal_is_offered_with_keep_and_drop_and_not_shown_raw(self):
        self.answer("Done.\n[[remember: The deploy script needs --dry-run first.]]")
        shown = " ".join(t for _, t in self.bot.texts) + " ".join(t for _, t, _ in self.bot.buttons)
        self.assertNotIn("[[remember", shown)
        self.assertIn("Done.", shown)
        chat, prompt, buttons = self.bot.buttons[-1]
        self.assertIn("The deploy script needs --dry-run first.", prompt)
        self.assertEqual([b[0] for b in buttons], ["Keep", "Drop"])
        self.assertEqual(self.h.nb.memory(), [])                  # nothing kept yet

    def test_keep_adds_it_to_memory_and_drop_does_not(self):
        self.answer("Ok.\n[[remember: Use port 8080 in tests.]]")
        keep = self.bot.buttons[-1][2][0][1]
        self.h.handle(tap(keep))
        self.assertTrue(any("Use port 8080 in tests." in m for m in self.h.nb.memory()))
        self.answer("Ok.\n[[remember: Something else.]]")
        drop = self.bot.buttons[-1][2][1][1]
        self.h.handle(tap(drop, uid=51))
        self.assertFalse(any("Something else." in m for m in self.h.nb.memory()))

    def test_a_keep_costs_no_turn(self):
        self.answer("Ok.\n[[remember: A fact.]]")
        n = len(self.engine.calls)
        self.h.handle(tap(self.bot.buttons[-1][2][0][1]))
        self.assertEqual(len(self.engine.calls), n)

    def test_at_most_three_and_each_capped(self):
        long = "x" * 1000
        self.answer("Ok.\n" + "".join(f"[[remember: fact {i} {long}]]\n" for i in range(5)))
        offers = [b for b in self.bot.buttons if b[2] and b[2][0][0] == "Keep"]
        self.assertEqual(len(offers), 3)
        self.assertTrue(all(len(p) < 600 for _, p, _ in offers))

    def test_a_known_memory_is_not_offered_again(self):
        self.h.nb.add_memory("- Use port 8080 in tests.")
        self.answer("Ok.\n[[remember: Use port 8080 in tests.]]")
        self.assertFalse([b for b in self.bot.buttons if b[2] and b[2][0][0] == "Keep"])

    def test_the_system_prompt_tells_the_model_how(self):
        self.assertIn("[[remember:", run.REMEMBER_HINT)


if __name__ == "__main__":
    unittest.main()
