import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import room_web, run, speak_first  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_room import Voice  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402


class Synth:
    def __init__(self):
        self.calls = []

    def __call__(self, text, out, voice):
        self.calls.append((text, voice))
        with open(out, "wb") as f:
            f.write(b"OggS" + voice.encode())
        return out


class PageVoices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.synth = Synth()
        voices = {"Raven": Voice("Raven", ["**Bold** idea, see `code`."] * 9, cost=0.0),
                  "Mute": Voice("Mute", ["quiet"] * 9, cost=0.0)}
        self.srv = room_web.serve(voices, record_dir=self.tmp.name, port=0, pace_s=0.01,
                                  synth=self.synth, voices={"Raven": "raven-voice"})
        self.base, self.tok = f"http://127.0.0.1:{self.srv.server_port}", self.srv.token

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def get(self, path, token=True):
        url = self.base + path + ("&" if "?" in path else "?") + ("t=" + self.tok if token else "")
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.headers.get("Content-Type"), r.read()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path + "?t=" + self.tok, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()

    def messages(self):
        end = time.time() + 5
        while time.time() < end:
            ev = json.loads(self.get("/api/events?since=0")[1])["events"]
            idx = {e["speaker"]: i for i, e in enumerate(ev) if e["type"] == "message"}
            if {"Raven", "Mute"} <= set(idx):
                return idx
            time.sleep(0.05)
        self.fail("no messages")

    def start(self):
        self.post("/api/start", {"topic": "t", "format": "brainstorm",
                                 "seats": [{"name": "Raven", "role": "ideas"}, {"name": "Mute", "role": "ideas"}]})

    def test_a_reply_is_spoken_in_its_agents_voice_and_cached(self):
        self.start()
        i = self.messages()["Raven"]
        ctype, data = self.get(f"/api/audio?i={i}")
        self.assertTrue(ctype.startswith("audio/ogg"))
        self.assertEqual(data, b"OggSraven-voice")
        self.get(f"/api/audio?i={i}")
        self.assertEqual(len(self.synth.calls), 1)                 # the second listen comes from the cache
        self.assertNotIn("**", self.synth.calls[0][0])             # markdown is not read aloud

    def test_an_agent_without_a_voice_and_bad_indexes_get_404(self):
        self.start()
        idx = self.messages()
        for q in (f"i={idx['Mute']}", "i=0", "i=99999", "i=abc", "i=-1"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get(f"/api/audio?{q}")
            self.assertEqual(e.exception.code, 404, q)

    def test_audio_needs_the_token(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/audio?i=1", token=False)
        self.assertEqual(e.exception.code, 403)

    def test_the_roster_says_who_has_a_voice(self):
        roster = json.loads(self.get("/api/roster")[1])
        self.assertEqual(roster["voices"], ["Raven"])


class TelegramVoice(unittest.TestCase):
    def test_the_room_summary_is_also_spoken_when_voice_is_on(self):
        with tempfile.TemporaryDirectory() as d:
            control, ws = os.path.join(d, "c"), os.path.join(d, "w")
            os.makedirs(control)
            os.makedirs(ws)
            bot, spoken = FakeBot(), []

            def synth(t, out):
                spoken.append(t)
                open(out, "wb").close()
                return out
            gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(control, "s.json"),
                                    os.path.join(control, "STOP"))
            h = run.Herald(bot, lambda *a, **k: TurnResult(True, "ok", "s", 0.0), EngineConfig(), gate, [], control,
                           ws, {OWNER}, synth=synth, transcript_roots=[], room_sync=True,
                           room_agents={"Raven": Voice("Raven", ["i", "y", "v", "the room says go"])})
            h.handle(text("/voice both"))
            h.handle(text("/room t"))
            self.assertTrue(any("the room says go" in s for s in spoken))
            self.assertTrue(bot.voices)


if __name__ == "__main__":
    unittest.main()
