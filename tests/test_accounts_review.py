"""herald.accounts: one red per finding of the adversarial review wf_456150f9-afd (20 stood, 2 refuted). Invented names and paths.
R1 two hot sources never get the same target in one pass
R2 a source with session None: the interval is of the WEEK and the text says so, never 'окно None%'
R3 a source whose window already reset (session_resets <= now) gives no proposal, an audit line instead
R4 a worker registered on another account is not proposed away from this one; the stale registry is an audit line
R5 negative, NaN and Infinity token counts are not numbers
R6 the pair key is the parsed reset instant: two spellings of one instant are one window; no readable reset = no proposal
R7 a file link inside projects is never opened (control: a real file next to it is)
R8 a secret-shaped or email KEY in the registry is refused without echoing it
R9 an unknown account that looks like a token is not echoed; a non-string account is a plain refusal
R10 audit lines name a transcript by its file name only, never the project folder
R11 an unknown account in the usage log that looks like a token is not echoed into the audit
R12 resets with a non-zero UTC offset are read (19:20+03:00 = 16:20Z)
R13 the usage log and the registry survive a BOM and a stray non-UTF8 byte
R14 a state file that is not an object is reset with an audit line, not a crash
R15 state trimming drops the OLDEST keys, never the one just added
R16 the interval rounds half up (22.5 -> 23)
R17 an exact tie goes to the first name case-insensitively ('alpha' before 'Zed')"""
import json, math, os, sys, tempfile, time, unittest
from unittest import mock
import builtins

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from herald import accounts as A  # noqa: E402

NOW = 1_800_000_000.0


def iso(e):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e))


def urow(acc, s, w, age_min=1, resets=NOW + 7200, **kw):
    r = {"at_utc": iso(NOW - age_min * 60), "account": acc, "session_pct": s, "week_pct": w,
         "session_resets": iso(resets) if isinstance(resets, float) else resets, "week_resets": iso(NOW + 86400)}
    r.update(kw)
    return r


def tok(worker, out, id=None, cc=0, cr=0, inp=0, account=None, minutes_ago=30):
    d = {"worker": worker, "id": id or "%s_%d_%d" % (worker, out, cc), "ts": iso(NOW - minutes_ago * 60),
         "input_tokens": inp, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr, "output_tokens": out}
    if account:
        d["account"] = account
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.ctl = os.path.join(self.t.name, "control"); os.makedirs(self.ctl)
        self.cfg = {n: os.path.join(self.t.name, "cfg_" + n) for n in ("a", "b", "c")}
        for d in self.cfg.values():
            os.makedirs(os.path.join(d, "projects", "proj"))

    def tearDown(self):
        self.t.cleanup()

    def regd(self, workers, accounts=("a", "b", "c")):
        return {"accounts": [{"name": n, "config_dir": self.cfg[n], "plan": "max20x"} for n in accounts],
                "workers": workers, "threshold_pct": 80, "free_below_pct": 50, "max_age_min": 30}

    def reg(self, workers, **kw):
        p = os.path.join(self.ctl, "accounts.json")
        json.dump(self.regd(workers, **kw), open(p, "w", encoding="utf-8"))
        return A.load_registry(p)

    def refuse(self, d, *forbidden):
        p = os.path.join(self.ctl, "accounts.json")
        json.dump(d, open(p, "w", encoding="utf-8"))
        with self.assertRaises(ValueError) as cm:
            A.load_registry(p)
        for f in forbidden:
            self.assertNotIn(f, str(cm.exception))
        return str(cm.exception)


class Logic(Base):
    def test_R1_two_hot_sources_one_free_target(self):
        r = self.reg({"Ann": {"account": "a"}, "Bob": {"account": "b"}})
        ps = A.propose(r, [urow("a", 90, 30), urow("b", 92, 30), urow("c", 10, 10)], [tok("Ann", 10), tok("Bob", 10)], NOW)
        self.assertEqual(len([p for p in ps if p.target == "c"]), 1, "R1: %r" % ([(p.worker, p.source, p.target) for p in ps],))

    def test_R2_session_none_on_the_source_speaks_of_the_week(self):
        r = self.reg({"Ann": {"account": "a"}, "Bob": {"account": "a"}})
        ps = A.propose(r, [urow("a", None, 90), urow("b", 10, 10)], [tok("Ann", 300), tok("Bob", 100)], NOW)
        self.assertEqual(len(ps), 1)
        self.assertNotIn("None", ps[0].text, "R2: %r" % ps[0].text)
        self.assertIn("недели", ps[0].text, "R2: the share is of the week: %r" % ps[0].text)

    def test_R3_window_already_reset_no_proposal(self):
        r = self.reg({"Ann": {"account": "a"}})
        audit = []
        ps = A.propose(r, [urow("a", 90, 30, resets=NOW - 600), urow("b", 10, 10)], [tok("Ann", 300)], NOW, audit)
        self.assertEqual(ps, [], "R3: %r" % (ps,))
        self.assertTrue(any("сброш" in a for a in audit), "R3: %r" % audit)

    def test_R4_registered_elsewhere_is_not_moved_away(self):
        r = self.reg({"Ann": {"account": "a"}, "Cat": {"account": "b"}})
        audit = []
        ps = A.propose(r, [urow("a", 90, 30), urow("c", 10, 10)],
                       [tok("Ann", 100, account="a"), tok("Cat", 900, account="a")], NOW, audit)
        self.assertEqual([p.worker for p in ps], ["Ann"], "R4: %r" % ([p.worker for p in ps],))
        self.assertTrue(any("Cat" in a and "реестр" in a for a in audit), "R4: %r" % audit)

    def test_R5_negative_nan_inf_are_not_numbers(self):
        r = self.reg({"Ann": {"account": "a"}, "Bob": {"account": "a"}})
        for bad in (-5000, float("nan"), float("inf")):
            ps = A.propose(r, [urow("a", 90, 30), urow("b", 10, 10)], [tok("Ann", 100), tok("Bob", 100, id="b2", cc=0, cr=0, inp=0) | {"output_tokens": bad}], NOW)
            self.assertEqual(len(ps), 1, "R5 %r: %r" % (bad, ps))
            self.assertRegex(ps[0].text, r"~\d+–\d+%", "R5 %r: %r" % (bad, ps[0].text))
            lo, hi = map(int, __import__("re").search(r"~(\d+)–(\d+)%", ps[0].text).groups())
            self.assertTrue(0 <= lo <= hi <= 100, "R5 %r: %r" % (bad, ps[0].text))

    def test_R6_pair_key_is_the_parsed_instant(self):
        r = self.reg({"Ann": {"account": "a"}})
        p1 = A.propose(r, [urow("a", 90, 30, resets="2027-01-15T10:00:00Z"), urow("b", 10, 10)], [tok("Ann", 1)], NOW)
        p2 = A.propose(r, [urow("a", 90, 30, resets="2027-01-15T10:00:00.4+00:00"), urow("b", 10, 10)], [tok("Ann", 1)], NOW)
        self.assertEqual(p1[0].pair_key, p2[0].pair_key, "R6: one instant, two spellings")
        audit = []
        p3 = A.propose(r, [urow("a", 90, 30, resets=None), urow("b", 10, 10)], [tok("Ann", 1)], NOW, audit)
        self.assertEqual(p3, [], "R6: no readable reset = no window = no proposal: %r" % (p3,))

    def test_R12_offset_is_read(self):
        self.assertEqual(A._epoch("2026-11-07T19:20:00+03:00"), A._epoch("2026-11-07T16:20:00Z"))
        self.assertIsNotNone(A._epoch("2026-11-07T16:20:00.123456+00:00"))

    def test_R16_half_up(self):
        r = self.reg({"Ann": {"account": "a"}, "Bob": {"account": "a"}, "Cy": {"account": "a"}, "Di": {"account": "a"}})
        rows = [tok(n, 100) for n in ("Ann", "Bob", "Cy", "Di")]
        ps = A.propose(r, [urow("a", 90, 30), urow("b", 10, 10)], rows, NOW)
        self.assertIn("~23–23%", ps[0].text, "R16: 90 x .25 = 22.5 -> 23: %r" % ps[0].text)

    def test_R17_tie_case_insensitive(self):
        r = self.reg({"Zed": {"account": "a"}, "alpha": {"account": "a"}})
        ps = A.propose(r, [urow("a", 90, 30), urow("b", 10, 10)], [tok("Zed", 100), tok("alpha", 100)], NOW)
        self.assertEqual(ps[0].worker, "alpha", "R17")


class Privacy(Base):
    def test_R7_a_file_link_inside_projects_is_never_opened(self):
        secret = os.path.join(self.t.name, "outside", ".credentials.json")
        os.makedirs(os.path.dirname(secret))
        open(secret, "w").write('{"type":"agent-name","agentName":"Leak"}\n')
        link = os.path.join(self.cfg["a"], "projects", "proj", "x.jsonl")
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("no file symlinks here")
        real = os.path.join(self.cfg["a"], "projects", "proj", "real.jsonl")
        open(real, "w").write('{"type":"agent-name","agentName":"Ann"}\n')
        reg = self.reg({"Ann": {"account": "a"}}, accounts=("a",))
        seen = []
        orig = builtins.open

        def spy(f, *a, **k):
            seen.append(os.path.realpath(str(f)))
            return orig(f, *a, **k)
        with mock.patch.object(builtins, "open", spy):
            A.collect_tokens(reg, NOW)
        self.assertIn(os.path.realpath(real), seen, "R7 control: the real transcript was not read")
        self.assertNotIn(os.path.realpath(secret), seen, "R7: a link led the watcher to a file outside projects")

    def test_R7b_a_directory_link_inside_projects_is_never_walked(self):
        outside = os.path.join(self.t.name, "private_notes")
        os.makedirs(outside)
        leak = os.path.join(outside, "notes.jsonl")
        open(leak, "w").write('{"type":"agent-name","agentName":"Leak"}\n')
        link = os.path.join(self.cfg["a"], "projects", "proj", "linked")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            try:
                import _winapi
                _winapi.CreateJunction(outside, link)
            except (ImportError, OSError):
                self.skipTest("no directory links here")
        real = os.path.join(self.cfg["a"], "projects", "proj", "real.jsonl")
        open(real, "w").write('{"type":"agent-name","agentName":"Ann"}\n')
        reg = self.reg({"Ann": {"account": "a"}}, accounts=("a",))
        seen = []
        orig = builtins.open

        def spy(f, *a, **k):
            seen.append(os.path.realpath(str(f)))
            return orig(f, *a, **k)
        with mock.patch.object(builtins, "open", spy):
            A.collect_tokens(reg, NOW)
        self.assertIn(os.path.realpath(real), seen, "R7b control: the real transcript was not read")
        self.assertNotIn(os.path.realpath(leak), seen, "R7b: a directory link led the watcher outside projects")

    def test_R8_secret_or_email_key_refused_not_echoed(self):
        for key in ("sk-ant-oat01-AAAAAAAAAAAAAAAAAAAA", "bob@example.com"):
            d = self.regd({key: {"account": "a"}})
            self.refuse(d, key)

    def test_R9_token_like_unknown_account_not_echoed(self):
        v = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        self.refuse(self.regd({"Ann": {"account": v}}), v)
        self.refuse(self.regd({"Ann": {"account": ["a"]}}))

    def test_R10_audit_names_file_only(self):
        p = os.path.join(self.cfg["a"], "projects", "C--secret-client-project", "nameless.jsonl")
        os.makedirs(os.path.dirname(p))
        open(p, "w").write(json.dumps({"type": "assistant", "timestamp": iso(NOW - 60),
                                       "message": {"id": "m", "usage": {"output_tokens": 5}}}) + "\n")
        reg = self.reg({"Ann": {"account": "a"}}, accounts=("a",))
        _, audit = A.collect_tokens(reg, NOW)
        self.assertTrue(any("nameless" in a for a in audit), "R10 control: %r" % audit)
        self.assertFalse(any("secret-client" in a for a in audit), "R10: %r" % audit)

    def test_R11_token_like_unknown_account_in_log_not_echoed(self):
        v = "sk-ant-oat01-BBBBBBBBBBBBBBBBBBBB"
        r = self.reg({"Ann": {"account": "a"}})
        audit = []
        A.propose(r, [urow(v, 90, 30)], [], NOW, audit)
        self.assertTrue(audit, "R11 control: no audit line at all")
        self.assertFalse(any(v in a for a in audit), "R11: %r" % audit)


class Files(Base):
    def test_R13_bom_and_stray_byte(self):
        json.dump(self.regd({"Ann": {"account": "a"}, "Bob": {"account": "a"}}), open(os.path.join(self.ctl, "accounts.json"), "w", encoding="utf-8-sig"))
        good = json.dumps(urow("a", 90, 30)).encode() + b"\n" + b"\xe0\xff garbage\n" + json.dumps(urow("b", 10, 10)).encode() + b"\n"
        open(os.path.join(self.ctl, "usage_log.jsonl"), "wb").write(b"\xef\xbb\xbf" + good)
        for n, out in (("Ann", 300), ("Bob", 100)):
            p = os.path.join(self.cfg["a"], "projects", "proj", n + ".jsonl")
            open(p, "w").write("\n".join(json.dumps(x) for x in ({"type": "agent-name", "agentName": n},
                               {"type": "assistant", "timestamp": iso(NOW - 600), "message": {"id": n, "usage": {"output_tokens": out}}})) + "\n")
        ps, audit = A.run_once(self.ctl, NOW)
        self.assertEqual([p.worker for p in ps], ["Ann"], "R13: %r / %r" % (ps, audit))

    def test_R14_state_not_an_object(self):
        A_ = A
        open(os.path.join(self.ctl, A_.STATE_FILE), "w").write("[1, 2]")
        audit = []
        out = A_._remember(self.ctl, [A_.Proposal("Ann", "a", "b", "t", "a>b@1")], audit)
        self.assertEqual(len(out), 1)
        self.assertTrue(any("состояние" in a for a in audit), "R14: %r" % audit)

    def test_R15_trim_drops_oldest(self):
        st = {"proposed": {"a>b@%d" % i: 1000.0 + i for i in range(600)}}
        open(os.path.join(self.ctl, A.STATE_FILE), "w").write(json.dumps(st))
        new = A.Proposal("Ann", "AAA", "b", "t", "AAA>b@9")         # sorts first alphabetically, newest in time
        A._remember(self.ctl, [new], [])
        kept = json.load(open(os.path.join(self.ctl, A.STATE_FILE)))["proposed"]
        self.assertIn("AAA>b@9", kept, "R15: the key just added was trimmed")
        self.assertNotIn("a>b@0", kept, "R15: the oldest key survived")
        self.assertLessEqual(len(kept), 500)


if __name__ == "__main__":
    unittest.main()
