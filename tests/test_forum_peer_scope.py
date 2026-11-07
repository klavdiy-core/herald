"""Peer scope in a forum group: a peer bot works only where `task.peers` lists it, and its turn is an isolated one.
#2 effective permission = `peer_bots` (config) AND `task.peers` (tasks.json, per topic); missing or empty = silent, with one audit line.
   S1 in peer_bots, not in peers of topic A -> no engine turn, an audit line naming the bot id and the thread
   S2 task without `peers` -> no turn, an audit line
   S3 control: bound in peers of A (and in peer_bots) -> exactly one turn, answered in thread A
   S4 control: bound to topic B only -> a message in A starts nothing, in B starts a turn
   S5 in task.peers but not in peer_bots -> no turn
#6 a peer's turn is fresh and sees no topic file.
   I1 run with session None, never the topic's session
   I2 the peer's session id is not written back into the task
   I3a tools are exactly ["Read"]
   I3b the cwd is a fresh EMPTY folder (listed at call time), not the topic's folder and not inside it
   I4 control: the owner's turn keeps the topic session, its tools and the topic's folder
Run from the repo root: python -X utf8 -m unittest discover -s tests   (or: python -X utf8 tests/test_forum_peer_scope.py [repo-root])"""
import json, os, sys, unittest

ROOT = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
    sys.argv.pop(1)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_forum_tasks as T  # noqa: E402
from herald.engine import TurnResult  # noqa: E402

FILE_TOOLS = {"Read", "Glob", "Grep", "Write", "Edit"}     # I4 control: the owner keeps these


class EngineRec(T.Engine):
    def __call__(self, prompt, workspace, cfg, session_id=None):
        r = super().__call__(prompt, workspace, cfg, session_id=session_id)
        self.calls[-1]["tools"] = list(getattr(cfg, "tools", []) or [])
        cwd = self.calls[-1]["cwd"]
        self.calls[-1]["cwd_entries"] = sorted(os.listdir(cwd)) if cwd and os.path.isdir(cwd) else None     # what the turn can see, at call time
        return r


class Base(T.Base):
    def setUp(self):
        super().setUp()
        self.engine = EngineRec()

    def bind(self, thread, peers):
        """write the binding straight into tasks.json (the command that edits `peers` is a separate unit)"""
        d = self.tasks()
        d["tasks"][str(thread)]["peers"] = list(peers)
        with open(os.path.join(self.control, "tasks.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)

    def audit(self):
        p = os.path.join(self.control, "audit.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


class PeerScope(Base):
    def setUp(self):
        super().setUp()
        self.make(peer_bots=(T.PEER, T.PEER2), max_bot_hops=3, bot_reply_gap_s=0)
        self.a = self.new_task("topic a")
        self.b = self.new_task("topic b")
        self.engine.calls.clear()

    def test_S1_bot_in_peer_bots_but_not_in_the_topics_peers_starts_no_turn(self):
        self.bind(self.a, [T.PEER])
        self.peer(self.a, "hello from the other bot", who=T.PEER2)
        self.assertEqual(self.engine.calls, [], "S1: a bot not bound to topic A started a turn there")

    def test_S1b_the_refused_bot_is_audited_by_id_and_thread(self):
        self.bind(self.a, [T.PEER])
        self.peer(self.a, "hello from the other bot", who=T.PEER2)
        line = [l for l in self.audit().splitlines() if str(T.PEER2) in l and str(self.a) in l]
        self.assertTrue(line, "S1b: no audit.log line names bot %s and thread %s; log tail: %r" % (T.PEER2, self.a, self.audit()[-200:]))

    def test_S2_a_task_without_peers_lets_no_peer_in(self):
        self.peer(self.a, "hello", who=T.PEER)
        self.assertEqual(self.engine.calls, [], "S2: default must be empty peers = no peer turn")
        self.assertTrue([l for l in self.audit().splitlines() if str(T.PEER) in l and str(self.a) in l], "S2: no audit line")

    def test_S3_control_a_bound_bot_gets_exactly_one_turn_in_its_thread(self):
        self.bind(self.a, [T.PEER])
        self.bot.texts.clear()
        self.peer(self.a, "hello", who=T.PEER)
        self.assertEqual(len(self.engine.calls), 1, "S3: the bound bot must still work")
        self.assertEqual([(c, th) for c, _, th in self.bot.texts], [(T.FORUM, self.a)])

    def test_S4_control_bound_to_b_only_means_nothing_in_a_and_a_turn_in_b(self):
        self.bind(self.a, [])
        self.bind(self.b, [T.PEER])
        self.peer(self.a, "in a", who=T.PEER)
        self.assertEqual(self.engine.calls, [], "S4: bound to B only, but started a turn in A")
        self.peer(self.b, "in b", who=T.PEER)
        self.assertEqual(len(self.engine.calls), 1, "S4: bound to B, no turn in B")

    def test_S5_in_task_peers_but_not_in_peer_bots_gets_nothing(self):
        outsider = 7999
        self.bind(self.a, [T.PEER, outsider])
        self.peer(self.a, "hi", who=outsider)
        self.assertEqual(self.engine.calls, [], "S5: a bot outside peer_bots started a turn (the permission is the intersection)")


class PeerTurnIsolation(Base):
    def setUp(self):
        super().setUp()
        self.make(peer_bots=(T.PEER,), max_bot_hops=9, bot_reply_gap_s=0)
        self.a = self.new_task("topic a")
        self.bind(self.a, [T.PEER])          # bound, so these tests are about the turn itself (a no-op on today's code)
        self.owner(self.a, "owner talks first")     # gives the topic a session of its own
        self.engine.calls.clear()
        self.owner_session = self.tasks()["tasks"][str(self.a)]["session_id"]

    def test_I1_a_peer_turn_is_a_fresh_session(self):
        self.peer(self.a, "bot speaks", who=T.PEER)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertIsNone(self.engine.calls[-1]["session"], "I1: the peer turn continued the topic's session %r" % (self.engine.calls[-1]["session"],))

    def test_I2_the_peers_session_is_not_written_back_into_the_task(self):
        self.peer(self.a, "bot speaks", who=T.PEER)
        now = self.tasks()["tasks"][str(self.a)]["session_id"]
        self.assertEqual(now, self.owner_session, "I2: the task's session was replaced by the peer turn's session")

    def test_I3a_a_peer_turn_has_exactly_the_read_tool(self):
        self.peer(self.a, "bot speaks", who=T.PEER)
        self.assertEqual(self.engine.calls[-1]["tools"], ["Read"], "I3a: the peer turn tools are %s" % self.engine.calls[-1]["tools"])

    def test_I3b_a_peer_turn_runs_in_a_fresh_empty_folder_that_is_not_the_topics(self):
        self.owner(self.a, "owner again")
        topic = os.path.realpath(self.engine.calls[-1]["cwd"])
        self.peer(self.a, "bot speaks", who=T.PEER)
        c = self.engine.calls[-1]
        cwd = os.path.realpath(c["cwd"]) if c["cwd"] else ""
        self.assertTrue(c["cwd"] and c["cwd_entries"] is not None, "I3b: the peer turn has no existing cwd")
        self.assertEqual(c["cwd_entries"], [], "I3b: the peer's cwd is not empty (%d entries)" % len(c["cwd_entries"] or []))
        self.assertNotEqual(cwd, topic, "I3b: the peer turn runs in the topic's own folder")
        self.assertFalse(cwd.startswith(topic + os.sep), "I3b: the peer's cwd is inside the topic's folder")
        self.assertFalse(topic.startswith(cwd + os.sep), "I3b: the topic's folder lies inside the peer's cwd (it would see it by a relative path)")

    def test_I4_control_the_owners_turn_keeps_the_topic_session_and_tools(self):
        self.owner(self.a, "owner again")
        c = self.engine.calls[-1]
        self.assertEqual(c["session"], self.owner_session, "I4: the owner's turn lost the topic session")
        self.assertTrue(set(c["tools"]) & FILE_TOOLS, "I4: the owner's turn lost its tools")


if __name__ == "__main__":
    unittest.main(verbosity=1)
