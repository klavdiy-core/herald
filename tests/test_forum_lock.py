"""Красные тесты (слой Bot / замок) для herald: темы форума + боты-пиры.
Были красными на telegram.py 0.23.7 (там нет forum_chat_id).

КОНТРАКТ слоя Bot (имена фиксированы):
  Bot(token, owners, api_base, ..., forum_chat_id=None, peer_bots=())      peer_bots — ТОЛЬКО числовые id (str/@username -> ValueError)
  Message получает поля:  thread_id: int = 0 · from_id: int = 0 · from_bot: bool = False
  _accept: сообщение/нажатие из группы принимается, только если ВСЁ: chat.id==forum_chat_id · chat.type=="supergroup" · chat.is_forum ·
           (from.id in owners and not is_bot)  ИЛИ  (from.is_bot and from.id in peer_bots — только для message, не callback)
  send_text / send_buttons (chat_id, ..., thread_id=None): thread_id -> message_thread_id в sendMessage; ключа нет, если None.
  _check: owners + forum_chat_id, больше ничего.
  create_topic(name, color=None)->thread_id · edit_topic(thread_id, name) · close_topic(thread_id) · reopen_topic(thread_id)
           (имя обрезается до 128; color вне {0x6FB9F0,0xFFD67E,0xCB86DB,0x8EEE98,0xFF93B2,0xFB6F5F} -> ValueError;
            без forum_chat_id -> ValueError). delete_topic НЕТ вообще, 'deleteForumTopic' нигде в herald/.
Запуск: python -X utf8 -m unittest tests.test_forum_lock
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fake_telegram import FakeTelegram  # noqa: E402

from herald import telegram  # noqa: E402

TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"
OWNER, STRANGER = 1001, 2002
FORUM, OTHER = -1001111111111, -1002222222222
PEER, STRANGER_BOT = 7001, 7002


def gmsg(uid, text="hi", chat=FORUM, frm=OWNER, bot=False, thread=42, ctype="supergroup", forum=True, username=None, **extra):
    c = {"id": chat, "type": ctype}
    if forum is not None:
        c["is_forum"] = forum
    f = {"id": frm, "is_bot": bot}
    if username:
        f["username"] = username
    m = {"message_id": uid, "date": 0, "chat": c, "from": f, "text": text, "is_topic_message": True}
    if thread:
        m["message_thread_id"] = thread
    m.update(extra)
    return {"update_id": uid, "message": m}


def gcb(uid, data, chat=FORUM, frm=OWNER, thread=42, ctype="supergroup", forum=True):
    c = {"id": chat, "type": ctype}
    if forum is not None:
        c["is_forum"] = forum
    m = {"message_id": 9, "chat": c}
    if thread:
        m["message_thread_id"] = thread
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "from": {"id": frm, "is_bot": False}, "data": data, "message": m}}


class Base(unittest.TestCase):
    forum, peers = FORUM, ()

    def setUp(self):
        self.api = FakeTelegram()
        kw = {}
        if self.forum is not None:
            kw["forum_chat_id"] = self.forum
        if self.peers:
            kw["peer_bots"] = self.peers
        self.bot = telegram.Bot(TOKEN, owners={OWNER}, api_base=self.api.url, sleep=lambda s: None, **kw)

    def tearDown(self):
        self.api.close()

    def hear(self, *ups):
        self.api.replies["getUpdates"] = [(200, {"ok": True, "result": list(ups)})]
        return self.bot.poll(0, timeout=0)[0]


class Lock(Base):
    def test_L1_owner_in_forum_thread_is_heard_with_thread_id(self):
        (m,) = self.hear(gmsg(1, thread=42))
        self.assertEqual((m.chat_id, m.thread_id, m.from_id, m.from_bot), (FORUM, 42, OWNER, False))
        self.assertEqual(m.text, "hi")

    def test_L2_stranger_in_forum_is_ignored(self):
        self.assertEqual(self.hear(gmsg(1, frm=STRANGER)), [])
        self.assertEqual(self.bot.ignored, 1)

    def test_L3_owner_in_another_supergroup_is_ignored(self):
        self.assertEqual(self.hear(gmsg(1, chat=OTHER)), [])

    def test_L4_forum_chat_without_is_forum_is_ignored(self):
        self.assertEqual(self.hear(gmsg(1, forum=False), gmsg(2, forum=None)), [])

    def test_L5_plain_group_type_is_ignored(self):
        self.assertEqual(self.hear(gmsg(1, ctype="group")), [])

    def test_L6_forwarded_in_group_is_marked_like_in_private(self):
        (m,) = self.hear(gmsg(1, forward_origin={"type": "user"}))
        self.assertTrue(m.forwarded)

    def test_L7_general_topic_has_thread_id_zero(self):
        (m,) = self.hear(gmsg(1, thread=0))
        self.assertEqual(m.thread_id, 0)

    def test_L8_callbacks_same_lock(self):
        (c,) = self.hear(gcb(1, "done:yes", thread=42))
        self.assertEqual((c.kind, c.chat_id, c.thread_id, c.from_id, c.text), ("callback", FORUM, 42, OWNER, "done:yes"))
        self.assertEqual(self.hear(gcb(2, "x", frm=STRANGER), gcb(3, "x", chat=OTHER), gcb(4, "x", forum=False)), [])

    def test_L9_private_chat_still_works_and_has_no_thread(self):
        up = {"update_id": 5, "message": {"message_id": 5, "chat": {"id": OWNER, "type": "private"},
                                          "from": {"id": OWNER, "is_bot": False}, "text": "hi"}}
        (m,) = self.hear(up)
        self.assertEqual((m.chat_id, m.thread_id), (OWNER, 0))


class Regress(Base):
    forum = None

    def test_R1_without_forum_chat_id_any_group_message_is_ignored(self):
        self.assertEqual(self.hear(gmsg(1), gmsg(2, chat=OTHER), gcb(3, "x")), [])
        self.assertEqual(self.bot.ignored, 3)

    def test_R2_without_forum_chat_id_nothing_but_owners_can_be_written_to(self):
        with self.assertRaises(PermissionError):
            self.bot.send_text(FORUM, "x")

    def test_R3_private_send_has_no_thread_key(self):
        self.bot.send_text(OWNER, "x")
        (p,) = self.api.of("sendMessage")
        self.assertNotIn("message_thread_id", p)


class Out(Base):
    def test_O1_reply_carries_the_thread(self):
        self.bot.send_text(FORUM, "answer", thread_id=42)
        (p,) = self.api.of("sendMessage")
        self.assertEqual((p["chat_id"], p["message_thread_id"], p["text"]), (FORUM, 42, "answer"))

    def test_O2_buttons_carry_the_thread(self):
        self.bot.send_buttons(FORUM, "close?", [("yes", "done:yes")], thread_id=42)
        (p,) = self.api.of("sendMessage")
        self.assertEqual(p["message_thread_id"], 42)
        self.assertIn("reply_markup", p)

    def test_O3_other_groups_and_strangers_refused(self):
        for chat in (OTHER, STRANGER, -100):
            with self.assertRaises(PermissionError, msg=str(chat)):
                self.bot.send_text(chat, "x", thread_id=1)
        self.assertEqual(self.api.of("sendMessage"), [])

    def test_O4_long_answer_every_part_in_the_thread(self):
        self.bot.send_text(FORUM, "a" * 5000, thread_id=7)
        ps = self.api.of("sendMessage")
        self.assertGreater(len(ps), 1)
        self.assertTrue(all(p["message_thread_id"] == 7 for p in ps))


class Topics(Base):
    def test_T1_create_returns_thread_id_and_targets_only_the_forum(self):
        self.api.replies["createForumTopic"] = [(200, {"ok": True, "result": {"message_thread_id": 77, "name": "t"}})]
        self.assertEqual(self.bot.create_topic("task one"), 77)
        (p,) = self.api.of("createForumTopic")
        self.assertEqual((p["chat_id"], p["name"]), (FORUM, "task one"))

    def test_T2_names_are_cut_to_128(self):
        self.api.replies["createForumTopic"] = [(200, {"ok": True, "result": {"message_thread_id": 1}})]
        self.bot.create_topic("n" * 300)
        self.bot.edit_topic(1, "✅ " + "n" * 300)
        self.assertLessEqual(len(self.api.of("createForumTopic")[0]["name"]), 128)
        self.assertLessEqual(len(self.api.of("editForumTopic")[0]["name"]), 128)

    def test_T3_bad_icon_colour_is_refused_before_any_call(self):
        with self.assertRaises(ValueError):
            self.bot.create_topic("x", color=0x123456)
        self.assertEqual(self.api.of("createForumTopic"), [])
        self.api.replies["createForumTopic"] = [(200, {"ok": True, "result": {"message_thread_id": 1}})]
        self.bot.create_topic("x", color=0x8EEE98)
        self.assertEqual(self.api.of("createForumTopic")[0]["icon_color"], 0x8EEE98)

    def test_T4_close_and_reopen_params(self):
        self.bot.close_topic(42)
        self.bot.reopen_topic(42)
        self.assertEqual(self.api.of("closeForumTopic"), [{"chat_id": FORUM, "message_thread_id": 42}])
        self.assertEqual(self.api.of("reopenForumTopic"), [{"chat_id": FORUM, "message_thread_id": 42}])

    def test_T5_a_failed_close_raises_and_does_not_pretend(self):
        self.api.replies["closeForumTopic"] = [(400, {"ok": False, "error_code": 400, "description": "TOPIC_NOT_MODIFIED"})]
        with self.assertRaises(telegram.TelegramError):
            self.bot.close_topic(42)

    def test_T6_delete_does_not_exist_anywhere(self):
        self.assertFalse(hasattr(self.bot, "delete_topic"))
        root = os.path.join(os.path.dirname(__file__), "..", "herald")
        for fn in os.listdir(root):
            if fn.endswith(".py"):
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    self.assertNotIn("deleteForumTopic", f.read(), fn)

    def test_T7_topics_need_a_forum_chat(self):
        b = telegram.Bot(TOKEN, owners={OWNER}, api_base=self.api.url)
        with self.assertRaises(ValueError):
            b.create_topic("x")
        self.assertEqual(self.api.of("createForumTopic"), [])


class Peers(Base):
    peers = (PEER,)

    def test_B1_peer_bot_in_forum_is_heard_marked_as_bot(self):
        (m,) = self.hear(gmsg(1, frm=PEER, bot=True))
        self.assertEqual((m.from_bot, m.from_id, m.chat_id, m.thread_id), (True, PEER, FORUM, 42))

    def test_B2_bot_not_in_the_list_is_ignored(self):
        self.assertEqual(self.hear(gmsg(1, frm=STRANGER_BOT, bot=True)), [])

    def test_B3_peer_bot_elsewhere_is_ignored(self):
        priv = {"update_id": 2, "message": {"message_id": 2, "chat": {"id": PEER, "type": "private"},
                                            "from": {"id": PEER, "is_bot": True}, "text": "x"}}
        self.assertEqual(self.hear(gmsg(1, frm=PEER, bot=True, chat=OTHER), gmsg(3, frm=PEER, bot=True, forum=False), priv), [])

    def test_B4_the_list_is_ids_not_usernames(self):
        for bad in ("@peer_bot", "peer_bot"):
            with self.assertRaises(ValueError, msg=bad):
                telegram.Bot(TOKEN, owners={OWNER}, api_base=self.api.url, forum_chat_id=FORUM, peer_bots=(bad,))
        # чужой бот, назвавшийся именем пира, но с другим id — не пускаем
        self.assertEqual(self.hear(gmsg(1, frm=STRANGER_BOT, bot=True, username="peer_bot")), [])

    def test_B5_a_message_that_claims_is_bot_false_but_has_a_peer_id_is_not_a_peer(self):
        # id в списке пиров, но is_bot=False: это не бот и не владелец -> молчим
        self.assertEqual(self.hear(gmsg(1, frm=PEER, bot=False)), [])

    def test_B6_a_peer_cannot_press_buttons(self):
        up = gcb(1, "done:yes", frm=PEER)
        up["callback_query"]["from"]["is_bot"] = True
        self.assertEqual(self.hear(up), [])

    def test_B7_owner_still_not_a_bot(self):
        (m,) = self.hear(gmsg(1, frm=OWNER))
        self.assertFalse(m.from_bot)


class PeersNeedForum(Base):
    forum, peers = None, (PEER,)

    def test_B8_peers_without_forum_chat_id_hear_nothing(self):
        self.assertEqual(self.hear(gmsg(1, frm=PEER, bot=True)), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
