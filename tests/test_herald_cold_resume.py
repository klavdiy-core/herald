"""«A minute after the light»: after a power cut every topic knows where it stopped, from DISK ALONE, in under a minute.
Interface (fixed by its owner): herald.forum.write_tail(control, thread, fields) writes <control>/tails/<thread>.md (tmp + fsync + replace + fsync of the folder,
the previous generation kept next to it as <thread>.md.prev); Forum._turn_in writes it at the end of an OWNER's turn, after the answer went to the topic, never
for a peer's turn; Herald.resume_report() -> {thread: {"tail": str|None, "age_s": float, "source": "tail"|"prev"|"none"}}, built from disk only (no network, no engine).
Tail lines, one each, these keys: done: next: files: turn_at: session:   (files: = paths inside the workspace taken from the turn's tool use, never from prose;
next: = the last answer line starting with "Next:" or "Дальше:", else the owner's last message cut to 200 characters; turn_at: ISO UTC)
  T1  after an owner's turn the tail exists with the five keys, and `next:` is not empty
  T2  after a second turn the previous generation exists next to it
  F1  a turn that writes a.txt in the topic folder gives `files: a.txt`        F2  a turn that writes nothing gives an empty `files:`
  F3  a file that already lay in the topic folder and was not touched is not listed; F4 a nested file is listed workspace-relative with `/` (sub/b.txt)
  F6  a file that already lay there and was REWRITTEN in the turn is listed
  F5  a file written OUTSIDE the topic folder (the workspace root, the control folder) is never listed
  (files: is the herald's own diff of the topic folder before and after the turn - path, mtime, size - not tool use and not prose)
  T3  a peer's turn writes no tail
  N1  an answer ending with "Next: <x>" gives `next: <x>`        N2  no such line -> the owner's last message, at most 200 characters
  R1  a real child process writes tails in a loop and, once its first tail exists, is killed (TerminateProcess) at 8 different moments: after each, resume_report has source tail or prev and
      the tail holds all five keys                                                                                       (8 trials)
  R7  deterministic crash points in a child process, on the SECOND write (the first generation is already on disk): the write of any .md/.tmp file is torn
      in the middle and the process dies; or the process dies before the 2nd or the 3rd os.replace. After each: source tail or prev, five keys   (3 points)
  R2  tail cut to zero bytes: source == "prev", a non-empty tail, and an audit.log line
  R3  at resume the engine and the bot are bombs that fail on any call: the report is still built
  R4  30 topics: a restart + resume_report takes at most 60 s (the number is printed)
  R5  control: a normal restart gives source "tail" and a small age_s
  R6  a path of the answer's PROSE outside the workspace is not in `files:`, and no tail line holds a private-layer path (by path, not by word)
Run from the repo root: python -X utf8 tests/test_herald_cold_resume.py [repo-root]"""
import json, os, subprocess, sys, tempfile, time, unittest

ROOT = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
    sys.argv.pop(1)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_forum_tasks as T  # noqa: E402
from herald import forum as forum_mod  # noqa: E402
from herald.engine import TurnResult  # noqa: E402

KEYS = ("done:", "next:", "files:", "turn_at:", "session:")


def parse(text):
    out = {}
    for line in (text or "").splitlines():
        for k in KEYS:
            if line.startswith(k):
                out[k] = line[len(k):].strip()
    return out


class Bomb:
    def __call__(self, *a, **k):
        raise AssertionError("touched at resume")

    def __getattr__(self, name):
        raise AssertionError("touched at resume: " + name)


class Say(T.Engine):
    """answers with a scripted text; `writes` = relative paths created in the turn's folder, `outside` = absolute paths written elsewhere"""
    text = None
    writes = ()
    outside = ()

    def __call__(self, prompt, workspace, cfg, session_id=None):
        r = super().__call__(prompt, workspace, cfg, session_id=session_id)
        for rel in self.writes:
            p = os.path.join(workspace, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("turn %d" % self.n)
            os.utime(p, (time.time() + 5 * self.n, time.time() + 5 * self.n))      # a changed mtime even when the clock has not moved
        for p in self.outside:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("outside")
        if self.text:
            return TurnResult(True, self.text, r.session_id, r.cost_usd)
        return r


class Base(T.Base):
    def setUp(self):
        super().setUp()
        self.engine = Say()
        self.make(peer_bots=(T.PEER,), max_bot_hops=9, bot_reply_gap_s=0)
        self.a = self.new_task("topic a")
        self.tails = os.path.join(self.control, "tails")

    def tail_path(self, thread=None):
        return os.path.join(self.tails, "%s.md" % (thread or self.a))

    def read_tail(self, thread=None):
        p = self.tail_path(thread)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else None

    def restart(self):
        self.make(peer_bots=(T.PEER,), max_bot_hops=9, bot_reply_gap_s=0)
        return self.h.resume_report()

    def audit(self):
        p = os.path.join(self.control, "audit.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


class TailWritten(Base):
    def test_T1_an_owners_turn_writes_the_tail_with_five_keys(self):
        self.owner(self.a, "please fix the login")
        t = self.read_tail()
        self.assertIsNotNone(t, "T1: no tail file after an owner's turn")
        got = parse(t)
        self.assertEqual(sorted(got), sorted(KEYS), "T1: keys found %s" % sorted(got))
        self.assertTrue(got["next:"], "T1: next: is empty")

    def test_T2_the_previous_generation_is_kept(self):
        self.owner(self.a, "first")
        self.owner(self.a, "second")
        self.assertTrue(os.path.exists(self.tail_path() + ".prev"), "T2: no .prev next to the tail after two turns")

    def test_T3_a_peers_turn_writes_no_tail(self):
        d = self.tasks()
        d["tasks"][str(self.a)]["peers"] = [T.PEER]
        with open(os.path.join(self.control, "tasks.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.peer(self.a, "bot speaks", who=T.PEER)
        self.assertIsNone(self.read_tail(), "T3: a peer's turn wrote a tail")

    def test_N1_next_is_the_last_Next_line_of_the_answer(self):
        self.engine.text = "Done the thing.\nNext: run the migration on staging"
        self.owner(self.a, "go")
        self.assertEqual(parse(self.read_tail() or "").get("next:"), "run the migration on staging")

    def test_N2_without_a_Next_line_it_is_the_owners_last_message_cut_to_200(self):
        self.engine.text = "Just an answer, no plan line."
        msg = "x" * 300
        self.owner(self.a, msg)
        n = parse(self.read_tail() or "").get("next:", "")
        self.assertTrue(n and len(n) <= 200 and n == msg[:200], "N2: next: is %d chars" % len(n))

    def files_of(self, **kw):
        for k, v in kw.items():
            setattr(self.engine, k, v)
        self.owner(self.a, "work")
        return [x.strip() for x in parse(self.read_tail() or "").get("files:", "").split(";") if x.strip()]

    def test_F1_a_written_file_is_listed(self):
        self.assertEqual(self.files_of(writes=["a.txt"]), ["a.txt"])

    def test_F2_no_write_gives_an_empty_list(self):
        self.assertEqual(self.files_of(), [])

    def test_F3_an_untouched_existing_file_is_not_listed(self):
        d = os.path.join(self.ws, "tasks", str(self.a))
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "old.txt"), "w").write("old")
        self.assertEqual(self.files_of(writes=["new.txt"]), ["new.txt"])

    def test_F6_a_rewritten_existing_file_is_listed(self):
        d = os.path.join(self.ws, "tasks", str(self.a))
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "old.txt"), "w").write("old")
        self.assertEqual(self.files_of(writes=["old.txt"]), ["old.txt"])

    def test_F4_a_nested_file_is_workspace_relative_with_forward_slashes(self):
        self.assertEqual(self.files_of(writes=["sub/b.txt"]), ["sub/b.txt"])

    def test_F5_a_file_outside_the_topic_folder_is_never_listed(self):
        out = [os.path.join(self.ws, "elsewhere.txt"), os.path.join(self.control, "elsewhere2.txt")]
        got = self.files_of(writes=["a.txt"], outside=out)
        self.assertEqual(got, ["a.txt"], "F5: files: holds %s" % got)

    def test_R6_prose_paths_outside_the_workspace_never_reach_files_or_any_line(self):
        outside = "C:\\elsewhere\\private\\notes\\x.md"
        self.engine.text = "I looked at %s and at /etc/passwd.\nNext: nothing" % outside
        self.owner(self.a, "go")
        t = self.read_tail() or ""
        self.assertTrue(t, "R6: no tail")
        self.assertNotIn("private", t.lower(), "R6: a private-layer path is in the tail")
        self.assertNotIn("/etc/passwd", t, "R6: a prose path is in the tail")
        for p in [x.strip() for x in parse(t).get("files:", "").split(";") if x.strip()]:
            self.assertTrue(not os.path.isabs(p) or os.path.realpath(p).startswith(os.path.realpath(self.ws)), "R6: files: holds %r outside the workspace" % p)


class Resume(Base):
    def test_R5_control_a_normal_restart_reads_the_tail(self):
        self.owner(self.a, "work")
        rep = self.restart().get(str(self.a)) or {}
        self.assertEqual(rep.get("source"), "tail", "R5: source %r" % rep.get("source"))
        self.assertTrue(rep.get("tail") and 0 <= rep.get("age_s", -1) < 600, "R5: age_s %r" % rep.get("age_s"))

    def test_R2_a_zero_length_tail_falls_back_to_prev_loudly(self):
        self.owner(self.a, "first")
        self.owner(self.a, "second")
        self.assertTrue(os.path.exists(self.tail_path()), "R2: no tail file to cut")
        open(self.tail_path(), "w").close()
        rep = self.restart().get(str(self.a)) or {}
        self.assertEqual(rep.get("source"), "prev", "R2: source %r" % rep.get("source"))
        self.assertTrue(rep.get("tail"), "R2: the report has no tail")
        self.assertTrue([l for l in self.audit().splitlines() if str(self.a) in l and "tail" in l.lower()], "R2: no audit line about the tail")

    def test_R3_resume_touches_neither_the_engine_nor_the_bot(self):
        self.owner(self.a, "work")
        self.engine, self.bot = Bomb(), Bomb()
        self.make(peer_bots=(T.PEER,), max_bot_hops=9, bot_reply_gap_s=0)
        rep = self.h.resume_report()
        self.assertIn(str(self.a), rep, "R3: no report for the topic")

    def test_R4_thirty_topics_in_under_a_minute(self):
        ids = [self.a] + [self.new_task("topic %d" % i) for i in range(29)]
        for th in ids:
            self.owner(th, "work on %s" % th)
        t0 = time.time()
        rep = self.restart()
        dt = time.time() - t0
        print("R4: %d topics resumed in %.2f s" % (len(rep), dt))
        self.assertGreaterEqual(len([1 for th in ids if (rep.get(str(th)) or {}).get("source") in ("tail", "prev")]), 30, "R4: not every topic has a tail")
        self.assertLessEqual(dt, 60, "R4: %.1f s" % dt)

    def test_R7_crash_points_in_the_second_write(self):
        child = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cold_resume_crash_child.py")
        bad = []
        for mode in ("torn", "rep2", "rep3"):
            for f in os.listdir(self.tails) if os.path.isdir(self.tails) else []:
                os.remove(os.path.join(self.tails, f))
            p = subprocess.run([sys.executable, "-X", "utf8", child, ROOT, self.control, str(self.a), mode], capture_output=True, text=True, timeout=60)
            if "AttributeError" in p.stderr or "ImportError" in p.stderr:
                self.fail("R7: the interface herald.forum.write_tail does not exist")
            rep = self.restart().get(str(self.a)) or {}
            if not (rep.get("source") in ("tail", "prev") and sorted(parse(rep.get("tail"))) == sorted(KEYS)):
                bad.append((mode, rep.get("source"), p.returncode))
        self.assertEqual(bad, [], "R7: unreadable after a crash at %s" % bad)

    def test_R1_a_killed_writer_leaves_a_readable_generation(self):
        child = ("import sys, itertools\nsys.path.insert(0, %r)\nfrom herald import forum\n"
                 "for i in itertools.count():\n"
                 "    forum.write_tail(%r, %r, {'done': 'step %%d' %% i, 'next': 'keep going', 'files': '', 'turn_at': '2026-11-07T10:00:00Z', 'session': 's%%d' %% i})\n") % (
            ROOT, self.control, str(self.a))
        bad = []
        for k, delay in enumerate([0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.6]):
            p = subprocess.Popen([sys.executable, "-X", "utf8", "-c", child], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            t0 = time.time()
            while not os.path.exists(self.tail_path()) and time.time() - t0 < 15 and p.poll() is None:
                time.sleep(0.02)            # the child needs its start-up time: a kill BEFORE the first write leaves nothing, and nothing is not a failure
            time.sleep(delay)
            p.kill()
            err = p.communicate()[1].decode("utf-8", "replace")
            if "AttributeError" in err or "ImportError" in err:
                self.fail("R1: the interface herald.forum.write_tail does not exist")
            rep = self.restart().get(str(self.a)) or {}
            ok = rep.get("source") in ("tail", "prev") and sorted(parse(rep.get("tail"))) == sorted(KEYS)
            if not ok:
                bad.append((k, rep.get("source")))
        self.assertEqual(bad, [], "R1: unreadable after a kill in %d of 8 trials: %s" % (len(bad), bad))


if __name__ == "__main__":
    unittest.main(verbosity=1)
