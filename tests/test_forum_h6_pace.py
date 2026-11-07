"""Общий потолок форума, тишина по часу, чистка _held, inflight.json.
Рядом: test_forum_h6_blockers.py (блокеры 1-7).

КОНТРАКТ:
  Herald(..., bot_turns_per_hour=10, bot_turns_per_hour_forum=30)  — потолок темы остаётся, ОБЩИЙ потолок форума — новый ключ.
  Тишина по любому из двух потолков держится ДО человека (час сам её не снимает). Потолок темы снимает сообщение владельца в
  ЭТОЙ теме; общий — сообщение владельца в ЛЮБОЙ теме форума. Сообщение владельца в теме B потолок темы A не снимает.
  _held (то, что ждёт паузы пары) стирается: сообщением владельца в теме, /new, /stop, закрытием темы — только для этой темы.
  Без forum_chat_id inflight.json пишет ровно прежние поля Message (без thread_id/from_id/from_bot); с форумом — с ними.
Запуск: python -X utf8 -m unittest tests.test_forum_h6_pace"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_forum_tasks as T  # noqa: E402
from herald import speak_first  # noqa: E402
from herald.engine import EngineConfig  # noqa: E402


class Mk(T.Base):
    def make2(self, **kw):
        """Как make(), но неизвестный НОВЫЙ ключ не роняет всё в «без форума»: тест краснеет по поведению."""
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        args = (self.bot, self.engine, EngineConfig(), gate, [], self.control, self.ws, {T.OWNER})
        base = dict(transcript_roots=[], room_sync=True, now=self.clock, forum_chat_id=T.FORUM)
        while True:
            try:
                self.h = T.HeraldCls(*args, **kw, **base)
                return self.h
            except TypeError as e:
                bad = [k for k in kw if "'%s'" % k in str(e)]
                if not bad:
                    raise
                kw.pop(bad[0])


class ForumCap(Mk):
    auto_bind_peers = True      # old peer tests assume a bound peer (fixture)
    def test_C1_four_busy_topics_stop_at_the_forum_cap_of_30(self):
        self.make2(peer_bots=(T.PEER,), max_bot_hops=99, bot_turns_per_hour=10, bot_turns_per_hour_forum=30, bot_reply_gap_s=0)
        topics = [self.new_task(f"t{i}") for i in range(4)]
        for i in range(12):
            for t in topics:
                self.peer(t, f"hello {t} {i}")
        self.assertEqual(len(self.engine.calls), 30, "ходов ботов за час на форум: %d, потолок 30" % len(self.engine.calls))

    def test_C1b_GUARD_one_topic_is_still_cut_at_its_own_10(self):
        self.make2(peer_bots=(T.PEER,), max_bot_hops=99, bot_turns_per_hour=10, bot_turns_per_hour_forum=30, bot_reply_gap_s=0)
        t = self.new_task("solo")
        for i in range(15):
            self.peer(t, f"hello {i}")
        self.assertEqual(len(self.engine.calls), 10)


class HeldClear(Mk):
    auto_bind_peers = True      # old peer tests assume a bound peer (fixture)
    """не-блокер 9."""

    def setUp(self):
        super().setUp()
        self.make2(peer_bots=(T.PEER,), max_bot_hops=99, bot_turns_per_hour=99, bot_reply_gap_s=5)
        self.t = self.new_task("held")
        self.clock.t = 1000.0
        self.peer(self.t, "m1")
        self.clock.t = 1000.5
        self.peer(self.t, "STALE-HELD")                       # ждёт паузы
        self.bot.texts.clear()

    def held_shown(self):
        self.bot.texts.clear()
        self.owner(self.t, "/status")
        return str(T.PEER) in "\n".join(self.said_in(self.t))      # «bot 7001: N held»; «Nothing held.» тоже содержит held

    def after_gap_prompt(self):
        self.clock.t = 1100.0
        n = len(self.engine.calls)
        self.peer(self.t, "m9")
        return self.engine.calls[n]["prompt"] if len(self.engine.calls) > n else ""

    def test_N0_GUARD_without_any_clearing_the_held_line_is_there(self):
        self.assertTrue(self.held_shown())
        self.assertIn("STALE-HELD", self.after_gap_prompt())

    def test_N1_an_owner_message_in_the_topic_clears_held(self):
        self.owner(self.t, "go on")
        self.assertFalse(self.held_shown(), "в /status всё ещё held после слова владельца")
        self.assertNotIn("STALE-HELD", self.after_gap_prompt())

    def test_N2_new_clears_held(self):
        self.owner(self.t, "/new")
        self.assertFalse(self.held_shown())
        self.assertNotIn("STALE-HELD", self.after_gap_prompt())

    def test_N3_stop_clears_held(self):
        self.owner(self.t, "/stop")
        self.owner(self.t, "/resume")
        self.assertFalse(self.held_shown())
        self.assertNotIn("STALE-HELD", self.after_gap_prompt())

    def test_N4_closing_the_topic_clears_held(self):
        self.tap(self.t, f"task:done:{self.t}")
        self.owner(self.t, "/reopen")
        self.assertFalse(self.held_shown())
        self.assertNotIn("STALE-HELD", self.after_gap_prompt())

    def test_N5_GUARD_another_topic_keeps_its_own_held(self):
        other = self.new_task("other")
        self.clock.t = 1200.0
        self.peer(other, "o1")
        self.clock.t = 1200.5
        self.peer(other, "OTHER-HELD")
        self.owner(self.t, "go on")
        self.bot.texts.clear()
        self.owner(other, "/status")
        self.assertIn(str(T.PEER), "\n".join(self.said_in(other)))


class HourSilence(Mk):
    auto_bind_peers = True      # old peer tests assume a bound peer (fixture)
    """Тишина по часу снимается только человеком."""

    def hot(self, **kw):
        self.make2(peer_bots=(T.PEER,), max_bot_hops=99, bot_reply_gap_s=0, **kw)

    def test_Y1_topic_cap_silence_does_not_lift_itself_after_an_hour(self):
        self.hot(bot_turns_per_hour=2, bot_turns_per_hour_forum=99)
        t = self.new_task("a")
        for i in range(3):
            self.peer(t, f"x{i}")
        self.assertEqual(len(self.engine.calls), 2)
        self.clock.t += 4000
        self.peer(t, "later")
        self.assertEqual(len(self.engine.calls), 2, "через час тишина сняла себя сама")

    def test_Y2_an_owner_message_in_that_topic_lifts_the_topic_silence(self):
        self.hot(bot_turns_per_hour=2, bot_turns_per_hour_forum=99)
        t = self.new_task("a")
        for i in range(3):
            self.peer(t, f"x{i}")
        self.owner(t, "carry on")
        n = len(self.engine.calls)
        self.peer(t, "after human")
        self.assertEqual(len(self.engine.calls), n + 1, "владелец сказал слово, а бот в теме всё ещё молчит")

    def test_Y3_an_owner_message_in_topic_B_does_not_lift_topic_As_own_cap(self):
        self.hot(bot_turns_per_hour=2, bot_turns_per_hour_forum=99)
        a, b = self.new_task("a"), self.new_task("b")
        for i in range(3):
            self.peer(a, f"x{i}")
        self.owner(b, "hello b")
        n = len(self.engine.calls)
        self.peer(a, "still capped")
        self.assertEqual(len(self.engine.calls), n, "слово в теме B сняло потолок темы A")

    def test_Y4_forum_cap_silence_holds_after_an_hour_too(self):
        self.hot(bot_turns_per_hour=99, bot_turns_per_hour_forum=3)
        a, b = self.new_task("a"), self.new_task("b")
        for i in range(3):
            self.peer(a if i % 2 == 0 else b, f"x{i}")
        self.peer(b, "over")
        self.assertEqual(len(self.engine.calls), 3)
        self.clock.t += 4000
        self.peer(a, "later")
        self.assertEqual(len(self.engine.calls), 3, "общая тишина сняла себя сама")

    def test_Y5_an_owner_message_in_ANY_topic_lifts_the_forum_silence(self):
        self.hot(bot_turns_per_hour=99, bot_turns_per_hour_forum=3)
        a, b = self.new_task("a"), self.new_task("b")
        for i in range(4):
            self.peer(a, f"x{i}")
        self.owner(b, "hello b")
        n = len(self.engine.calls)
        self.peer(a, "after human")
        self.assertEqual(len(self.engine.calls), n + 1, "слово в другой теме не сняло общую тишину")

    def test_Y6_GUARD_below_the_cap_everything_runs(self):
        self.hot(bot_turns_per_hour=5, bot_turns_per_hour_forum=5)
        t = self.new_task("a")
        for i in range(5):
            self.peer(t, f"x{i}")
        self.assertEqual(len(self.engine.calls), 5)


class InflightBytes(Mk):
    """не-блокер 7 (старый): без forum_chat_id inflight.json — прежние поля."""

    OLD = ["update_id", "chat_id", "kind", "text", "file_id", "forwarded", "file_size", "callback_id"]

    def _build(self, forum):
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        kw = dict(forum_chat_id=T.FORUM, peer_bots=(T.PEER,)) if forum else {}
        return T.HeraldCls(self.bot, self.engine, EngineConfig(), gate, [], self.control, self.ws, {T.OWNER},
                           transcript_roots=[], room_sync=True, now=self.clock, **kw)

    def _rows(self, h):
        h._pending = [T.Message(5, T.OWNER, "text", text="hi")]
        h._keep_inflight()
        with open(os.path.join(self.control, "inflight.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_V1_without_a_forum_the_inflight_row_has_only_the_old_fields(self):
        rows = self._rows(self._build(False))
        self.assertEqual(list(rows[0]), self.OLD, "поля inflight.json: %s" % list(rows[0]))

    def test_V2_GUARD_with_a_forum_the_thread_and_sender_survive_a_crash(self):
        rows = self._rows(self._build(True))
        for k in ("thread_id", "from_id", "from_bot"):
            self.assertIn(k, rows[0])

    def test_V3_GUARD_an_old_format_file_is_resumed_not_set_aside(self):
        h = self._build(False)
        with open(os.path.join(self.control, "inflight.json"), "w", encoding="utf-8") as f:
            json.dump([dict(zip(self.OLD, (6, T.OWNER, "text", "old words", "", False, 0, "")))], f)
        h.resume()
        self.assertFalse(os.path.exists(os.path.join(self.control, "inflight.json.bad")))
        self.assertTrue(any("old words" in c["prompt"] for c in self.engine.calls), "старый файл не обработан")


if __name__ == "__main__":
    unittest.main()
