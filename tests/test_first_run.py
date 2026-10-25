"""herald init (round 3 next to Hermes, 25.10: setup 6.5 / 7.5 - a hand-edited JSON and a user id from @userinfobot)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import first_run, run  # noqa: E402
from fake_telegram import FakeTelegram  # noqa: E402

TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"


EMPTY = (200, {"ok": True, "result": []})


def msg(update_id, from_id, chat_type="private", is_bot=False):
    return {"update_id": update_id, "message": {"message_id": update_id, "date": 0,
                                                 "chat": {"id": from_id, "type": chat_type},
                                                 "from": {"id": from_id, "is_bot": is_bot, "first_name": "Ann"},
                                                 "text": "my code 111111"}}


class Init(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        self.api.replies["getMe"] = [(200, {"ok": True, "result": {"username": "my_test_bot"}})]
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.lines = []

    def tearDown(self):
        os.chdir(self.cwd)
        self.api.close()
        self.tmp.cleanup()

    def init(self, **k):
        k.setdefault("code", "111111")
        return first_run.init("control", TOKEN, api_base=self.api.url, wait_s=5, poll_s=0, out=self.lines.append, **k)

    def test_the_first_private_sender_becomes_the_owner_and_a_config_loads(self):
        self.api.replies["getUpdates"] = [EMPTY, (200, {"ok": True, "result": [msg(7, 555, "group"), msg(8, 999, is_bot=True),
                                                                        msg(9, 4242)]}),
                                          (200, {"ok": True, "result": []})]
        self.assertEqual(self.init(), 0)
        cfg = run.load_config(os.path.join("control", "config.json"))       # what run would load, unchanged
        self.assertEqual(cfg["owners"], [4242])
        self.assertEqual(cfg["max_usd_per_day"], 5.0)
        self.assertTrue(os.path.isfile(os.path.join("control", "persona.md")))
        self.assertTrue(os.path.isdir("workspace"))
        self.assertIn("@my_test_bot", " ".join(self.lines))
        self.assertEqual(self.api.of("getUpdates")[-1]["offset"], 10)       # the message is marked read

    def test_only_the_sender_of_the_printed_code_becomes_the_owner(self):
        # an outside review (25.10): "the first to write is the owner" - with offset 0, someone who wrote to the bot
        # before init ran was already in the queue
        self.api.replies["getUpdates"] = [(200, {"ok": True, "result": []})]
        code = first_run.pair_code()
        self.assertRegex(code, r"^\d{6}$")
        stranger, owner = msg(20, 666), msg(21, 4242)
        stranger["message"]["text"] = "hi bot"
        owner["message"]["text"] = f"my code {code}"
        self.api.replies["getUpdates"] = [EMPTY, (200, {"ok": True, "result": [stranger, owner]}),
                                          (200, {"ok": True, "result": []})]
        self.assertEqual(first_run.init("control", TOKEN, api_base=self.api.url, wait_s=5, poll_s=0,
                                        out=self.lines.append, code=code), 0)
        self.assertEqual(run.load_config(os.path.join("control", "config.json"))["owners"], [4242])
        self.assertIn(code, " ".join(self.lines))                     # the code was shown in the terminal

    def test_messages_from_before_init_are_skipped(self):
        old = msg(5, 666)
        old["message"]["text"] = "123456"
        self.api.replies["getUpdates"] = [(200, {"ok": True, "result": [old]}),
                                          (200, {"ok": True, "result": []})]
        self.assertEqual(first_run.init("control", TOKEN, api_base=self.api.url, wait_s=0.5, poll_s=0,
                                        out=self.lines.append, code="123456"), 2)
        self.assertFalse(os.path.exists(os.path.join("control", "config.json")))

    def test_the_token_never_reaches_the_screen(self):
        self.api.replies["getUpdates"] = [EMPTY, (200, {"ok": True, "result": [msg(1, 4242)]})]
        self.init()
        self.assertNotIn(TOKEN.split(":")[1], " ".join(self.lines))

    def test_nothing_is_overwritten(self):
        os.makedirs("control")
        with open(os.path.join("control", "config.json"), "w") as f:
            f.write('{"mine": true}')
        self.assertEqual(self.init(), 2)
        with open(os.path.join("control", "config.json")) as f:
            self.assertEqual(f.read(), '{"mine": true}')

    def test_no_message_writes_nothing(self):
        self.api.replies["getUpdates"] = [(200, {"ok": True, "result": []})]
        self.assertEqual(first_run.init("control", TOKEN, api_base=self.api.url, wait_s=0.3, poll_s=0,
                                        out=self.lines.append), 2)
        self.assertFalse(os.path.exists(os.path.join("control", "config.json")))

    def test_no_token_says_how(self):
        self.assertEqual(first_run.init("control", "", out=self.lines.append), 2)
        self.assertIn("HERALD_TELEGRAM_TOKEN", " ".join(self.lines))

    def test_an_existing_persona_is_kept(self):
        os.makedirs("control")
        with open(os.path.join("control", "persona.md"), "w") as f:
            f.write("my own persona")
        self.api.replies["getUpdates"] = [EMPTY, (200, {"ok": True, "result": [msg(1, 4242)]})]
        self.assertEqual(self.init(), 0)
        with open(os.path.join("control", "persona.md")) as f:
            self.assertEqual(f.read(), "my own persona")


if __name__ == "__main__":
    unittest.main()
