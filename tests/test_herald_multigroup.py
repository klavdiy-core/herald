"""Several groups with their own members in one herald: red tests G1-G12 BEFORE the code (design: groups.json, one state folder per group).
Interface (agreed with its designer): Herald(..., groups_file=<path to groups.json>) and the same key in config; groups are forums like today's forum group.
  groups.json: {"her_id": <id>, "groups": [{"chat_id", "name", "kind": "hall"|"train", "owners": [ids], "peer_bots": [ids], "persona": <file>, "notes": ""}]}
  the state of a group lives in control/groups/<chat_id>/ (tasks.json, learned/, system.md, STOP, herald.json, tails/); its tasks' folders under workspace/groups/<chat_id>/
  control/STOP stays the general stop; groups/<id>/STOP stops that group only. her_id is top level, NOT in owners; owners = the lead only; her rights are above an owner's.
  /resume lifts a stop (/go is an alias); a line starting with `!` from her is the owner's command (`!stop` = /stop, `!task x` = /task x); `!veto` in a task topic closes
  it, a running turn's answer is not posted, no new turn starts.
G1  a lesson approved in train A (its learned/) is not in the system prompt of a turn in train B; control: it IS in A's own prompt
G2  /stop in A does not stop B; the general control/STOP stops both
G3  /model in A does not change the model of a turn in B
G4  a task opened in A is not listed by /tasks in B; a turn in B runs in B's folder, never in A's; A's turn runs in A's
G5  two turns in a row, A then B: B's system prompt holds no line of A's persona (and the other way); control: each holds its own
G6  migration: an old setup with one forum_chat_id, a non-empty tasks.json and a STOP file: the same tasks listed, the same session continued, the same STOP in force
G7  groups.json with an unknown key, or a chat_id that is not a number: the start is refused with a line that names it
G8  her /stop in A stops A; the lead's /resume (and /go) does NOT lift it; her /resume does
G9  her plain line in a task topic starts no turn, and is in no learned/ and no system.md of a worker's turn
G10 her `!task x` is executed as the owner's /task x, her `!stop` as /stop; the same lines from a stranger do nothing
G11 her `!veto` closes the task at once, even in the middle of a turn: the turn's answer is not posted and no new turn starts; a task of the same name in B is untouched
G12 a hall holds no tasks and no notebook: /task in a hall creates no topic, an owner's line in a hall starts no turn
Fixture: three groups (a hall, trains A and B), two different ids for the lead and for her (made up). Mutants (for the code, when it exists): her stop = an ordinary stop (G8),
her line = a turn (G9), notebook shared (G1), STOP shared (G2), system.md shared (G5).
Run from the repo root: python -X utf8 tests/test_herald_multigroup.py [repo-root]"""
import json, os, sys, tempfile, unittest

ROOT = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
    sys.argv.pop(1)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from herald import notebook, run, speak_first  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from herald.telegram import Message  # noqa: E402

LEAD, HER, STRANGER = 1001, 5005, 2002
HALL, GA, GB = -1001000000001, -1001000000002, -1001000000003


class Bot:
    def __init__(self):
        self.texts, self.topics, self.closed, self._next = [], [], [], 100
        self.script, self.after_script = [], None

    def send_text(self, chat_id, text, thread_id=None):
        self.texts.append((chat_id, text, thread_id))

    def send_buttons(self, chat_id, text, buttons, thread_id=None):
        self.texts.append((chat_id, text, thread_id))

    def answer_callback(self, *a, **k):
        pass

    def send_voice(self, *a, **k):
        pass

    def send_action(self, *a, **k):
        pass

    def create_topic(self, name, color=None, chat_id=None, *a, **k):
        self._next += 1
        self.topics.append((chat_id, name, self._next))
        return self._next

    def edit_topic(self, *a, **k):
        pass

    def close_topic(self, *a, **k):
        self.closed.append((a, k))

    def reopen_topic(self, *a, **k):
        pass

    def poll(self, offset, timeout=50):
        if self.script:                                    # a watcher test scripts what arrives during a turn
            batch = self.script.pop(0)
            if not self.script and self.after_script:
                self.after_script()
            return batch, offset + len(batch)
        return [], offset


class Engine:
    """records, at call time: the system prompt text, the model, the cwd, the session; `during` is a hook run inside the turn"""
    def __init__(self):
        self.calls, self.n, self.during = [], 0, None

    def __call__(self, prompt, workspace, cfg, session_id=None):
        self.n += 1
        sp = getattr(cfg, "append_system_prompt_file", "") or ""
        text = open(sp, encoding="utf-8").read() if sp and os.path.exists(sp) else ""
        self.calls.append({"sp_path": sp, "prompt": prompt, "cwd": workspace, "session": session_id, "model": getattr(cfg, "model", None), "system": text})
        if self.during:
            hook, self.during = self.during, None
            hook()
        return TurnResult(True, "answer %d" % self.n, "s%d" % self.n, 0.1)


class World(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control, self.ws = os.path.join(self.tmp.name, "c"), os.path.join(self.tmp.name, "w")
        os.makedirs(self.control)
        os.makedirs(self.ws)
        self.uid, self.bot, self.engine = 0, Bot(), Engine()
        self.persona = {g: os.path.join(self.tmp.name, "persona_%d.md" % abs(g)) for g in (GA, GB)}
        for g, p in self.persona.items():
            open(p, "w", encoding="utf-8").write("PERSONA-OF-%d line one\nPERSONA-OF-%d line two\n" % (abs(g), abs(g)))
        self.groups_path = os.path.join(self.control, "groups.json")
        self.write_groups()
        self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def groups_dict(self, extra_keys=None, bad_chat=None):
        g = [{"chat_id": HALL, "name": "hall", "kind": "hall", "owners": [LEAD], "peer_bots": [], "persona": "", "notes": ""},
             {"chat_id": GA, "name": "train a", "kind": "train", "owners": [LEAD], "peer_bots": [], "persona": self.persona[GA], "notes": ""},
             {"chat_id": GB, "name": "train b", "kind": "train", "owners": [LEAD], "peer_bots": [], "persona": self.persona[GB], "notes": ""}]
        d = {"her_id": HER, "groups": g}
        if extra_keys:
            d.update(extra_keys)
        if bad_chat is not None:
            g[1]["chat_id"] = bad_chat
        return d

    def write_groups(self, **kw):
        json.dump(self.groups_dict(**kw), open(self.groups_path, "w", encoding="utf-8"))

    def make(self, legacy=None):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"), os.path.join(self.control, "STOP_GATE"))
        args = (self.bot, self.engine, EngineConfig(), gate, [], self.control, self.ws, {LEAD})
        extra = {"forum_chat_id": legacy} if legacy is not None else {}
        try:
            self.h = run.Herald(*args, groups_file=self.groups_path, transcript_roots=[], room_sync=True, now=lambda: 1000.0, **extra)
        except TypeError as e:
            self.fail("interface: Herald(groups_file=...) is not accepted (%s)" % e)
        return self.h

    # ---- messages ----
    def say(self, chat, who, text, thread=0):
        self.uid += 1
        self.h.handle(Message(self.uid, chat, "text", text=text, thread_id=thread, from_id=who))

    def lead(self, chat, text, thread=0):
        self.say(chat, LEAD, text, thread)

    def her(self, chat, text, thread=0):
        self.say(chat, HER, text, thread)

    def task(self, chat, name):
        self.lead(chat, "/task " + name)
        made = [t for t in self.bot.topics if t[1] == name]
        return made[-1][2] if made else None

    # ---- state ----
    def gdir(self, chat):
        return os.path.join(self.control, "groups", str(chat))

    def said(self, chat, thread=None):
        return [t for c, t, th in self.bot.texts if c == chat and (thread is None or th == thread)]

    def turns(self):
        return len(self.engine.calls)

    def stopped(self, chat):
        """a turn in a fresh task of `chat` does not reach the engine"""
        th = self.task(chat, "probe %d" % self.uid)
        before = self.turns()
        self.lead(chat, "go", th)
        return self.turns() == before


class Isolation(World):
    def test_G1_a_lesson_of_A_is_not_in_the_prompt_of_B(self):
        a, b = self.task(GA, "alpha"), self.task(GB, "beta")
        os.makedirs(os.path.join(self.gdir(GA), "learned"), exist_ok=True)
        notebook.Notebook(os.path.join(self.gdir(GA), "learned")).add_memory("LESSON-ONLY-FOR-A-71c3")
        self.lead(GA, "work", a)
        self.assertIn("LESSON-ONLY-FOR-A-71c3", self.engine.calls[-1]["system"], "G1 control: A's lesson is not even in A's own prompt")
        self.lead(GB, "work", b)
        self.assertNotIn("LESSON-ONLY-FOR-A-71c3", self.engine.calls[-1]["system"], "G1: A's lesson reached B's prompt")

    def test_G2_stop_in_A_does_not_stop_B_but_the_general_stop_stops_both(self):
        self.lead(GA, "/stop", 5)
        self.assertTrue(self.stopped(GA), "G2 control: /stop in A did not stop A")
        self.assertFalse(self.stopped(GB), "G2: /stop in A stopped B")
        open(os.path.join(self.control, "STOP"), "w").close()
        self.assertTrue(self.stopped(GA) and self.stopped(GB), "G2: the general STOP did not stop both")

    def test_G3_model_in_A_does_not_change_B(self):
        a, b = self.task(GA, "alpha"), self.task(GB, "beta")
        self.lead(GA, "/model claude-haiku-5-5")
        self.lead(GA, "work", a)
        ma = self.engine.calls[-1]["model"]
        self.lead(GB, "work", b)
        mb = self.engine.calls[-1]["model"]
        self.assertEqual(ma, "claude-haiku-5-5", "G3 control: /model did not take in A (%r)" % ma)
        self.assertNotEqual(mb, "claude-haiku-5-5", "G3: B runs on A's model")

    def test_G4_tasks_and_folders_are_per_group(self):
        a, b = self.task(GA, "only-in-alpha"), self.task(GB, "beta")
        self.bot.texts.clear()
        self.lead(GB, "/tasks")
        self.assertNotIn("only-in-alpha", "\n".join(self.said(GB)), "G4: B lists A's task")
        self.lead(GA, "work", a)
        self.lead(GB, "work", b)
        ca, cb = [os.path.realpath(c["cwd"]) for c in self.engine.calls[-2:]]
        ga, gb = os.path.realpath(os.path.join(self.ws, "groups", str(GA))), os.path.realpath(os.path.join(self.ws, "groups", str(GB)))
        self.assertTrue(ca.startswith(ga + os.sep), "G4: A's turn runs outside A's folder: %s" % ca)
        self.assertTrue(cb.startswith(gb + os.sep) and not cb.startswith(ga), "G4: B's turn runs outside B's folder or inside A's: %s" % cb)

    def test_G5_system_md_does_not_cross(self):
        a, b = self.task(GA, "alpha"), self.task(GB, "beta")
        self.lead(GA, "work", a)
        self.lead(GB, "work", b)
        sa, sb = self.engine.calls[-2]["system"], self.engine.calls[-1]["system"]
        self.assertIn("PERSONA-OF-%d" % abs(GA), sa, "G5 control: A's prompt lacks A's persona")
        self.assertIn("PERSONA-OF-%d" % abs(GB), sb, "G5 control: B's prompt lacks B's persona")
        self.assertNotIn("PERSONA-OF-%d" % abs(GA), sb, "G5: A's persona is in B's prompt")
        self.assertNotIn("PERSONA-OF-%d" % abs(GB), sa, "G5: B's persona is in A's prompt")
        pa, pb = self.engine.calls[-2]["sp_path"], self.engine.calls[-1]["sp_path"]
        self.assertNotEqual(os.path.realpath(pa), os.path.realpath(pb), "G5: the two groups share ONE system-prompt file (two turns at once would swap prompts)")
        self.assertIn("PERSONA-OF-%d" % abs(GA), open(pa, encoding="utf-8").read(), "G5: A's system-prompt file was overwritten by B's turn")

    def test_G12_a_hall_has_no_tasks_and_no_turns(self):
        before = len(self.bot.topics)
        self.lead(HALL, "/task something")
        self.assertEqual(len(self.bot.topics), before, "G12: /task in a hall created a topic")
        n = self.turns()
        self.lead(HALL, "hello hall", 7)
        self.assertEqual(self.turns(), n, "G12: a line in a hall started a turn")


class Migration(World):
    def test_G6_an_old_setup_survives(self):
        """the old layout: one forum_chat_id, tasks.json and STOP in control/ itself"""
        old = {"tasks": {"101": {"name": "old task", "session_id": "s-old", "state": "open"}}, "done_topic": None}
        json.dump(old, open(os.path.join(self.control, "tasks.json"), "w", encoding="utf-8"))
        os.remove(self.groups_path)
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"), os.path.join(self.control, "STOP_GATE"))
        self.h = run.Herald(self.bot, self.engine, EngineConfig(), gate, [], self.control, self.ws, {LEAD}, forum_chat_id=GA,
                            transcript_roots=[], room_sync=True, now=lambda: 1000.0)
        self.lead(GA, "/tasks")
        self.assertIn("old task", "\n".join(self.said(GA)), "G6: the old task is not listed after the update")
        n = self.turns()
        self.lead(GA, "continue", 101)
        self.assertEqual(self.turns(), n + 1, "G6: no turn in the old task")
        self.assertEqual(self.engine.calls[-1]["session"], "s-old", "G6: the old session was not continued")
        open(os.path.join(self.control, "STOP"), "w").close()
        n = self.turns()
        self.lead(GA, "again", 101)
        self.assertEqual(self.turns(), n, "G6: the old STOP is not in force")


class Config(World):
    def test_G7_unknown_key_refuses_to_start(self):
        for kw in ({"extra_keys": {"surprise": 1}},):
            self.write_groups(**kw)
            with self.assertRaises(ValueError) as cm:
                self.make()
            self.assertIn("surprise", str(cm.exception), "G7: the refusal does not name the key")

    def test_G7b_chat_id_not_a_number_refuses_to_start(self):
        self.write_groups(bad_chat="not-a-number")
        with self.assertRaises(ValueError) as cm:
            self.make()
        self.assertIn("chat_id", str(cm.exception), "G7b: the refusal does not name chat_id")


class Her(World):
    def setUp(self):
        super().setUp()
        self.a = self.task(GA, "alpha")
        self.engine.calls.clear()

    def blocked(self, chat, th):
        n = self.turns()
        self.lead(chat, "go", th)
        return self.turns() == n

    def test_G8_her_stop_is_lifted_only_by_her(self):
        self.her(GA, "/stop", self.a)
        self.assertTrue(self.blocked(GA, self.a), "G8 control: her /stop did not stop the group")
        self.lead(GA, "/resume", self.a)
        self.lead(GA, "/go", self.a)
        self.assertTrue(self.blocked(GA, self.a), "G8: the lead's /resume or /go lifted her stop")
        self.her(GA, "/resume", self.a)
        self.assertFalse(self.blocked(GA, self.a), "G8: her own /resume did not lift her stop")

    def test_G8b_control_the_leads_stop_is_lifted_by_the_lead(self):
        self.lead(GA, "/stop", self.a)
        self.assertTrue(self.blocked(GA, self.a))
        self.lead(GA, "/resume", self.a)
        self.assertFalse(self.blocked(GA, self.a), "G8b: the lead's own stop was not lifted by the lead (the rule must not be 'nobody lifts')")

    def test_G9_her_plain_line_is_a_participant_not_a_trigger(self):
        n = self.turns()
        self.her(GA, "HER-PLAIN-LINE-5d2e please do the thing", self.a)
        self.assertEqual(self.turns(), n, "G9: her plain line started a turn")
        hits = []
        for root, _d, names in os.walk(self.control):
            for f in names:
                try:
                    if "HER-PLAIN-LINE-5d2e" in open(os.path.join(root, f), encoding="utf-8", errors="replace").read():
                        hits.append(os.path.relpath(os.path.join(root, f), self.control))
                except OSError:
                    pass
        self.assertEqual(hits, [], "G9: her line was written to %s" % hits)
        self.lead(GA, "work", self.a)
        self.assertNotIn("HER-PLAIN-LINE-5d2e", self.engine.calls[-1]["system"], "G9: her line is in a worker's system prompt")

    def test_G10_bang_lines_are_commands_only_from_her(self):
        before = len(self.bot.topics)
        self.her(GA, "!task from her", 0)
        self.assertEqual(len(self.bot.topics), before + 1, "G10: her !task did not create a task")
        self.her(GA, "!stop", self.a)
        n = self.turns()
        self.lead(GA, "go", self.a)
        self.assertEqual(self.turns(), n, "G10: her !stop did not stop")
        self.her(GA, "!resume", self.a)
        before = len(self.bot.topics)
        self.say(GA, STRANGER, "!task from a stranger", 0)
        self.assertEqual(len(self.bot.topics), before, "G10: a stranger's !task created a task")

    def test_G11_her_veto_wins_even_in_the_middle_of_a_turn(self):
        b = self.task(GB, "alpha")          # the same name in B must stay untouched
        self.engine.during = lambda: self.her(GA, "!veto", self.a)
        self.bot.texts.clear()
        self.lead(GA, "work", self.a)
        self.assertEqual(self.turns(), 1)
        posted = [t for t in self.said(GA, self.a) if t.startswith("answer")]
        self.assertEqual(posted, [], "G11: the vetoed turn's answer was posted: %s" % posted)
        d = json.load(open(os.path.join(self.gdir(GA), "tasks.json"), encoding="utf-8"))
        self.assertEqual(d["tasks"][str(self.a)]["state"], "closed", "G11: the task is not closed")
        n = self.turns()
        self.lead(GA, "again", self.a)
        self.assertEqual(self.turns(), n, "G11: a new turn started in a vetoed task")
        db = json.load(open(os.path.join(self.gdir(GB), "tasks.json"), encoding="utf-8"))
        self.assertEqual(db["tasks"][str(b)]["state"], "open", "G11: B's task was touched")


LEG = -1001000000009


class Review(World):
    """the four points of the review of the multigroup code (F3, the watcher, legacy + groups, the silent !cmd)"""

    def setUp(self):
        super().setUp()
        self.a = self.task(GA, "alpha")
        self.engine.calls.clear()

    def blocked(self, chat, th):
        n = self.turns()
        self.lead(chat, "go", th)
        return self.turns() == n

    # (1) F3: a persona feeds system prompts, so it never lives where the model can write
    def test_F3_a_persona_inside_the_workspace_is_refused(self):
        inside = os.path.join(self.ws, "groups", str(GA), "persona.md")
        os.makedirs(os.path.dirname(inside), exist_ok=True)
        open(inside, "w", encoding="utf-8").write("PERSONA-INSIDE\n")
        for label, path in (("absolute", inside), ("dotdot", os.path.join(self.ws, "x", "..", "groups", str(GA), "persona.md")),
                            ("the workspace root", os.path.join(self.ws, "persona.md"))):
            open(os.path.join(self.ws, "persona.md"), "w", encoding="utf-8").write("x\n")
            self.persona[GA] = path
            self.write_groups()
            with self.assertRaises(ValueError, msg="F3: a persona inside the workspace (%s) was accepted" % label) as cm:
                self.make()
            self.assertIn("persona", str(cm.exception).lower(), "F3: the refusal does not name persona (%s)" % label)

    def test_F3b_a_persona_reached_through_a_link_into_the_workspace_is_refused(self):
        import subprocess
        inside = os.path.join(self.ws, "p")
        os.makedirs(inside)
        open(os.path.join(inside, "persona.md"), "w", encoding="utf-8").write("x\n")
        link = os.path.join(self.tmp.name, "linkdir")
        made = False
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, inside], capture_output=True).returncode == 0
        else:
            try:
                os.symlink(inside, link)
                made = True
            except OSError:
                pass
        if not made:
            self.skipTest("no junction or symlink here")
        try:
            self.persona[GA] = os.path.join(link, "persona.md")
            self.write_groups()
            with self.assertRaises(ValueError, msg="F3b: a persona behind a link into the workspace was accepted"):
                self.make()
        finally:
            try:
                os.rmdir(link)                      # a junction goes with rmdir, never with a recursive delete
            except OSError:
                try:
                    os.unlink(link)
                except OSError:
                    pass

    def test_F3c_control_a_persona_outside_the_workspace_is_fine(self):
        self.make()
        self.lead(GA, "work", self.a)
        self.assertIn("PERSONA-OF-%d" % abs(GA), self.engine.calls[-1]["system"])

    # (2) her !stop arrives WHILE a turn runs: the watcher must act on it
    def test_W1_her_bang_stop_in_the_middle_of_a_turn(self):
        import threading
        done = threading.Event()

        def during():
            self.bot.script = [[Message(900, GA, "text", text="!stop", thread_id=self.a, from_id=HER)]]
            self.bot.after_script = done.set
            self.h._watch(done)
        self.engine.during = during
        self.bot.texts.clear()
        self.lead(GA, "work", self.a)
        self.assertEqual(self.turns(), 1)
        self.assertEqual([t for t in self.said(GA, self.a) if t.startswith("answer")], [], "W1: the answer of a turn she stopped was posted")
        self.assertFalse([m for m in self.h._pending if getattr(m, "text", "") == "!stop"], "W1: her !stop was queued behind the turn instead of acted on")
        self.assertTrue(self.blocked(GA, self.a), "W1: after her !stop in the middle of a turn the group still runs")
        self.lead(GA, "/resume", self.a)
        self.assertTrue(self.blocked(GA, self.a), "W1: the lead lifted a stop she gave in the middle of a turn")

    def test_W1b_control_the_leads_stop_in_the_middle_of_a_turn_works(self):
        import threading
        done = threading.Event()

        def during():
            self.bot.script = [[Message(901, GA, "text", text="/stop", thread_id=self.a, from_id=LEAD)]]
            self.bot.after_script = done.set
            self.h._watch(done)
        self.engine.during = during
        self.lead(GA, "work", self.a)
        self.assertTrue(self.blocked(GA, self.a), "W1b control: the lead's /stop in the middle of a turn did not stop")

    # (3) the legacy forum next to groups: decided by the designer: a start with both is REFUSED
    def test_L3_forum_chat_id_together_with_groups_file_is_refused(self):
        for legacy in (LEG, GA):                           # a separate chat, and a chat that is also a group
            before = (self.engine.n, len(self.bot.texts))
            with self.assertRaises(ValueError, msg="L3: forum_chat_id=%s together with groups_file started" % legacy) as cm:
                self.make(legacy=legacy)
            msg = str(cm.exception)
            for w in ("forum_chat_id", "groups_file", "groups.json"):
                self.assertIn(w, msg, "L3: the refusal does not say %r: %r" % (w, msg))
            self.assertEqual((self.engine.n, len(self.bot.texts)), before, "L3: a refused start made a turn or sent a message")

    def test_L3b_control_only_groups_file_starts(self):
        self.make()
        th = self.task(GA, "alpha")
        self.lead(GA, "work", th)
        self.assertTrue(self.engine.calls, "L3b control: groups only did not run a turn")

    def test_L3c_control_only_forum_chat_id_starts_as_before(self):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"), os.path.join(self.control, "STOP_GATE"))
        self.h = run.Herald(self.bot, self.engine, EngineConfig(), gate, [], self.control, self.ws, {LEAD}, forum_chat_id=LEG,
                            transcript_roots=[], room_sync=True, now=lambda: 1000.0)
        th = self.task(LEG, "legacy-task")
        self.assertIsNotNone(th, "L3c control: /task in the legacy forum made no topic")
        self.lead(LEG, "work", th)
        self.assertEqual(self.turns(), 1, "L3c control: no turn in the legacy forum")
        self.lead(LEG, "/stop", th)
        n = self.turns()
        self.lead(LEG, "again", th)
        self.assertEqual(self.turns(), n, "L3c control: /stop in the legacy forum no longer stops")

    # (4) her unknown !command is answered, not met with silence
    def test_C4_her_unknown_bang_command_gets_a_refusal_line(self):
        for line in ("!frobnicate now", "!"):
            self.bot.texts.clear()
            n = self.turns()
            self.her(GA, line, self.a)
            said = [t for t in self.said(GA, self.a) if t.strip()]
            self.assertTrue(said, "C4: %r from her was met with silence" % line)
            self.assertEqual(self.turns(), n, "C4: %r started a turn" % line)

    def test_C4b_control_her_plain_line_stays_silent(self):
        self.bot.texts.clear()
        self.her(GA, "just a remark, not a command", self.a)
        self.assertEqual([t for t in self.said(GA, self.a) if t.strip()], [], "C4b: her plain line got an answer (it is a participant's line)")


def aliases_of(path):
    """other spellings of one existing Windows path: the extended prefix and the loopback admin share; only those that name the SAME file are returned"""
    p = os.path.realpath(path)
    drive, rest = os.path.splitdrive(p)
    cand = {"extended-prefix": "\\\\?\\" + p}
    if len(drive) == 2 and drive[1] == ":":
        cand["loopback-share"] = "\\\\localhost\\" + drive[0] + "$" + rest
    out = {}
    for k, v in cand.items():
        try:
            if os.path.samefile(v, p):
                out[k] = v
        except OSError:
            pass
    return out


class Aliases(World):
    """round 2 of the review: string comparison of realpath() lets an alias of the workspace slip through; her stop with the bot's name"""

    def setUp(self):
        super().setUp()
        if os.name != "nt":
            self.skipTest("Windows path aliases")
        self.a = self.task(GA, "alpha")
        self.inside = os.path.join(self.ws, "groups", str(GA), "persona.md")
        os.makedirs(os.path.dirname(self.inside), exist_ok=True)
        open(self.inside, "w", encoding="utf-8").write("PERSONA-INSIDE\n")
        self.alias = aliases_of(self.inside)
        if not self.alias:
            self.skipTest("no alias of a path resolves to the same file here")

    def test_F3d_a_persona_inside_the_workspace_spelled_as_an_alias_is_refused(self):
        for label, spelled in self.alias.items():
            self.persona[GA] = spelled
            self.write_groups()
            with self.assertRaises(ValueError, msg="F3d: a persona inside the workspace written as %s (%s) was accepted" % (label, spelled)) as cm:
                self.make()
            self.assertIn("persona", str(cm.exception).lower(), "F3d: the refusal does not say persona (%s)" % label)

    def test_F3g_the_same_through_load_config_with_groups_file(self):
        for label, spelled in self.alias.items():
            self.persona[GA] = spelled
            self.write_groups()
            with self.assertRaises(ValueError, msg="F3g: load_config accepted a group persona inside the workspace written as %s" % label) as cm:
                run.load_config(self.cfg(groups_file=self.groups_path))
            self.assertIn("persona", str(cm.exception).lower(), "F3g: the refusal does not say persona (%s)" % label)
        self.persona[GA] = self.inside
        self.write_groups()
        with self.assertRaises(ValueError, msg="F3g control: the plain path inside the workspace was accepted by load_config"):
            run.load_config(self.cfg(groups_file=self.groups_path))

    def test_F3h_control_a_real_persona_outside_the_workspace_starts(self):
        self.persona[GA] = os.path.join(self.tmp.name, "persona_%d.md" % abs(GA))
        self.write_groups()
        self.make()
        run.load_config(self.cfg(groups_file=self.groups_path))

    def test_F3e_the_workspace_spelled_as_an_alias_and_a_plain_persona_inside_it(self):
        self.persona[GA] = self.inside
        self.write_groups()
        self.ws = "\\\\?\\" + os.path.realpath(self.ws)
        with self.assertRaises(ValueError, msg="F3e: workspace spelled with the extended prefix + a plain persona inside it was accepted"):
            self.make()

    def cfg(self, **kw):
        ctl = os.path.join(self.tmp.name, "single_control")
        os.makedirs(ctl, exist_ok=True)
        d = {"owners": [LEAD], "workspace": self.ws, "control": ctl}
        d.update(kw)
        p = os.path.join(self.tmp.name, "single.json")
        json.dump(d, open(p, "w"))
        return p

    def test_F3f_the_single_bot_has_the_same_hole_or_not(self):
        outside = os.path.join(self.tmp.name, "single_persona.md")
        open(outside, "w", encoding="utf-8").write("ok\n")
        run.load_config(self.cfg(persona_file=outside))                         # control: outside the workspace loads
        with self.assertRaises(ValueError, msg="F3f control: the plain path inside the workspace was accepted"):
            run.load_config(self.cfg(persona_file=self.inside))
        for label, spelled in self.alias.items():
            for key in ("persona_file", "agents_dir"):
                with self.assertRaises(ValueError, msg="F3f: %s inside the workspace written as %s was accepted" % (key, label)):
                    run.load_config(self.cfg(**{key: spelled if key == "persona_file" else os.path.dirname(spelled)}))
        with self.assertRaises(ValueError, msg="F3f: workspace spelled with the extended prefix + a plain persona inside it was accepted"):
            run.load_config(self.cfg(workspace="\\\\?\\" + os.path.realpath(self.ws), persona_file=self.inside))

    def blocked(self):
        n = self.turns()
        self.lead(GA, "go", self.a)
        return self.turns() == n

    def test_S1_her_bang_stop_with_the_bots_name_outside_a_turn_is_a_stop(self):
        self.her(GA, "!stop@herald_bot", self.a)
        self.assertTrue(self.blocked(), "S1: her !stop@herald_bot outside a turn is not a stop")
        self.her(GA, "/resume", self.a)
        self.assertFalse(self.blocked(), "S1 control: her /resume did not lift")
        self.her(GA, "/stop@herald_bot", self.a)
        self.assertTrue(self.blocked(), "S1 control: her /stop@herald_bot is not a stop")
        self.her(GA, "!resume@herald_bot", self.a)
        self.assertFalse(self.blocked(), "S1: her !resume@herald_bot does not lift her stop")
        before = len(self.bot.topics)
        self.her(GA, "!task@herald_bot from her", 0)
        self.assertEqual(len(self.bot.topics), before + 1, "S1 control: her !task@herald_bot made no task (the @bot suffix broke a working command)")


class GroupsFileOutsideWorkspace(World):
    """round 3: groups.json holds owners / her_id / peer_bots, so the model (cwd = workspace) must never be able to rewrite it"""

    def cfg(self, groups_file):
        ctl = os.path.join(self.tmp.name, "single_control")
        os.makedirs(ctl, exist_ok=True)
        p = os.path.join(self.tmp.name, "single.json")
        json.dump({"owners": [LEAD], "workspace": self.ws, "control": ctl, "groups_file": groups_file}, open(p, "w"))
        return p

    def inside_copy(self):
        d = os.path.join(self.ws, "cfg")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "groups.json")
        open(p, "w", encoding="utf-8").write(open(self.groups_path, encoding="utf-8").read())
        return p

    def test_G13_control_a_groups_file_outside_the_workspace_loads(self):
        run.load_config(self.cfg(self.groups_path))

    def test_G13_groups_file_inside_the_workspace_is_refused(self):
        with self.assertRaises(ValueError, msg="G13: a groups_file inside the workspace was accepted") as cm:
            run.load_config(self.cfg(self.inside_copy()))
        self.assertIn("groups_file", str(cm.exception), "G13: the refusal does not name the key")

    def test_G13b_groups_file_inside_the_workspace_through_a_link_or_an_alias_is_refused(self):
        import subprocess
        inside = self.inside_copy()
        tried = 0
        link = os.path.join(self.tmp.name, "linkcfg")
        made = False
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, os.path.dirname(inside)], capture_output=True).returncode == 0
        else:
            try:
                os.symlink(os.path.dirname(inside), link)
                made = True
            except OSError:
                pass
        spelled = []
        if made:
            spelled.append(("link", os.path.join(link, "groups.json")))
        if os.name == "nt":
            spelled.extend(aliases_of(inside).items())
        if not spelled:
            self.skipTest("no link and no alias here")
        try:
            for label, path in spelled:
                tried += 1
                with self.assertRaises(ValueError, msg="G13b: a groups_file inside the workspace written as %s (%s) was accepted" % (label, path)) as cm:
                    run.load_config(self.cfg(path))
                self.assertIn("groups_file", str(cm.exception), "G13b: the refusal does not name the key (%s)" % label)
        finally:
            if made:
                try:
                    os.rmdir(link)                  # a junction goes with rmdir, never with a recursive delete
                except OSError:
                    try:
                        os.unlink(link)
                    except OSError:
                        pass
        self.assertTrue(tried)

    def test_G14_inside_workspace_refuses_what_is_not_a_path_with_a_value_error(self):
        from herald import groups as G
        for bad in (123, None, b"x", ["a"]):
            with self.assertRaises(ValueError, msg="G14: inside_workspace(%r, ws) did not raise ValueError" % (bad,)):
                G.inside_workspace(bad, self.ws)
        self.assertFalse(G.inside_workspace(self.groups_path, self.ws), "G14 control: a path outside the workspace was reported inside")


if __name__ == "__main__":
    unittest.main(verbosity=1)
