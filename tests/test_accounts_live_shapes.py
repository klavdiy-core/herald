"""herald.accounts on the shapes a live run showed (the review reds cover the spec; these cover what real transcripts do).
L1 a workflow subagent transcript (<session>/subagents/workflows/<wf>/agent-x.jsonl) is its session's worker, not nobody's
L2 spend counts for the account whose config dir holds the transcript, whatever the registry says
L3 an unregistered spender is in the divisor but is never proposed
L4 many nameless transcripts give a capped audit (5 paths + one 'ещё N' line), not one line each
L5 a usage row labelled by config_dir (the meter writes dirs, not accounts) maps to the account of that dir
Invented names and paths only."""
import json, os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from herald import accounts as A  # noqa: E402

NOW = 1_800_000_000.0


def iso(e):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e))


def answer(mid, out, cc=0, cr=0):
    return {"type": "assistant", "timestamp": iso(NOW - 600),
            "message": {"id": mid, "content": [{"type": "text", "text": "words"}],
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr, "output_tokens": out}}}


class Live(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        r = self.t.name
        self.ctl = os.path.join(r, "control"); os.makedirs(self.ctl)
        self.cfg = {n: os.path.join(r, "cfg_" + n) for n in ("hot", "cool")}
        for d in self.cfg.values():
            os.makedirs(os.path.join(d, "projects", "proj"))

    def tearDown(self):
        self.t.cleanup()

    def registry(self, workers):
        d = {"accounts": [{"name": "hot", "config_dir": self.cfg["hot"], "plan": "max20x"},
                          {"name": "cool", "config_dir": self.cfg["cool"], "plan": "max5x"}],
             "workers": workers, "threshold_pct": 80, "free_below_pct": 50, "max_age_min": 30}
        json.dump(d, open(os.path.join(self.ctl, "accounts.json"), "w", encoding="utf-8"))

    def usage(self, rows=None):
        rows = rows or [{"at_utc": iso(NOW - 60), "account": "hot", "session_pct": 90, "week_pct": 30,
                         "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)},
                        {"at_utc": iso(NOW - 60), "account": "cool", "session_pct": 10, "week_pct": 10,
                         "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)}]
        open(os.path.join(self.ctl, "usage_log.jsonl"), "w", encoding="utf-8").write("\n".join(json.dumps(x) for x in rows) + "\n")

    def put(self, acc, rel, lines):
        p = os.path.join(self.cfg[acc], "projects", "proj", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write("\n".join(json.dumps(l) for l in lines) + "\n")
        return p

    def workers(self, ps):
        return [p.worker for p in ps]

    def test_L1_workflow_subagent_spends_for_its_session(self):
        self.registry({"Ann": {"account": "hot"}, "Bob": {"account": "hot"}})
        self.usage()
        self.put("hot", "s-ann.jsonl", [{"type": "agent-name", "agentName": "Ann"}, answer("a0", 10)])
        self.put("hot", "s-bob.jsonl", [{"type": "agent-name", "agentName": "Bob"}, answer("b0", 100)])
        self.put("hot", os.path.join("s-ann", "subagents", "workflows", "wf_x", "agent-1.jsonl"), [answer("a1", 1000)])
        ps, audit = A.run_once(self.ctl, NOW)
        self.assertEqual(self.workers(ps), ["Ann"], "L1: the subagent's 1000 belongs to Ann: %r / %r" % (ps, audit))
        self.assertFalse(any("ничей" in a for a in audit), "L1: %r" % audit)

    def test_L2_spend_counts_for_the_account_of_the_folder(self):
        # Cat is registered on cool, but this window she spent on hot (her transcript sits in hot's folder)
        self.registry({"Ann": {"account": "hot"}, "Cat": {"account": "cool"}})
        self.usage()
        self.put("hot", "s-ann.jsonl", [{"type": "agent-name", "agentName": "Ann"}, answer("a0", 100)])
        self.put("hot", "s-cat.jsonl", [{"type": "agent-name", "agentName": "Cat"}, answer("c0", 900)])
        ps, audit = A.run_once(self.ctl, NOW)
        # Cat's 900 count for hot (the folder tells it), so Ann is 100 of 1000 = 10% x a 90% window = 9%.
        # Cat herself is registered on cool: the registry is stale, so she is not proposed (review R4), an audit line says so.
        self.assertEqual([p.worker for p in ps], ["Ann"], "L2: %r" % (ps,))
        self.assertIn("Ann ~9–9%", ps[0].text, "L2: Cat's spend on hot is in the divisor: %r" % ps[0].text)
        self.assertTrue(any("Cat" in a and "реестр" in a for a in audit), "L2: %r" % audit)

    def test_L3_unregistered_spender_is_in_the_divisor_never_proposed(self):
        self.registry({"Ann": {"account": "hot"}})
        self.usage()
        self.put("hot", "s-ann.jsonl", [{"type": "agent-name", "agentName": "Ann"}, answer("a0", 100)])
        self.put("hot", "s-ghost.jsonl", [{"type": "agent-name", "agentName": "Ghost"}, answer("g0", 900)])
        ps, _ = A.run_once(self.ctl, NOW)
        self.assertEqual(self.workers(ps), ["Ann"], "L3: %r" % (ps,))
        self.assertIn("~9–9%", ps[0].text, "L3: Ann is 10%% of all spend (Ghost counted) -> 9%% of a 90%% window: %r" % ps[0].text)

    def test_L4_nameless_audit_is_capped(self):
        self.registry({"Ann": {"account": "hot"}})
        self.usage()
        for i in range(12):
            self.put("hot", "nameless-%02d.jsonl" % i, [answer("n%d" % i, 5)])
        _, audit = A.run_once(self.ctl, NOW)
        lines = [a for a in audit if "ничей" in a]
        self.assertEqual(len(lines), 6, "L4: 5 paths + 1 summary, got %d" % len(lines))
        self.assertTrue(any("ещё 7" in a for a in lines), "L4: %r" % lines)

    def test_L5_usage_row_by_config_dir_maps_to_its_account(self):
        self.registry({"Ann": {"account": "hot"}, "Bob": {"account": "hot"}})
        rows = [{"at_utc": iso(NOW - 60), "config": "x", "config_dir": self.cfg["hot"], "session_pct": 90, "week_pct": 30,
                 "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)},
                {"at_utc": iso(NOW - 60), "config": "y", "config_dir": self.cfg["cool"] + os.sep, "session_pct": 10, "week_pct": 10,
                 "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)}]
        self.usage(rows)
        self.put("hot", "s-ann.jsonl", [{"type": "agent-name", "agentName": "Ann"}, answer("a0", 900)])
        self.put("hot", "s-bob.jsonl", [{"type": "agent-name", "agentName": "Bob"}, answer("b0", 100)])
        ps, audit = A.run_once(self.ctl, NOW)
        self.assertEqual(self.workers(ps), ["Ann"], "L5: %r / %r" % (ps, audit))
        self.assertFalse(any("неизвестн" in a for a in audit), "L5: %r" % audit)

    def test_L6_a_newer_error_row_does_not_hide_a_fresh_measurement(self):
        # cool: a good row 20 min ago (fresh, max_age 30), then the meter failed 1 min ago. The failure is not a
        # measurement: the last good fresh row stands, so cool is still a free target.
        self.registry({"Ann": {"account": "hot"}})
        rows = [{"at_utc": iso(NOW - 60), "account": "hot", "session_pct": 90, "week_pct": 30,
                 "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)},
                {"at_utc": iso(NOW - 1200), "account": "cool", "session_pct": 10, "week_pct": 10,
                 "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)},
                {"at_utc": iso(NOW - 60), "account": "cool", "error": "unreadable"}]
        self.usage(rows)
        self.put("hot", "s-ann.jsonl", [{"type": "agent-name", "agentName": "Ann"}, answer("a0", 900)])
        ps, _ = A.run_once(self.ctl, NOW)
        self.assertEqual(self.workers(ps), ["Ann"], "L6: the error row replaced the fresh measurement: %r" % (ps,))


def link_dir(target, link):
    """a directory link without admin: symlink on POSIX, a junction on Windows; None if neither works here"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return link
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return link
    except (ImportError, OSError):
        return None


class Shared(Live):
    def test_L7_a_projects_folder_shared_by_two_accounts_is_read_once_and_follows_the_registry(self):
        # cool's projects is a link to hot's projects: one folder, two accounts. The folder cannot tell the account,
        # so the spend follows the registry and is never counted on both.
        import shutil
        shutil.rmtree(os.path.join(self.cfg["cool"], "projects"))
        if link_dir(os.path.join(self.cfg["hot"], "projects"), os.path.join(self.cfg["cool"], "projects")) is None:
            self.skipTest("no directory links on this system")
        self.registry({"Ann": {"account": "hot"}, "Bob": {"account": "hot"}, "Cat": {"account": "cool"}})
        self.usage([{"at_utc": iso(NOW - 60), "account": "hot", "session_pct": 90, "week_pct": 30,
                     "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)},
                    {"at_utc": iso(NOW - 60), "account": "cool", "session_pct": 85, "week_pct": 10,
                     "session_resets": iso(NOW + 7200), "week_resets": iso(NOW + 86400)},
                    ])
        self.put("hot", "s-ann.jsonl", [{"type": "agent-name", "agentName": "Ann"}, answer("a0", 100)])
        self.put("hot", "s-bob.jsonl", [{"type": "agent-name", "agentName": "Bob"}, answer("b0", 300)])
        self.put("hot", "s-cat.jsonl", [{"type": "agent-name", "agentName": "Cat"}, answer("c0", 600)])
        rows, audit = A.collect_tokens(A.load_registry(os.path.join(self.ctl, "accounts.json")), NOW)
        self.assertEqual(len(rows), 3, "L7: the shared folder must be read once, got %d rows" % len(rows))
        self.assertTrue(all("account" not in r for r in rows), "L7: a shared folder cannot tag an account: %r" % rows)
        self.assertTrue(any("общ" in a for a in audit), "L7: no audit line about the shared folder: %r" % audit)


if __name__ == "__main__":
    unittest.main()
