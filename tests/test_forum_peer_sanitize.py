"""P9 #6 addition: a peer bot's turn has no tools, and a model without tools may print a fake tool call
(<invoke>/<parameter> blocks with the full path of its working folder). That text goes to the GROUP, so a peer's answer is
scrubbed before sending: tool-call blocks and the local workspace/cwd paths are cut. The owner's own topic answers stay as they are.
Run: python -X utf8 -m unittest tests.test_forum_peer_sanitize"""
import os
import unittest

import test_forum_tasks as T
from herald.engine import TurnResult


class Said(T.Engine):
    """the engine answers with the text set in `reply` (a callable of the workspace)"""
    reply = None

    def __call__(self, prompt, workspace, cfg, session_id=None):
        super().__call__(prompt, workspace, cfg, session_id=session_id)
        return TurnResult(True, self.reply(workspace), "s%d" % self.n, 0.25)


FAKE = ('Sure, let me look.\n<invoke name="Read">\n<parameter name="file_path">{p}</parameter>\n</invoke>\nDone.')


class PeerScrub(T.Base):
    auto_bind_peers = True

    def setUp(self):
        super().setUp()
        self.engine = Said()
        self.make(peer_bots=(T.PEER,), max_bot_hops=9, bot_reply_gap_s=0)
        self.a = self.new_task("topic a")
        self.bot.texts.clear()

    def sent(self):
        return "\n".join(t for _, t, th in self.bot.texts if th == self.a)

    def test_X1_a_fake_tool_call_block_is_cut_from_a_peer_answer(self):
        self.engine.reply = lambda ws: FAKE.format(p=os.path.join(ws, "note.txt"))
        self.peer(self.a, "hi", who=T.PEER)
        got = self.sent()
        self.assertNotIn("<invoke", got, "X1: a fake tool call went to the group: %r" % got)
        self.assertNotIn("<parameter", got)
        self.assertIn("Sure, let me look.", got, "X1: the honest text around the block must survive")

    def test_X2_the_local_workspace_path_never_reaches_the_group(self):
        self.engine.reply = lambda ws: "my folder is %s ok" % ws
        self.peer(self.a, "where are you", who=T.PEER)
        got = self.sent()
        self.assertNotIn(self.ws, got, "X2: the workspace path leaked: %r" % got)
        self.assertNotIn(self.tmp.name, got, "X2: a path under the temp root leaked: %r" % got)

    def test_X3_control_the_owners_turn_is_not_touched(self):
        self.engine.reply = lambda ws: "plain %s end" % ws
        self.owner(self.a, "owner asks")
        self.assertIn("plain", self.sent())
        self.assertIn(self.ws, self.sent(), "X3: the owner's answer was scrubbed too (only a peer's may be)")

    def test_X4_control_an_ordinary_peer_answer_arrives_unchanged(self):
        self.engine.reply = lambda ws: "just words, 2 < 3 and a <b>tag</b>"
        self.peer(self.a, "hi", who=T.PEER)
        self.assertIn("just words, 2 < 3 and a <b>tag</b>", self.sent())


if __name__ == "__main__":
    unittest.main(verbosity=1)
