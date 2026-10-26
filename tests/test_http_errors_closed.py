"""An HTTP error from Telegram is closed after it is read. urllib's HTTPError holds the response; read and dropped, it is
left open until garbage collection - one per API error (a 400, a 403, a 429) in a bot that runs for weeks. Seen as
ResourceWarnings on a Linux run. The test spies on the HTTPError objects and checks they were closed."""
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import telegram  # noqa: E402
from fake_telegram import FakeTelegram  # noqa: E402

TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"      # the same fake shape as the other tests


class HttpErrorsClosed(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        self.addCleanup(self.api.close)
        self.bot = telegram.Bot(TOKEN, owners={1}, api_base=self.api.url, sleep=lambda s: None)
        self.errors = []
        real_init = urllib.error.HTTPError.__init__
        errors = self.errors

        def spy(err, *a, **k):
            real_init(err, *a, **k)
            errors.append(err)

        urllib.error.HTTPError.__init__ = spy
        self.addCleanup(setattr, urllib.error.HTTPError, "__init__", real_init)

    def assert_all_closed(self):
        self.assertTrue(self.errors, "no HTTPError was raised - the test did not exercise the path")
        for e in self.errors:
            fp = getattr(e, "fp", None)
            self.assertTrue(fp is None or getattr(fp, "closed", True), f"HTTPError {e.code} left open")

    def test_an_api_error_is_closed(self):
        self.api.replies["sendMessage"] = [(400, {"ok": False, "error_code": 400, "description": "Bad Request"})]
        with self.assertRaises(telegram.TelegramError):
            self.bot._call("sendMessage", {"chat_id": 1, "text": "x"})
        self.assert_all_closed()

    def test_a_failed_download_is_closed(self):
        self.api.replies["getFile"] = [(200, {"ok": True, "result": {"file_path": "voice/missing.oga", "file_size": 8}})]
        with tempfile.TemporaryDirectory() as d, self.assertRaises(telegram.TelegramError):
            self.bot.download("F1", os.path.join(d, "in.oga"), max_bytes=1000)
        self.assert_all_closed()


if __name__ == "__main__":
    unittest.main()
