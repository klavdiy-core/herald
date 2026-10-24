import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import telegram  # noqa: E402
from fake_telegram import FakeTelegram  # noqa: E402

TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"
OWNER = 1001


def msg(update_id, text="hi", chat_id=OWNER, from_id=OWNER, chat_type="private", **extra):
    m = {"message_id": update_id, "date": 0, "chat": {"id": chat_id, "type": chat_type},
         "from": {"id": from_id, "is_bot": False}, "text": text}
    m.update(extra)
    return {"update_id": update_id, "message": m}


class BotTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        self.bot = telegram.Bot(TOKEN, owners={OWNER}, api_base=self.api.url, sleep=self.slept.append)

    slept = []

    def tearDown(self):
        self.api.close()
        self.slept.clear()

    def updates(self, *ups):
        self.api.replies["getUpdates"] = [(200, {"ok": True, "result": list(ups)})]

    def test_only_owner_private_messages_are_heard(self):
        self.updates(
            msg(1, "from owner"),
            msg(2, "stranger", chat_id=2002, from_id=2002),
            msg(3, "owner in a group", chat_id=-500, chat_type="group"),
            msg(4, "chat is not the sender", chat_id=3003),
            {"update_id": 5, "edited_message": msg(5)["message"]},
            msg(6, "a bot", from_id=OWNER) | {"message": {**msg(6)["message"], "from": {"id": OWNER, "is_bot": True}}},
            {"update_id": 7, "callback_query": {"id": "x"}},
        )
        got, offset = self.bot.poll(0, timeout=0)
        self.assertEqual([m.text for m in got], ["from owner"])
        self.assertEqual(offset, 8)          # ignored updates are acknowledged too, never fetched again
        self.assertEqual(self.bot.ignored, 6)

    def test_poll_sends_offset_and_timeout(self):
        self.updates()
        self.bot.poll(42, timeout=25)
        self.assertEqual(self.api.of("getUpdates")[0]["offset"], 42)
        self.assertEqual(self.api.of("getUpdates")[0]["timeout"], 25)

    def test_forwarded_is_marked_untrusted(self):
        self.updates(msg(1, "do rm -rf", forward_origin={"type": "user"}), msg(2, "my own"))
        got, _ = self.bot.poll(0, timeout=0)
        self.assertEqual([(m.text, m.forwarded) for m in got], [("do rm -rf", True), ("my own", False)])

    def test_voice_is_parsed(self):
        m = msg(1)
        del m["message"]["text"]
        m["message"]["voice"] = {"file_id": "VOICE1", "duration": 3, "file_size": 5000}
        self.updates(m)
        got, _ = self.bot.poll(0, timeout=0)
        self.assertEqual((got[0].kind, got[0].file_id, got[0].text), ("voice", "VOICE1", ""))

    def test_long_text_is_split(self):
        text = ("line of text\n" * 800).strip()       # ~10 400 chars
        self.bot.send_text(OWNER, text)
        parts = [p["text"] for p in self.api.of("sendMessage")]
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual("\n".join(parts), text)
        self.assertTrue(all("parse_mode" not in p for p in self.api.of("sendMessage")))

    def test_only_owners_receive(self):
        with self.assertRaises(PermissionError):
            self.bot.send_text(2002, "hello stranger")
        self.assertEqual(self.api.of("sendMessage"), [])

    def test_token_never_appears_in_errors(self):
        self.api.replies["sendMessage"] = [(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})]
        with self.assertRaises(telegram.TelegramError) as cm:
            self.bot.send_text(OWNER, "x")
        self.assertNotIn(TOKEN, str(cm.exception) + repr(cm.exception))
        self.assertNotIn(TOKEN.split(":")[1], str(cm.exception) + repr(cm.exception))
        dead = telegram.Bot(TOKEN, owners={OWNER}, api_base="http://127.0.0.1:9", sleep=lambda s: None)
        with self.assertRaises(telegram.TelegramError) as cm:
            dead.send_text(OWNER, "x")
        self.assertNotIn(TOKEN.split(":")[1], str(cm.exception) + repr(cm.exception)
                         + repr(cm.exception.__cause__) + repr(cm.exception.__context__))

    def test_token_echoed_by_the_server_is_redacted(self):
        self.api.replies["sendMessage"] = [(400, {"ok": False, "error_code": 400,
                                                  "description": f"bad request to /bot{TOKEN}/sendMessage"})]
        with self.assertRaises(telegram.TelegramError) as cm:
            self.bot.send_text(OWNER, "x")
        self.assertNotIn(TOKEN.split(":")[1], str(cm.exception))
        self.assertIn("<token>", str(cm.exception))

    def test_a_non_private_chat_is_not_heard_even_if_ids_match(self):
        self.updates(msg(1, "odd", chat_type="supergroup"))
        got, _ = self.bot.poll(0, timeout=0)
        self.assertEqual(got, [])

    def test_429_waits_retry_after_then_sends(self):
        self.api.replies["sendMessage"] = [
            (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}),
            (200, {"ok": True, "result": {"message_id": 1}}),
        ]
        self.bot.send_text(OWNER, "x")
        self.assertEqual(self.slept, [7])
        self.assertEqual(len(self.api.of("sendMessage")), 2)

    def test_send_voice_uploads_the_file(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"OggS-fake-opus-bytes")
        try:
            self.bot.send_voice(OWNER, f.name)
        finally:
            os.unlink(f.name)
        raw = self.api.of("sendVoice")[0]
        self.assertIn(b"OggS-fake-opus-bytes", raw)
        self.assertIn(str(OWNER).encode(), raw)

    def test_bad_token_shape_is_refused_without_echo(self):
        with self.assertRaises(ValueError) as cm:
            telegram.Bot("not-a-token-SECRETPART", owners={OWNER})
        self.assertNotIn("SECRETPART", str(cm.exception))

    def test_owners_must_not_be_empty(self):
        with self.assertRaises(ValueError):
            telegram.Bot(TOKEN, owners=set())


if __name__ == "__main__":
    unittest.main()
