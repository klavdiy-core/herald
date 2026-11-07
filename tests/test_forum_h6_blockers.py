"""Красные тесты на блокеры 1-7 внешнего ревью форума.
Каждый красный должен краснеть по СВОЕЙ причине; охранные (GUARD) зелёные.

КОНТРАКТ (поведение, не структура; единственное внутреннее имя — Forum._seen):
  1  telegram.Bot получает публичный атрибут/аргумент `log` (callable(str) или None). Update с ключом guest_message и Message с
     guest_query_id (в любом чате) НЕ слышны, ignored += 1 и в log уходит ОДНА строка со словом «guest». answerGuestQuery и
     deleteForumTopic не встречаются ни в одном herald/*.py; allowed_updates не шире {message, callback_query}.
  2  Forum._seen ограничен по размеру (не больше 512 записей) при любом числе уникальных эхо; дедуп последнего текста работает.
  3  bot_turns_per_hour — общий потолок бот-ходов в час на весь форум (все темы вместе); пауза bot_reply_gap_s — на пару
     (мой бот, чужой бот), поперёк тем: второе сообщение того же бота в ДРУГОЙ теме в ту же секунду ходом не становится.
  4  /done идемпотентен: edit прошёл, close упал -> state=open; повторное «Yes, close» закрывает тему; TOPIC_NOT_MODIFIED от
     editForumTopic = успех (close всё равно вызывается).
  5  /reopen сходится с Telegram в обе стороны: (а) tasks.json=closed, reopenForumTopic -> TOPIC_NOT_MODIFIED => state=open;
     (б) tasks.json=open, а тема закрыта в UI руками => /reopen всё равно зовёт reopen_topic (не «Already open.»).
  6  tasks.json: валидный JSON, но tasks не dict => файл уходит в .bad, строка в control/audit.log, не затирается пустым.
  7  Ход чужого бота: в системном промпте нет НИ тетради владельца (nb.compose), НИ skills_toc; персона есть.
     Ход владельца в той же теме: и тетрадь, и skills_toc, и персона на месте (негативный контроль).
Запуск: python -X utf8 tests\\test_forum_h6_blockers.py"""
import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_forum_tasks as T  # noqa: E402
from herald import telegram  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_forum_lock import Base as LockBase, gmsg, FORUM, OWNER  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "..", "herald")


class Guest(LockBase):
    def setUp(self):
        super().setUp()
        self.lines = []
        self.bot.log = self.lines.append

    def test_G1_a_guest_message_update_is_dropped_with_one_log_line(self):
        inner = gmsg(1)["message"]
        heard = self.hear({"update_id": 1, "guest_message": inner})
        self.assertEqual(heard, [])
        self.assertEqual(self.bot.ignored, 1)
        self.assertEqual(len([x for x in self.lines if "guest" in x.lower()]), 1, f"в лог ушло: {self.lines}")

    def test_G2_a_message_with_guest_query_id_is_dropped_with_one_log_line(self):
        heard = self.hear(gmsg(1, guest_query_id="Q1"))
        self.assertEqual(heard, [], "сообщение с guest_query_id услышано как обычное")
        self.assertEqual(len([x for x in self.lines if "guest" in x.lower()]), 1, f"в лог ушло: {self.lines}")

    def test_G3_the_drop_does_not_swallow_the_next_normal_message(self):
        heard = self.hear(gmsg(1, guest_query_id="Q1"), gmsg(2, text="real"))
        self.assertEqual([m.text for m in heard], ["real"])

    def test_G4_GUARD_nothing_in_herald_answers_a_guest_or_deletes_a_topic(self):
        src = ""
        for p in glob.glob(os.path.join(PKG, "*.py")):
            with open(p, encoding="utf-8") as f:
                src += f.read()
        for word in ("answerGuestQuery", "deleteForumTopic"):
            self.assertNotIn(word, src)

    def test_G5_GUARD_allowed_updates_stay_narrow(self):
        self.hear()
        call = [p for m, p in self.api.calls if m == "getUpdates"][-1]
        self.assertTrue(set(call["allowed_updates"]) <= {"message", "callback_query"}, call)


class Seen(T.Base):
    def test_M1_a_thousand_unique_echoes_do_not_grow_seen_without_bound(self):
        self.make(peer_bots=(T.PEER,), max_bot_hops=2, bot_turns_per_hour=99, bot_reply_gap_s=0)
        t = self.new_task("echo")
        for i in range(1000):
            self.peer(t, f"echo {i}")
        self.assertLessEqual(len(self.h.forum._seen), 512, "len(_seen) = %d" % len(self.h.forum._seen))

    def test_M2_dedup_of_the_latest_text_survives_the_bound(self):
        big = 10 ** 6                                        # ни хопы, ни часовые потолки не должны скрыть потерю дедупа
        self.make(peer_bots=(T.PEER,), max_bot_hops=big, bot_turns_per_hour=big, bot_turns_per_hour_forum=big, bot_reply_gap_s=0)
        t = self.new_task("echo")
        for i in range(1000):
            self.clock.t += 0.01                             # разные времена, все в окне дедупа (600 с): «самый новый» отличим от «самого старого»
            self.peer(t, f"echo {i}")
        n = len(self.engine.calls)
        self.assertGreater(n, 900, "предпосылка: каждое эхо дало ход (%d)" % n)
        self.peer(t, "echo 999")
        self.assertEqual(len(self.engine.calls), n, "последний текст перестал дедупиться")


class Pace(T.Base):
    # C1 (общий потолок форума) переехал в test_forum_h6_pace.py — отдельное имя bot_turns_per_hour_forum.

    def test_C2_the_gap_is_per_pair_across_topics(self):
        self.make(peer_bots=(T.PEER,), max_bot_hops=99, bot_turns_per_hour=99, bot_reply_gap_s=5)
        a, b = self.new_task("a"), self.new_task("b")
        self.clock.t = 1000.0
        self.peer(a, "x")
        self.peer(b, "y")
        self.assertEqual(len(self.engine.calls), 1, "та же пара бот-бот ответила в двух темах в одну секунду")

    def test_C3_GUARD_after_the_gap_the_other_topic_gets_its_turn(self):
        self.make(peer_bots=(T.PEER,), max_bot_hops=99, bot_turns_per_hour=99, bot_reply_gap_s=5)
        a, b = self.new_task("a"), self.new_task("b")
        self.clock.t = 1000.0
        self.peer(a, "x")
        self.clock.t = 1006.0
        self.peer(b, "y")
        self.assertEqual(len(self.engine.calls), 2)

    def test_C4_GUARD_two_different_peers_do_not_block_each_other(self):
        self.make(peer_bots=(T.PEER, T.PEER2), max_bot_hops=99, bot_turns_per_hour=99, bot_reply_gap_s=5)
        a, b = self.new_task("a"), self.new_task("b")
        self.clock.t = 1000.0
        self.peer(a, "x", who=T.PEER)
        self.peer(b, "y", who=T.PEER2)
        self.assertEqual(len(self.engine.calls), 2)


class StuckBot(T.FakeForumBot):
    """Как настоящий Telegram: то же имя второй раз -> 400 TOPIC_NOT_MODIFIED."""

    def __init__(self):
        super().__init__()
        self.names = {}
        self.closed_in_ui = set()
        self.open_topics = set()
        self.close_already = False
        self.reopen_fails = False

    def edit_topic(self, thread_id, name):
        if self.fail_edit:
            raise telegram.TelegramError("editForumTopic: 400 boom")
        if self.names.get(thread_id) == name:
            raise telegram.TelegramError("editForumTopic: 400 Bad Request: TOPIC_NOT_MODIFIED")
        self.names[thread_id] = name
        self.topic.append(("edit", thread_id, name))

    def close_topic(self, thread_id):
        if self.fail_close:
            raise telegram.TelegramError("closeForumTopic: 400 boom")
        self.topic.append(("close", thread_id))
        if self.close_already:
            raise telegram.TelegramError("closeForumTopic: 400 Bad Request: TOPIC_NOT_MODIFIED")

    def reopen_topic(self, thread_id):
        self.topic.append(("reopen", thread_id))
        if self.reopen_fails:
            raise telegram.TelegramError("reopenForumTopic: 400 boom")
        if thread_id not in self.closed_in_ui and thread_id in self.open_topics:
            raise telegram.TelegramError("reopenForumTopic: 400 Bad Request: TOPIC_NOT_MODIFIED")


class Done(T.Base):
    def setUp(self):
        super().setUp()
        self.bot = StuckBot()
        self.make()
        self.t = self.new_task("ship it")

    def test_I1_edit_ok_close_failed_then_a_second_done_closes(self):
        self.bot.fail_close = True
        self.owner(self.t, "/done")
        self.tap(self.t, f"task:done:{self.t}")
        self.assertEqual(self.state(self.t), "open")
        self.bot.fail_close = False
        self.tap(self.t, f"task:done:{self.t}")
        self.assertEqual(self.state(self.t), "closed", "повторный /done не закрыл: имя уже «✅», edit упал TOPIC_NOT_MODIFIED")
        self.assertEqual(len(self.bot.of("close")), 1)

    def test_I2_topic_not_modified_on_edit_is_success_close_still_called(self):
        self.bot.names[self.t] = "✅ ship it"
        self.tap(self.t, f"task:done:{self.t}")
        self.assertEqual(len(self.bot.of("close")), 1, "close не вызван после TOPIC_NOT_MODIFIED")
        self.assertEqual(self.state(self.t), "closed")

    def test_I4_GUARD_topic_not_modified_on_close_is_success(self):
        self.bot.close_already = True                       # уже закрыта в Telegram (в UI)
        self.tap(self.t, f"task:done:{self.t}")
        self.assertEqual(self.state(self.t), "closed")

    def test_I3_GUARD_a_real_edit_failure_still_keeps_the_topic_open(self):
        self.bot.fail_edit = True
        self.tap(self.t, f"task:done:{self.t}")
        self.assertEqual(self.state(self.t), "open")
        self.assertEqual(self.bot.of("close"), [])


class Reopen(T.Base):
    def setUp(self):
        super().setUp()
        self.bot = StuckBot()
        self.make()
        self.t = self.new_task("held")

    def test_R1_tasks_json_closed_but_telegram_already_open_converges(self):
        self.tap(self.t, f"task:done:{self.t}")
        self.assertEqual(self.state(self.t), "closed")
        self.bot.open_topics.add(self.t)                    # кто-то открыл тему руками: reopen -> TOPIC_NOT_MODIFIED
        self.owner(self.t, "/reopen")
        self.assertEqual(self.state(self.t), "open", "расхождение с Telegram: тема застряла closed")

    def test_R2_tasks_json_open_but_the_topic_was_closed_in_the_ui(self):
        self.bot.closed_in_ui.add(self.t)
        self.owner(self.t, "/reopen")
        self.assertEqual(len(self.bot.of("reopen")), 1, "/reopen ответил «Already open.» и не позвал Telegram")

    def test_R4_GUARD_a_real_reopen_failure_keeps_the_topic_closed(self):
        self.tap(self.t, f"task:done:{self.t}")
        self.bot.reopen_fails = True
        self.owner(self.t, "/reopen")
        self.assertEqual(self.state(self.t), "closed")

    def test_R3_GUARD_reopen_of_a_normally_closed_topic_still_works(self):
        self.tap(self.t, f"task:done:{self.t}")
        self.bot.closed_in_ui.add(self.t)
        self.owner(self.t, "/reopen")
        self.assertEqual(self.state(self.t), "open")
        self.assertEqual(len(self.bot.of("reopen")), 1)


class BadTasks(T.Base):
    def setUp(self):
        super().setUp()
        self.make()
        self.path = os.path.join(self.control, "tasks.json")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def _log_text(self):
        p = os.path.join(self.control, "audit.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def test_B1_valid_json_with_tasks_not_a_dict_goes_to_bad_with_a_log_line(self):
        for n, body in enumerate(('{"tasks": [], "done_topic": null}', '{"tasks": "x"}', '{"tasks": null}')):
            for p in (self.path, self.path + ".bad"):
                if os.path.exists(p):
                    os.remove(p)
            open(os.path.join(self.control, "audit.log"), "w").close()
            self._write(body)
            self.owner(0, "/tasks")
            self.assertTrue(os.path.exists(self.path + ".bad"), f"[{body}] не ушёл в .bad")
            with open(self.path + ".bad", encoding="utf-8") as f:
                self.assertEqual(f.read(), body)
            self.assertIn("tasks.json", self._log_text(), f"[{body}] нет строки в audit.log")

    def test_B2_the_odd_file_is_not_overwritten_by_the_next_new_task(self):
        self._write('{"tasks": [1, 2], "done_topic": 5}')
        self.new_task("fresh")
        self.assertTrue(os.path.exists(self.path + ".bad"), "новая задача затёрла чужой tasks.json без .bad")
        with open(self.path + ".bad", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"tasks": [1, 2], "done_topic": 5})

    def test_B3_GUARD_broken_json_already_goes_to_bad(self):
        self._write("{not json")
        self.owner(0, "/tasks")
        self.assertTrue(os.path.exists(self.path + ".bad"))


class PersonaEngine:
    def __init__(self):
        self.calls = []

    def __call__(self, prompt, workspace, cfg, session_id=None):
        sysf = getattr(cfg, "append_system_prompt_file", "") or ""
        text = open(sysf, encoding="utf-8").read() if sysf and os.path.exists(sysf) else ""
        self.calls.append({"prompt": prompt, "system": text})
        return TurnResult(True, "ok", "s%d" % len(self.calls), 0.1)


class PeerPrompt(T.Base):
    NB, TOC, PERSONA = "NOTEBOOK-MARK-7", "SKILLS-TOC-MARK-7", "PERSONA-MARK-7"

    def setUp(self):
        super().setUp()
        persona = os.path.join(self.control, "persona.md")
        with open(persona, "w", encoding="utf-8") as f:
            f.write(self.PERSONA)
        self.engine = PersonaEngine()
        from herald import speak_first
        gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(self.control, "s.json"),
                                os.path.join(self.control, "STOP"))
        self.h = T.HeraldCls(self.bot, self.engine, EngineConfig(append_system_prompt_file=persona), gate, [], self.control,
                             self.ws, {T.OWNER}, forum_chat_id=T.FORUM, peer_bots=(T.PEER,), max_bot_hops=99,
                             bot_turns_per_hour=99, bot_reply_gap_s=0, transcript_roots=[], room_sync=True, now=self.clock)
        self.h.nb.add_memory(self.NB)
        self.h.skills_toc = self.TOC
        self.t = self.new_task("prompts")

    def test_P1_a_peer_turn_gets_persona_only(self):
        self.peer(self.t, "hello from the other bot")
        self.assertEqual(len(self.engine.calls), 1)
        s = self.engine.calls[0]["system"]
        self.assertIn(self.PERSONA, s, "персона пропала")
        self.assertNotIn(self.NB, s, "тетрадь владельца ушла чужому боту")
        self.assertNotIn(self.TOC, s, "skills_toc ушёл чужому боту")

    def test_P2_GUARD_the_owner_turn_in_the_same_topic_gets_everything(self):
        self.owner(self.t, "hi")
        s = self.engine.calls[0]["system"]
        for mark in (self.PERSONA, self.NB, self.TOC):
            self.assertIn(mark, s)

    def test_P3_the_peer_turn_after_an_owner_turn_still_gets_no_notebook(self):
        self.owner(self.t, "hi")
        self.peer(self.t, "and me")
        s = self.engine.calls[1]["system"]
        self.assertNotIn(self.NB, s)
        self.assertNotIn(self.TOC, s)


if __name__ == "__main__":
    unittest.main()
