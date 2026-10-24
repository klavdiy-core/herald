import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import hearing, telegram  # noqa: E402
from fake_telegram import FakeTelegram  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fake_stt.py")
TOKEN = "123456789:AAH-fake_token_value_for_tests_00000"


class Transcribe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = os.path.join(self.tmp.name, "in.oga")
        with open(self.audio, "wb") as f:
            f.write(b"OggS")

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, mode, timeout=10):
        return hearing.HearConfig(stt_cmd=[sys.executable, FAKE, mode], timeout_s=timeout)

    def test_transcript_is_returned_stripped(self):
        self.assertEqual(hearing.transcribe(self.audio, self.cfg("ok")), "Привет, это голосовое.")

    def test_audio_path_is_the_last_argument(self):
        with open(self.audio, "wb") as f:
            f.write("echo-me".encode())
        self.assertEqual(hearing.transcribe(self.audio, self.cfg("echo")), "echo-me")

    def test_failures_are_hearing_errors(self):
        for mode in ("fail", "empty"):
            with self.subTest(mode=mode), self.assertRaises(hearing.HearingError):
                hearing.transcribe(self.audio, self.cfg(mode))

    def test_timeout_is_a_hearing_error(self):
        with self.assertRaises(hearing.HearingError) as cm:
            hearing.transcribe(self.audio, self.cfg("sleep", timeout=2))
        self.assertIn("timeout", str(cm.exception).lower())

    def test_missing_program_is_said_plainly(self):
        with self.assertRaises(hearing.HearingError):
            hearing.transcribe(self.audio, hearing.HearConfig(stt_cmd=["no-such-stt-program-xyz"]))


class Download(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegram()
        self.bot = telegram.Bot(TOKEN, owners={1}, api_base=self.api.url, sleep=lambda s: None)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.api.close()
        self.tmp.cleanup()

    def test_downloads_the_file_by_id(self):
        self.api.replies["getFile"] = [(200, {"ok": True, "result": {"file_path": "voice/f1.oga", "file_size": 8}})]
        self.api.files["voice/f1.oga"] = b"OggS-abc"
        out = os.path.join(self.tmp.name, "in.oga")
        self.bot.download("F1", out, max_bytes=1000)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"OggS-abc")

    def test_too_big_is_refused_before_downloading(self):
        self.api.replies["getFile"] = [(200, {"ok": True, "result": {"file_path": "voice/f1.oga", "file_size": 5000}})]
        self.api.files["voice/f1.oga"] = b"x" * 5000
        with self.assertRaises(telegram.TelegramError):
            self.bot.download("F1", os.path.join(self.tmp.name, "in.oga"), max_bytes=1000)
        self.assertEqual(self.api.file_gets, 0)

    def test_download_errors_never_carry_the_token(self):
        self.api.replies["getFile"] = [(200, {"ok": True, "result": {"file_path": "voice/missing.oga", "file_size": 8}})]
        with self.assertRaises(telegram.TelegramError) as cm:
            self.bot.download("F1", os.path.join(self.tmp.name, "in.oga"), max_bytes=1000)
        self.assertNotIn(TOKEN.split(":")[1], str(cm.exception))


if __name__ == "__main__":
    unittest.main()
