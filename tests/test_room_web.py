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
from herald import room_web  # noqa: E402
from test_room import Voice  # noqa: E402


class Web(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.voices = {"Raven": Voice("Raven", [f"raven {i}" for i in range(20)], cost=0.001),
                       "Magpie": Voice("Magpie", [f"magpie {i}" for i in range(20)], cost=0.001)}
        self.srv = room_web.serve(self.voices, record_dir=self.tmp.name, port=0, pace_s=0.01)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.tok = self.srv.token

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, token=True):
        url = self.base + path + (("&" if "?" in path else "?") + "t=" + self.tok if token else "")
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
        return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw.decode()

    def wait(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            ev = self.call("/api/events?since=0")["events"]
            if pred(ev):
                return ev
            time.sleep(0.05)
        self.fail("timed out: " + json.dumps(self.call("/api/events?since=0"))[:500])

    def test_without_the_token_nothing_is_served(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call("/", token=False)
        self.assertEqual(e.exception.code, 403)

    def test_the_page_and_the_roster(self):
        self.assertIn("<title>", self.call("/"))
        roster = self.call("/api/roster")
        self.assertEqual(sorted(roster["agents"]), ["Magpie", "Raven"])
        self.assertIn("skeptic", roster["roles"])
        self.assertIn("brainstorm", roster["formats"])

    def test_a_room_runs_live_and_the_owner_steers_it(self):
        self.call("/api/start", {"topic": "name the bot", "format": "brainstorm", "budget_usd": 1.0,
                                 "seats": [{"name": "Raven", "role": "ideas"}, {"name": "Magpie", "role": "skeptic"}]})
        self.wait(lambda ev: sum(e["type"] == "message" for e in ev) >= 2)
        self.call("/api/control", {"action": "pause"})
        self.call("/api/control", {"action": "inject", "text": "think about voices"})
        self.call("/api/control", {"action": "next", "name": "Magpie"})
        self.call("/api/control", {"action": "resume"})
        def magpie_after_owner(ev):
            msgs = [e for e in ev if e["type"] == "message"]
            owner = next((i for i, e in enumerate(msgs) if e["speaker"] == "Owner"), None)
            return owner is not None and any(e["speaker"] == "Magpie" for e in msgs[owner + 1:])
        self.wait(magpie_after_owner)
        self.assertIn("think about voices", self.voices["Magpie"].prompts[-1])
        self.call("/api/control", {"action": "end"})
        ev = self.wait(lambda ev: any(e["type"] == "summary" for e in ev))
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)

    def test_events_since_returns_only_new_ones(self):
        self.call("/api/start", {"topic": "t", "format": "decide", "seats": [{"name": "Raven", "role": "ideas"}]})
        self.wait(lambda ev: any(e["type"] == "message" for e in ev))
        first = self.call("/api/events?since=0")
        later = self.call(f"/api/events?since={first['next']}")
        self.assertTrue(all(e not in first["events"] for e in later["events"]))

    def test_unknown_agents_and_bad_formats_are_refused(self):
        for body in ({"topic": "t", "format": "brainstorm", "seats": [{"name": "Nobody", "role": "ideas"}]},
                     {"topic": "t", "format": "chaos", "seats": [{"name": "Raven", "role": "ideas"}]},
                     {"topic": "", "format": "brainstorm", "seats": [{"name": "Raven", "role": "ideas"}]}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.call("/api/start", body)
            self.assertEqual(e.exception.code, 400)
            reason = json.loads(e.exception.read())["error"]
            if body["seats"][0]["name"] == "Nobody":
                self.assertIn("unknown agent", reason)          # a reason the owner can act on


if __name__ == "__main__":
    unittest.main()

