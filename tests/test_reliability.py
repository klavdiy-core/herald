"""Reliability findings from an outside review (25.10): spending after /stop, speaking into quiet hours,
a killed turn costing nothing, jobs marked run without running. Each test is red on 0.12.0."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import engine, speak_first, telegram  # noqa: E402
from herald.telegram import Message  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_run import OWNER, Base  # noqa: E402


class StopAndQuietDuringATurn(Base):
    def job(self):
        return [speak_first.Job("check", "anything new?", every_min=60)]

    def test_a_stop_that_arrives_during_a_proactive_turn_sends_nothing(self):
        h = self.make(jobs=self.job())
        stop = os.path.join(self.control, "STOP")
        real = self.engine

        def engine_then_stop(*a, **k):
            r = real(*a, **k)
            open(stop, "w").close()                      # /stop lands while the turn runs
            return r
        h.run_turn = engine_then_stop
        h.tick()
        self.assertEqual(self.bot.texts, [])

    def test_quiet_hours_that_begin_during_the_turn_send_nothing(self):
        h = self.make(jobs=self.job())
        self.clock.t = datetime(2026, 10, 24, 21, 59)
        real = self.engine

        def slow(*a, **k):
            self.clock.t = datetime(2026, 10, 24, 22, 10)  # the turn took 11 minutes
            return real(*a, **k)
        h.run_turn = slow
        h.tick()
        self.assertEqual(self.bot.texts, [])


class Jobs(Base):
    def test_a_job_that_did_not_run_this_tick_is_still_due(self):
        jobs = [speak_first.Job("a", "first?", every_min=60), speak_first.Job("b", "second?", every_min=60)]
        self.engine.answer = "[SILENT]"                  # silent: the gate stays open for the next tick
        h = self.make(jobs=jobs)
        h.tick()
        h.tick()
        prompts = [c["prompt"] for c in self.engine.calls]
        self.assertTrue(any("first?" in p for p in prompts), prompts)
        self.assertTrue(any("second?" in p for p in prompts), prompts)


class CorruptSpeakState(Base):
    def corrupt(self):
        with open(os.path.join(self.control, "speak.json"), "w") as f:
            f.write("{broken")

    def test_it_stays_closed_on_the_next_tick_too(self):
        self.corrupt()
        self.assertFalse(self.h.gate.may_speak()[0])
        self.assertFalse(self.h.gate.may_speak()[0])      # it used to start fresh here, caps forgotten

    def test_an_owner_reply_opens_it_again(self):
        self.corrupt()
        self.h.gate.record_owner_reply()                  # the owner writes before the gate looks at the file
        self.assertTrue(self.h.gate.may_speak()[0])       # it used to stay shut for good ("broken" was saved)
        self.assertTrue(self.h.gate.may_speak()[0])


class TelegramHiccups(Base):
    def test_a_late_toast_does_not_burn_the_choice(self):
        # the tap is answered after a long turn: Telegram says "query is too old"; the chosen turn must still run
        did = self.h.decisions.offer("which?", ["A", "B"])

        def too_old(*a, **k):
            raise telegram.TelegramError("Bad Request: query is too old")
        self.bot.answer_callback = too_old
        self.h.handle(Message(9, OWNER, "callback", text=f"d:{did}:0", callback_id="cb9"))
        self.assertTrue(any("The owner chose: A" in c["prompt"] for c in self.engine.calls), self.engine.calls)

    def test_a_failed_typing_signal_does_not_lose_the_message(self):
        def blip(*a, **k):
            raise telegram.TelegramError("network blip")
        self.bot.send_action = blip
        self.h.handle(Message(10, OWNER, "text", text="hi"))
        self.assertEqual(len(self.engine.calls), 1)
        self.assertTrue(self.bot.texts)


class DecisionsFromTwoThreads(unittest.TestCase):
    def test_offers_from_several_threads_are_all_kept(self):
        import threading
        import time as _time
        from herald import decisions
        with tempfile.TemporaryDirectory() as d:
            store = decisions.Decisions(os.path.join(d, "decisions.json"), now=_time.time)
            ids, errors = [], []

            def offer_many():
                for _ in range(25):
                    try:
                        ids.append(store.offer("x", ["A", "B"]))
                    except Exception as e:             # a race on the shared .tmp raised here
                        errors.append(e)
            threads = [threading.Thread(target=offer_many) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(store._load()), 150)

    def test_old_closed_decisions_are_dropped(self):
        from herald import decisions
        clock = [1000.0]
        with tempfile.TemporaryDirectory() as d:
            store = decisions.Decisions(os.path.join(d, "decisions.json"), now=lambda: clock[0], ttl_s=60)
            old = store.offer("old", ["A"])
            store.tap(old, 0)
            clock[0] += 8 * 24 * 3600                      # a week and a day later
            store.offer("new", ["A"])
            self.assertNotIn(old, store._load())


class HaltedWebRoom(unittest.TestCase):
    def test_a_room_halted_by_stop_ends_its_thread(self):
        # its thread used to loop every pace_s until someone clicked "end"
        import time as _time
        from herald import room_web
        from test_room import Voice
        voices = {"Raven": Voice("Raven", [f"raven {i}" for i in range(20)], cost=0.001)}
        with tempfile.TemporaryDirectory() as d:
            state = room_web.State(voices, d, None, 0.01, should_stop=lambda: "stop")
            state.start({"topic": "x", "format": "brainstorm", "seats": [{"name": "Raven", "role": "ideas"}]})
            state.thread.join(timeout=3)
            self.assertFalse(state.thread.is_alive())
            self.assertEqual(state.room.halted, "stop")


class StopDuringATurn(Base):
    """The last finding: one poll thread, so /stop was heard only after a 10-minute turn had spent its money."""

    def update(self, uid, t):
        return Message(uid, OWNER, "text", text=t)

    def test_stop_is_heard_while_a_turn_runs_and_ends_it(self):
        import time as _time
        seen = {}

        def long_turn(prompt, workspace, cfg, session_id=None, should_abort=None):
            self.bot.updates.append(self.update(2, "/stop"))   # the owner sends /stop mid-turn
            end = _time.time() + 5
            while _time.time() < end:
                if should_abort and should_abort():
                    seen["aborted"] = True
                    return TurnResult(False, error="stopped by /stop", cost_usd=0.2, cost_estimated=True)
                _time.sleep(0.02)
            return TurnResult(True, "finished anyway", "s", 0.2)
        self.h.run_turn = long_turn
        self.h.handle(self.update(1, "do a long thing"))
        self.assertTrue(seen.get("aborted"), "the turn never heard /stop")
        self.assertTrue(os.path.exists(os.path.join(self.control, "STOP")))
        self.assertNotIn("finished anyway", " ".join(t for _, t in self.bot.texts))

    def test_other_messages_during_a_turn_wait_their_turn_in_order(self):
        calls = []

        def turn(prompt, workspace, cfg, session_id=None, should_abort=None):
            calls.append(prompt)
            if len(calls) == 1:
                self.bot.updates.extend([self.update(2, "second"), self.update(3, "third")])
                import time as _time
                _time.sleep(0.5)                                    # long enough for the watcher to collect them
            return TurnResult(True, "ok " + prompt, "s", 0.01)
        self.h.run_turn = turn
        self.h.handle(self.update(1, "first"))
        self.h.drain_pending()
        self.assertEqual(calls, ["first", "second", "third"])


class OffsetBot:
    """Like Telegram: poll(offset) returns every update with update_id >= offset, until the offset moves past it."""

    def __init__(self, base):
        self.base, self.all = base, []

    def poll(self, offset, timeout=50):
        ups = [m for m in self.all if m.update_id >= offset]
        return ups, (max(m.update_id for m in ups) + 1) if ups else offset

    def __getattr__(self, name):
        return getattr(self.base, name)


class NoDoubleHandling(Base):
    def test_the_rest_of_a_batch_is_not_handled_twice_when_a_turn_watches(self):
        import time as _time
        bot = OffsetBot(self.bot)
        self.h.bot = bot
        calls = []

        def turn(prompt, workspace, cfg, session_id=None, should_abort=None):
            calls.append(prompt)
            if prompt == "first":
                _time.sleep(0.5)                       # the watcher polls meanwhile
            return TurnResult(True, "ok", "s", 0.01)
        self.h.run_turn = turn
        bot.all = [Message(1, OWNER, "text", text="first"), Message(2, OWNER, "text", text="second")]
        msgs, offset = bot.poll(0)
        self.h._process(msgs, offset)
        self.h.drain_pending()
        self.assertEqual(calls, ["first", "second"])

    def test_the_loop_never_polls_from_an_old_watcher_offset(self):
        bot = OffsetBot(self.bot)
        self.h.bot = bot
        self.h._pending_offset = 2                     # left by a watcher long ago
        self.h._save(offset=10)                        # the loop has moved on since
        polled = []
        real = bot.poll
        bot.poll = lambda offset, timeout=50: (polled.append(offset), real(offset, timeout))[1]

        def one_round(*a, **k):
            raise KeyboardInterrupt                    # stop the endless loop after its first poll
        self.h.tick = one_round
        with self.assertRaises(KeyboardInterrupt):
            self.h.loop(poll_timeout=0)
        self.assertEqual(polled, [10])


@unittest.skipUnless(sys.platform == "win32", "cmd.exe quoting is a Windows matter")
class CommandAgentInjection(unittest.TestCase):
    """Outside review, round 3 (25.10): a .cmd shim with prompt_arg went through cmd.exe, and
    'fine & echo injected > PWNED.txt & rem' wrote PWNED.txt."""

    def test_a_batch_target_with_the_prompt_in_argv_is_refused(self):
        from herald import agents
        with tempfile.TemporaryDirectory() as d:
            shim = os.path.join(d, "tool.cmd")
            with open(shim, "w") as f:
                f.write("@echo %*\n")
            with self.assertRaises(ValueError) as cm:
                agents.Command("Shim", [shim], prompt_arg=True, cwd=d)
            self.assertIn("cmd.exe", str(cm.exception))
            # on stdin the same shim is fine: the prompt is never parsed by cmd.exe
            a = agents.Command("Shim", [shim], prompt_arg=False, cwd=d)
            a.speak("fine & echo injected > PWNED.txt & rem")
            self.assertFalse(os.path.exists(os.path.join(d, "PWNED.txt")))

    def test_a_batch_target_found_on_path_is_refused_too(self):
        from herald import agents
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "shimtool.cmd"), "w") as f:
                f.write("@echo %*\n")
            old = os.environ["PATH"]
            os.environ["PATH"] = d + os.pathsep + old
            try:
                with self.assertRaises(ValueError):
                    agents.Command("Shim", ["shimtool"], prompt_arg=True)
            finally:
                os.environ["PATH"] = old


class HardRoomBudget(unittest.TestCase):
    """Round 3: budget 0.3 ended at 0.406 before end() and 0.606 after it."""

    def room(self, cost, cap, budget):
        from herald import room
        from test_room import Voice
        seats = []
        for n in ("Raven", "Magpie"):
            v = Voice(n, [f"{n} {i}" for i in range(20)], cost=cost)
            v.reply_cap = cap                          # what one reply may cost at most (a claude role's cap)
            seats.append(room.Seat(v, "ideas"))
        return room.Room("x", seats, fmt="brainstorm", budget_usd=budget)

    def test_a_reply_that_could_cross_the_budget_does_not_start(self):
        r = self.room(cost=0.12, cap=0.2, budget=0.3)
        while r.step():
            pass
        self.assertLessEqual(r.spent, 0.3)
        self.assertEqual(len(r.messages), 1)           # 0.12 + 0.2 > 0.3: the second reply would not fit

    def test_over_budget_the_summary_is_made_without_paying(self):
        r = self.room(cost=0.12, cap=0.2, budget=0.3)
        while r.step():
            pass
        before = r.spent
        summary = r.end()
        self.assertEqual(r.spent, before)
        self.assertIn("Raven 0", summary)              # built from what was said

    def test_agents_without_a_known_cap_still_count(self):
        r = self.room(cost=0.12, cap=0, budget=0.3)
        while r.step():
            pass
        self.assertEqual(len(r.messages), 3)           # the old rule: stop once spent reaches the budget


class TelegramRoomAndLearn(Base):
    def test_a_telegram_room_has_a_budget_by_default(self):
        from herald import run
        self.assertIsNotNone(run.Herald(bot=self.bot, run_turn=self.engine, engine_cfg=None, gate=self.h.gate,
                                        jobs=[], control=self.control, workspace=self.workspace,
                                        owners={OWNER}).room_budget)



class SmallFindings(Base):
    """From two outside reviews, 25.10."""

    def test_a_command_agent_can_say_what_a_call_costs(self):
        from herald import agents
        with tempfile.TemporaryDirectory() as d:
            a = agents.from_config({"name": "Gemini", "kind": "command", "command": [sys.executable, "-c",
                                    "import sys; print('an idea')"], "cost_per_call_usd": 0.02}, d)
            text, cost = a.speak("hi")
            self.assertEqual((text, cost), ("an idea", 0.02))           # the room budget can count it now
            b = agents.from_config({"name": "Free", "kind": "command", "command": [sys.executable, "-c",
                                    "print('x')"]}, d)
            self.assertEqual(b.speak("hi")[1], 0.0)

    def test_an_answer_that_is_only_choices_shows_no_raw_markup(self):
        self.engine.answer = "[[choices: Rewrite | Keep]]"
        self.h.handle(Message(40, OWNER, "text", text="what now?"))
        shown = self.bot.buttons[-1][1]
        self.assertNotIn("[[choices", shown)
        self.assertEqual([b[0] for b in self.bot.buttons[-1][2]][:2], ["Rewrite", "Keep"])

    def test_a_memory_line_holding_the_separator_does_not_split_memory(self):
        self.h.nb.add_memory("- first\n§\nsecond half")
        self.assertEqual(len(self.h.nb.memory()), 1)


class CrashMidBatch(Base):
    def test_the_rest_of_a_batch_survives_a_crash(self):
        # an outside review (25.10): the watcher confirms the whole batch to Telegram before it is handled; a crash
        # mid-batch lost the tail for good. The tail is kept on disk and handled at the next start.
        calls = []

        def turn(prompt, workspace, cfg, session_id=None, should_abort=None):
            calls.append(prompt)
            if prompt == "first":
                raise KeyboardInterrupt                      # the daemon dies in the middle of the batch
            return TurnResult(True, "ok", "s", 0.01)
        self.h.run_turn = turn
        batch = [Message(1, OWNER, "text", text="first"), Message(2, OWNER, "text", text="second")]
        with self.assertRaises(KeyboardInterrupt):
            self.h._process(batch, 3)
        h2 = self.make()                                     # the next start
        h2.run_turn = turn
        h2.resume()
        self.assertEqual(calls, ["first", "second"])         # "first" is not run twice; "second" is not lost
        self.assertFalse(os.path.exists(os.path.join(self.control, "inflight.json")))


class FakeRoom:
    def __init__(self):
        self.topic, self.halted, self.spent, self.messages, self.ended = "x", None, 0.0, [], False
        self.record_path = None

    def reopen(self):
        pass

    def round(self):
        return True

    def end(self):
        self.ended = True                                # a paid summary call
        return "summary"


class RoomAfterStop(Base):
    def test_one_more_round_after_stop_spends_nothing(self):
        r = FakeRoom()
        self.h.live_rooms["k"] = r
        open(os.path.join(self.control, "STOP"), "w").close()
        self.h._room_choice(OWNER, {"choice": "One more round", "meta": {"room": "k"}})
        self.assertFalse(r.ended)
        self.assertIn("stop", self.bot.texts[-1][1].lower())

    def test_a_round_halted_by_the_budget_makes_no_summary(self):
        r = FakeRoom()

        def halted_round():
            r.halted = "budget"
            return False
        r.round = halted_round
        self.h.live_rooms["k"] = r
        self.h._room_choice(OWNER, {"choice": "One more round", "meta": {"room": "k"}})
        self.assertFalse(r.ended)


class KilledTurnCost(unittest.TestCase):
    def cfg(self, cmd, timeout):
        return EngineConfig(claude_cmd=[sys.executable, "-c", cmd], timeout_s=timeout, max_budget_usd=0.5)

    def test_a_turn_killed_by_timeout_counts_its_whole_budget(self):
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg("import time; time.sleep(30)", 1))
        self.assertFalse(r.ok)
        self.assertEqual(r.cost_usd, 0.5)
        self.assertTrue(r.cost_estimated)

    def test_a_turn_without_json_counts_its_whole_budget(self):
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg("print('not json')", 30))
        self.assertEqual(r.cost_usd, 0.5)
        self.assertTrue(r.cost_estimated)

    def test_should_abort_kills_a_real_turn_quickly(self):
        import time as _time
        t0 = _time.time()
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg("import time; time.sleep(30)", 60),
                                should_abort=lambda: _time.time() - t0 > 1, check_s=0.2)
        self.assertLess(_time.time() - t0, 10)
        self.assertIn("stopped by /stop", r.error)
        self.assertTrue(r.cost_estimated)

    def test_a_turn_that_finishes_is_read_whole_with_should_abort_on(self):
        out = "import json,sys,time; sys.stdin.read(); time.sleep(0.6); " \
              "print(json.dumps({'subtype':'success','result':'done','session_id':'s','total_cost_usd':0.01}))"
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg(out, 30), should_abort=lambda: False, check_s=0.2)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.text, "done")


class LedgerMarksAnEstimate(Base):
    def test_the_ledger_says_when_a_cost_is_an_estimate(self):
        self.h.run_turn = lambda *a, **k: TurnResult(False, error="timeout", cost_usd=0.5, cost_estimated=True)
        self.h._turn("hi")
        with open(self.h.costs_path, encoding="utf-8") as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual(row["usd"], 0.5)
        self.assertTrue(row["estimated"])


if __name__ == "__main__":
    unittest.main()
