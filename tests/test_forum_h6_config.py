"""Ключи конфига бот-потолков run.py BOT_LIMIT_KEYS.

КОНТРАКТ (поведение load_config/build; единственные внутренние имена — атрибуты Forum в K4):
  max_bot_hops, bot_turns_per_hour, bot_turns_per_hour_forum, bot_reply_gap_s — известные ключи config.json.
  С forum_chat_id: целое число (не bool, не float, не строка, не null) >= 1; bot_reply_gap_s >= 0 (0 можно).
  Без forum_chat_id любой из четырёх — ValueError, где названы ключ и forum_chat_id (и это не «unknown config keys»).
  build(): значения доходят до Forum (K4); без ключей — прежние умолчания 6 / 10 / 30 / 5 (K5).
Запуск: python -X utf8 -m unittest tests.test_forum_h6_config"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import run  # noqa: E402

FORUM, TOKEN = -1001111111111, "123456789:AAH-fake_token_value_for_tests_00000"
KEYS = ("max_bot_hops", "bot_turns_per_hour", "bot_turns_per_hour_forum", "bot_reply_gap_s")


class BotLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **extra):
        cfg = {"owners": [1001], "workspace": os.path.join(self.dir, "w"), "control": os.path.join(self.dir, "c")}
        cfg.update(extra)
        p = os.path.join(self.dir, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p

    def refused(self, **extra):
        with self.assertRaises(ValueError, msg=str(extra)) as cm:
            run.load_config(self.write(**extra))
        self.assertNotIn("unknown config keys", str(cm.exception), "отказ должен быть по значению, а не «неизвестный ключ»: %s" % extra)
        return str(cm.exception)

    def test_K1_each_key_is_accepted_with_a_forum(self):
        for k, v in (("max_bot_hops", 3), ("bot_turns_per_hour", 5), ("bot_turns_per_hour_forum", 20), ("bot_reply_gap_s", 7), ("bot_reply_gap_s", 0)):
            loaded = run.load_config(self.write(forum_chat_id=FORUM, **{k: v}))
            self.assertEqual(loaded[k], v)

    def test_K2_without_forum_chat_id_every_key_is_refused_naming_both(self):
        for k in KEYS:
            msg = self.refused(**{k: 3})
            self.assertIn(k, msg)
            self.assertIn("forum_chat_id", msg)

    def test_K3_bad_values_are_refused_with_a_forum(self):
        for k in KEYS[:3]:
            for bad in (True, False, 0, -1, 1.5, "5", None, [3]):
                self.refused(forum_chat_id=FORUM, **{k: bad})
        for bad in (True, False, -1, 1.5, "5", None, [3]):
            self.refused(forum_chat_id=FORUM, bot_reply_gap_s=bad)

    def test_K4_build_hands_the_numbers_to_the_forum(self):
        h, _ = run.build(run.load_config(self.write(forum_chat_id=FORUM, peer_bots=[7001], max_bot_hops=3, bot_turns_per_hour=5,
                                                     bot_turns_per_hour_forum=20, bot_reply_gap_s=7)), TOKEN)
        f = h.forum
        self.assertEqual((f.max_hops, f.per_hour, f.per_hour_forum, f.gap_s), (3, 5, 20, 7))

    def test_K5_GUARD_without_the_keys_the_defaults_stay(self):
        h, _ = run.build(run.load_config(self.write(forum_chat_id=FORUM, peer_bots=[7001])), TOKEN)
        f = h.forum
        self.assertEqual((f.max_hops, f.per_hour, f.per_hour_forum, f.gap_s), (6, 10, 30, 5))

    def test_K6_GUARD_an_unknown_neighbour_key_is_still_refused(self):
        with self.assertRaises(ValueError) as cm:
            run.load_config(self.write(forum_chat_id=FORUM, bot_turns_per_day=9))
        self.assertIn("unknown config keys", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
