"""Red tests: voice in a task topic gets one line instead of silence; the done topic is named "Done".
The forwarded-text point (1) is covered by the class Forwarded (W1-W7) in test_forum_tasks.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald.telegram import Message  # noqa: E402
import test_forum_tasks as T  # noqa: E402

LINE = "Text only in task topics; voice works in the private chat."


class VoiceInTopic(T.Base):
    def voice(self, thread, who=T.OWNER, bot=False):
        self.h.handle(Message(self._id(), T.FORUM, "voice", file_id="f1", thread_id=thread, from_id=who, from_bot=bot))

    def test_H4b_1_owner_voice_in_a_task_topic_gets_one_line_and_no_turn(self):
        self.make()
        a = self.new_task("fix login")
        self.bot.texts.clear()
        self.voice(a)
        self.assertEqual(self.said_in(a), [LINE])
        self.assertEqual(self.engine.calls, [])

    def test_H4b_2_voice_in_a_topic_nobody_opened_stays_silent(self):
        self.make()
        self.voice(777)
        self.assertEqual(self.said_in(777), [])
        self.assertEqual(self.engine.calls, [])

    def test_H4b_3_a_bot_voice_stays_silent(self):
        self.make()
        a = self.new_task("fix login")
        self.bot.texts.clear()
        self.voice(a, who=T.PEER, bot=True)
        self.assertEqual(self.said_in(a), [])

    def test_H4b_4_a_stranger_voice_stays_silent(self):
        self.make()
        a = self.new_task("fix login")
        self.bot.texts.clear()
        self.voice(a, who=424242)
        self.assertEqual(self.said_in(a), [])


class DoneTopicName(T.Base):
    def test_H4b_5_the_done_topic_is_named_Done(self):
        self.make()
        a = self.new_task("fix login")
        self.owner(a, "/done")
        self.tap(a, self.bot.buttons[-1][2][0][1])
        names = [t[1] for t in self.bot.of("create")]
        self.assertIn("Done", names)
        self.assertNotIn("Сделано", names)


if __name__ == "__main__":
    unittest.main()
