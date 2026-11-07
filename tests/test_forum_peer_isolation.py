"""Peer turn isolation: a peer's turn runs in a fresh EMPTY folder outside the topic folder, the workspace and control,
and its answer is stripped of any absolute path (its own cwd included). The owner's turn keeps the topic folder.
  A1 a peer turn's cwd is not the topic folder, not inside workspace/control, and is empty while the turn runs
  A2 every peer turn gets its own folder, and it is gone after the turn
  A3 control: the owner's turn runs in the topic folder
  A4 the peer's cwd and any other absolute path (Windows or POSIX) never reach the group
  A5 control: relative paths and ordinary text in a peer answer arrive unchanged
Run from the repo root: python -X utf8 -m unittest discover -s tests"""
import os
import unittest

import test_forum_tasks as T
from herald.engine import TurnResult


class Rec(T.Engine):
    reply = None

    def __call__(self, prompt, workspace, cfg, session_id=None):
        super().__call__(prompt, workspace, cfg, session_id=session_id)
        self.calls[-1]["listing"] = os.listdir(workspace) if os.path.isdir(workspace) else None
        text = self.reply(workspace) if self.reply else "ok"
        return TurnResult(True, text, "s%d" % self.n, 0.25)


class Iso(T.Base):
    auto_bind_peers = True

    def setUp(self):
        super().setUp()
        self.engine = Rec()
        self.make(peer_bots=(T.PEER,), max_bot_hops=9, bot_reply_gap_s=0)
        self.a = self.new_task("topic a")
        self.bot.texts.clear()
        self.engine.calls.clear()

    def sent(self):
        return "\n".join(t for _, t, th in self.bot.texts if th == self.a)

    def inside(self, p, root):
        p, root = os.path.normcase(os.path.realpath(p)), os.path.normcase(os.path.realpath(root))
        return p == root or p.startswith(root + os.sep)

    def test_A1_peer_cwd_is_a_fresh_empty_folder_outside_the_topic(self):
        self.peer(self.a, "hi", who=T.PEER)
        c = self.engine.calls[-1]
        self.assertFalse(self.inside(c["cwd"], self.ws), "A1: the peer ran inside the workspace: %r" % c["cwd"])
        self.assertFalse(self.inside(c["cwd"], self.control), "A1: the peer ran inside control")
        self.assertEqual(c["listing"], [], "A1: the peer's folder was not empty: %r" % c["listing"])

    def test_A2_each_peer_turn_has_its_own_folder_removed_after(self):
        self.peer(self.a, "one", who=T.PEER)
        self.peer(self.a, "two", who=T.PEER)
        c1, c2 = self.engine.calls[-2]["cwd"], self.engine.calls[-1]["cwd"]
        self.assertNotEqual(c1, c2, "A2: two peer turns shared a folder")
        self.assertFalse(os.path.exists(c1) or os.path.exists(c2), "A2: a peer's folder was left behind")

    def test_A3_control_the_owner_runs_in_the_topic_folder(self):
        self.owner(self.a, "owner")
        c = self.engine.calls[-1]
        self.assertTrue(self.inside(c["cwd"], self.ws), "A3: the owner left the workspace: %r" % c["cwd"])

    def test_A4_no_absolute_path_reaches_the_group(self):
        self.engine.reply = lambda ws: "I am in %s, also C:\\Users\\Admin\\secret\\a.txt and /home/x/y/z.txt end" % ws
        self.peer(self.a, "where", who=T.PEER)
        got = self.sent()
        self.assertNotIn(self.engine.calls[-1]["cwd"], got, "A4: the peer's cwd leaked: %r" % got)
        self.assertNotIn("C:\\Users", got, "A4: a Windows path leaked: %r" % got)
        self.assertNotIn("/home/x", got, "A4: a POSIX path leaked: %r" % got)
        self.assertIn("end", got)

    def test_A5_control_relative_text_is_unchanged(self):
        self.engine.reply = lambda ws: "see docs/a.md, 3/4 done, and a/b"
        self.peer(self.a, "hi", who=T.PEER)
        self.assertIn("see docs/a.md, 3/4 done, and a/b", self.sent())


class Ints(T.Base):
    """task.peers binds only real ints: a float, a string, a dict, a username or a bool-looking value binds nobody"""

    def setUp(self):
        super().setUp()
        self.engine = T.Engine()
        self.make(peer_bots=(T.PEER,), max_bot_hops=9, bot_reply_gap_s=0)
        self.a = self.new_task("topic a")
        self.engine.calls.clear()

    def bind_raw(self, value):
        import json
        d = self.tasks()
        d["tasks"][str(self.a)]["peers"] = value
        with open(os.path.join(self.control, "tasks.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_B1_only_a_real_int_binds(self):
        for bad in ([float(T.PEER)], [str(T.PEER)], ["@bot"], [{"id": T.PEER}], str(T.PEER), {"id": T.PEER}, [None], [True]):
            self.engine.calls.clear()
            self.bind_raw(bad)
            self.peer(self.a, "hi", who=T.PEER)
            self.assertEqual(self.engine.calls, [], "B1: %r bound a bot" % (bad,))

    def test_B2_control_a_real_int_binds(self):
        self.bind_raw([T.PEER])
        self.peer(self.a, "hi", who=T.PEER)
        self.assertEqual(len(self.engine.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
