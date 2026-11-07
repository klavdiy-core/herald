"""The tail's files: line is read from the disk, not from what the model says.
  F1 an owner turn that writes a.txt (and sub/b.md) in its task folder -> files: a.txt;sub/b.md
  F2 a turn that writes nothing -> files: is empty, even if the answer names a file
  F3 a file that was there before and did not change is not listed; a changed one is
  F4 a peer's turn writes no tail at all
"""
import os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_forum_tasks as T  # noqa: E402
from herald import forum  # noqa: E402
from herald.engine import TurnResult  # noqa: E402


class WritingEngine(T.Engine):
    def __init__(self):
        super().__init__()
        self.plan = []                                   # per call: list of (relative path, text) to write

    def __call__(self, prompt, workspace, cfg, session_id=None):
        writes = self.plan.pop(0) if self.plan else []
        for rel, body in writes:
            p = os.path.join(workspace, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
        self.n += 1
        self.calls.append({"prompt": prompt, "cwd": workspace, "session": session_id})
        return TurnResult(True, "I wrote note.txt and report.md for you.\nNext: check the numbers", f"s{self.n}", 0.1)


class TailFiles(T.Base):
    def setUp(self):
        super().setUp()
        self.engine = WritingEngine()
        self.make(peer_bots=[T.PEER])
        self.owner(0, "/task files")
        self.t = self.bot.of("create")[0][2]

    def files_line(self):
        _src, text = forum.read_tail(self.control, self.t)
        return next(ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("files:"))

    def test_F1_written_files_are_listed(self):
        self.engine.plan = [[("a.txt", "1"), ("sub/b.md", "2")]]
        self.owner(self.t, "write them")
        self.assertEqual(self.files_line(), "a.txt;sub/b.md")

    def test_F2_nothing_written_means_empty_even_if_the_answer_names_files(self):
        self.engine.plan = [[]]
        self.owner(self.t, "just talk")
        self.assertEqual(self.files_line(), "")

    def test_F3_unchanged_files_are_not_listed_changed_ones_are(self):
        self.engine.plan = [[("keep.txt", "same"), ("edit.txt", "v1")], [("edit.txt", "v2-longer")]]
        self.owner(self.t, "first")
        self.clock.t += 5
        self.owner(self.t, "second")
        self.assertEqual(self.files_line(), "edit.txt")

    def test_F4_a_peer_turn_writes_no_tail(self):
        self._bind_peer(self.t, T.PEER)
        self.engine.plan = [[("x.txt", "peer")]]
        self.peer(self.t, "hello from a bot")
        self.assertEqual(forum.read_tail(self.control, self.t), ("none", None))


if __name__ == "__main__":
    unittest.main()
