"""Red tests: a tap on a closed task says the truth; doctor checks the forum; config keys forum_chat_id / peer_bots."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import doctor, run, telegram  # noqa: E402
from fake_telegram import FakeTelegram  # noqa: E402
import test_forum_tasks as T  # noqa: E402

TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"
FORUM = T.FORUM


class TapOnClosed(T.Base):
    def closed(self):
        a = self.new_task("fix login")
        self.owner(a, "/done")
        data = self.bot.buttons[-1][2][0][1]
        self.tap(a, data)
        return a, data

    def test_H4_1_second_tap_on_a_closed_task_says_already_closed_and_touches_nothing(self):
        self.make()
        a, data = self.closed()
        edits, closes = len(self.bot.of("edit")), len(self.bot.of("close"))
        self.tap(a, data)
        self.assertEqual(self.bot.answers[-1][1], "Already closed.")
        self.assertEqual((len(self.bot.of("edit")), len(self.bot.of("close"))), (edits, closes))
        self.assertEqual(self.state(a), "closed")

    def test_H4_2_keep_on_an_open_task_still_says_left_open(self):
        self.make()
        a = self.new_task("fix login")
        self.owner(a, "/done")
        self.tap(a, self.bot.buttons[-1][2][1][1])
        self.assertEqual(self.bot.answers[-1][1], "Left open.")


def reply(api, chat, member, me=None):
    api.replies["getChat"] = [(200, {"ok": True, "result": chat})] if not isinstance(chat, tuple) else [chat]
    api.replies["getMe"] = [(200, {"ok": True, "result": me or {"id": 555, "username": "hb", "is_bot": True}})]
    api.replies["getChatMember"] = [(200, {"ok": True, "result": member})] if not isinstance(member, tuple) else [member]


GOOD_CHAT = {"id": FORUM, "type": "supergroup", "is_forum": True}


class ForumCheck(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        self.bot = telegram.Bot(TOKEN, owners={1}, api_base=self.api.url)

    def tearDown(self):
        self.api.close()

    def check(self):
        return doctor.check_forum(self.bot, FORUM)

    def test_admin_with_manage_topics_passes_and_the_bots_own_id_is_asked(self):
        reply(self.api, GOOD_CHAT, {"status": "administrator", "can_manage_topics": True})
        r = self.check()
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(self.api.of("getChat")[-1]["chat_id"], FORUM)
        self.assertEqual(self.api.of("getChatMember")[-1], {"chat_id": FORUM, "user_id": 555})

    def test_creator_passes(self):
        reply(self.api, GOOD_CHAT, {"status": "creator"})
        self.assertTrue(self.check().ok)

    def test_a_plain_group_fails_and_says_what_to_turn_on(self):
        reply(self.api, {"id": FORUM, "type": "group"}, {"status": "administrator", "can_manage_topics": True})
        r = self.check()
        self.assertFalse(r.ok)
        self.assertIn("Topics", r.detail)

    def test_supergroup_without_topics_fails(self):
        reply(self.api, {"id": FORUM, "type": "supergroup"}, {"status": "administrator", "can_manage_topics": True})
        r = self.check()
        self.assertFalse(r.ok)
        self.assertIn("Topics", r.detail)

    def test_admin_without_manage_topics_fails_and_names_the_right(self):
        reply(self.api, GOOD_CHAT, {"status": "administrator", "can_manage_topics": False})
        r = self.check()
        self.assertFalse(r.ok)
        self.assertIn("Manage Topics", r.detail)

    def test_plain_member_fails(self):
        reply(self.api, GOOD_CHAT, {"status": "member"})
        r = self.check()
        self.assertFalse(r.ok)
        self.assertIn("Manage Topics", r.detail)

    def test_api_error_fails_without_leaking_the_token(self):
        reply(self.api, (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}), {})
        r = self.check()
        self.assertFalse(r.ok)
        self.assertNotIn(TOKEN.split(":")[1], r.detail)

    def test_a_forum_problem_is_a_warning_not_a_block(self):
        reply(self.api, {"id": FORUM, "type": "group"}, {})
        self.assertFalse(self.check().fatal)        # private chats still work


class RunAll(unittest.TestCase):
    def run_all(self, **kw):
        cfg = run.engine.EngineConfig(tools=[])
        ok = doctor.Check("x", True, "fine")
        with mock.patch.object(doctor, "check_claude_flags", return_value=ok), \
                mock.patch.object(doctor, "check_token_live", return_value=ok), \
                mock.patch.object(doctor, "canary", return_value=ok), \
                mock.patch.object(doctor, "check_ffmpeg", return_value=ok), \
                mock.patch.object(doctor, "check_forum", return_value=doctor.Check("forum", True, "ok")) as cf:
            return doctor.run_all(cfg, TOKEN, [1], None, **kw), cf

    def test_without_a_forum_no_forum_check_runs(self):
        checks, cf = self.run_all()
        cf.assert_not_called()
        self.assertNotIn("forum", [c.name for c in checks])

    def test_with_a_forum_the_check_is_in_the_report(self):
        checks, cf = self.run_all(forum_chat_id=FORUM)
        cf.assert_called_once()
        self.assertEqual(cf.call_args[0][1], FORUM)
        self.assertIn("forum", [c.name for c in checks])


class Config(unittest.TestCase):
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

    def test_a_config_without_forum_keys_loads_exactly_as_before(self):
        loaded = run.load_config(self.write())
        self.assertEqual(sorted(loaded), ["control", "owners", "workspace"])        # nothing added, nothing renamed
        self.assertEqual(loaded["owners"], [1001])

    def test_forum_keys_are_known_and_normalised_to_numbers(self):
        loaded = run.load_config(self.write(forum_chat_id=str(FORUM), peer_bots=[7001, "7002"]))
        self.assertEqual(loaded["forum_chat_id"], FORUM)
        self.assertEqual(loaded["peer_bots"], [7001, 7002])

    def test_bad_forum_values_are_refused(self):
        for extra in ({"forum_chat_id": True}, {"forum_chat_id": "abc"}, {"forum_chat_id": 5},      # a forum id is a negative supergroup id
                      {"forum_chat_id": FORUM, "peer_bots": ["@somebot"]}, {"forum_chat_id": FORUM, "peer_bots": "7001"},
                      {"forum_chat_id": FORUM, "peer_bots": [True]}):
            with self.assertRaises(ValueError, msg=str(extra)) as cm:
                run.load_config(self.write(**extra))
            self.assertNotIn("unknown config keys", str(cm.exception), "refused for its value, not as an unknown key")

    def test_peer_bots_without_a_forum_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            run.load_config(self.write(peer_bots=[7001]))
        self.assertNotIn("unknown config keys", str(cm.exception))
        self.assertIn("forum_chat_id", str(cm.exception))

    def test_build_without_forum_has_no_forum(self):
        h, _ = run.build(run.load_config(self.write()), TOKEN)
        self.assertIsNone(h.forum)
        self.assertIsNone(h.bot.forum_chat_id)

    def test_build_with_forum_wires_herald_and_the_bot(self):
        h, _ = run.build(run.load_config(self.write(forum_chat_id=FORUM, peer_bots=[7001])), TOKEN)
        self.assertIsNotNone(h.forum)
        self.assertEqual(h.bot.forum_chat_id, FORUM)
        self.assertEqual(h.bot.peer_bots, {7001})

    def test_main_run_hands_the_forum_id_to_the_doctor(self):
        p = self.write(forum_chat_id=FORUM)
        with mock.patch.object(doctor, "run_all", return_value=[doctor.Check("x", False, "stop")]) as ra:
            self.assertEqual(run.main_run(p, TOKEN), 1)
        self.assertEqual(ra.call_args.kwargs.get("forum_chat_id"), FORUM)


if __name__ == "__main__":
    unittest.main()
