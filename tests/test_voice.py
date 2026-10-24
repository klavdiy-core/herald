import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import voice  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fake_piper.py")
HAS_FFMPEG = shutil.which("ffmpeg") is not None


class CleanTest(unittest.TestCase):
    def test_markdown_is_not_read_aloud(self):
        self.assertEqual(voice.clean_for_speech("**Done.** See _this_ and `x = 1`.\n# Title\n> quote\n- item"),
                         "Done. See this and x = 1. Title quote item")

    def test_urls_become_link(self):
        self.assertEqual(voice.clean_for_speech("read https://example.com/a?b=1 now"), "read link now")

    def test_code_blocks_are_skipped(self):
        self.assertEqual(voice.clean_for_speech("Here:\n```py\nprint(1)\n```\nok"), "Here: (code skipped) ok")

    def test_speakable(self):
        self.assertTrue(voice.speakable("Привет, всё готово."))
        self.assertFalse(voice.speakable("x" * 700))
        self.assertFalse(voice.speakable("look:\n```\ncode\n```"))
        self.assertFalse(voice.speakable("| a | b |\n|---|---|\n| 1 | 2 |"))
        self.assertFalse(voice.speakable("   "))


class SynthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model = os.path.join(self.tmp.name, "voice.onnx")
        open(self.model, "wb").close()
        self.cfg = voice.VoiceConfig(piper_cmd=[sys.executable, FAKE], model=self.model, timeout_s=10)
        self.out = os.path.join(self.tmp.name, "out.ogg")

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_makes_an_ogg_opus_file(self):
        voice.synth("Привет", self.out, self.cfg)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(4), b"OggS")
        codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                "stream=codec_name,channels,sample_rate", "-of", "csv=p=0", self.out],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(codec, "opus,48000,1")

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_text_goes_on_stdin_cleaned_not_argv(self):
        voice.synth("**Готово** https://x.y --dangerous", self.out, self.cfg)
        seen = open(os.path.join(self.tmp.name, [f for f in os.listdir(self.tmp.name) if f.endswith(".seen")][0]),
                    encoding="utf-8").read()
        text, argv = seen.split("\n", 1)
        self.assertEqual(text, "Готово link --dangerous")
        self.assertNotIn("Готово", argv)

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_link_word_follows_the_voice_language(self):
        self.cfg.link_word = "ссылка"
        voice.synth("смотри https://x.y", self.out, self.cfg)
        seen = [f for f in os.listdir(self.tmp.name) if f.endswith(".seen")][0]
        self.assertTrue(open(os.path.join(self.tmp.name, seen), encoding="utf-8").read().startswith("смотри ссылка\n"))

    def test_piper_failure_is_a_voice_error(self):
        self.cfg.model = os.path.join(self.tmp.name, "fail.onnx")
        open(self.cfg.model, "wb").close()
        with self.assertRaises(voice.VoiceError):
            voice.synth("hi", self.out, self.cfg)
        self.assertFalse(os.path.exists(self.out))

    def test_piper_timeout_is_a_voice_error(self):
        self.cfg.model = os.path.join(self.tmp.name, "sleep.onnx")
        open(self.cfg.model, "wb").close()
        self.cfg.timeout_s = 2
        with self.assertRaises(voice.VoiceError) as cm:
            voice.synth("hi", self.out, self.cfg)
        self.assertIn("timeout", str(cm.exception).lower())

    def test_missing_model_is_said_plainly(self):
        self.cfg.model = os.path.join(self.tmp.name, "nope.onnx")
        with self.assertRaises(voice.VoiceError) as cm:
            voice.synth("hi", self.out, self.cfg)
        self.assertIn("model", str(cm.exception))

    def test_unspeakable_text_is_refused(self):
        with self.assertRaises(voice.VoiceError):
            voice.synth("x" * 700, self.out, self.cfg)

    def test_no_temp_wav_left_behind(self):
        try:
            voice.synth("hi", self.out, self.cfg)
        except voice.VoiceError:
            pass
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".wav")], [])


if __name__ == "__main__":
    unittest.main()
