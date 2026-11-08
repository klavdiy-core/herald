"""accounts_build: the registry rebuilt from launchers. Fake launchers and fake logins in a temp folder;
the emails are example.com and must never reach the written registry or an audit line."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from herald import accounts as A  # noqa: E402
from herald import accounts_build as B  # noqa: E402

LAUNCHER = """# opens {name} if not alive (sentinel --name {name})
$settings = 'x'
$cmd = @"
`$env:CLAUDE_CONFIG_DIR = '{cfg}'
claude --model claude-opus-5-5 --name {name} --append-system-prompt 'role' --settings '$settings'
"@
"""
LAUNCHER_VAR = """$home_ = '{home}'
$cmd = @"
`$env:CLAUDE_CONFIG_DIR = '$home_\\config'
claude --model claude-haiku-5-5 --name {name} --remote-control 'x'
"@
"""
HELPER = "# a helper, no claude line\nWrite-Host 'hi'\n"


class Build(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="accb_")
        self.ctl = os.path.join(self.t, "control"); os.makedirs(self.ctl)
        self.lau = os.path.join(self.t, "launchers"); os.makedirs(self.lau)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def cfg(self, name, email):
        d = os.path.join(self.t, name); os.makedirs(d, exist_ok=True)
        if email is not None:
            with open(os.path.join(d, ".claude.json"), "w", encoding="utf-8") as f:
                json.dump({"oauthAccount": {"emailAddress": email, "accountUuid": "u"}, "other": 1}, f)
        return d

    def launcher(self, fname, text):
        with open(os.path.join(self.lau, fname), "w", encoding="utf-8-sig") as f:
            f.write(text)

    def sources(self):
        with open(os.path.join(self.ctl, B.SOURCES_FILE), "w", encoding="utf-8") as f:
            json.dump({"launchers": [os.path.join(self.lau, "*.ps1")]}, f)

    def registry(self):
        with open(os.path.join(self.ctl, B.REGISTRY_FILE), encoding="utf-8") as f:
            return f.read()

    def test_groups_by_login_and_never_writes_the_email(self):
        c1, c2, c3 = self.cfg("claude_team_config", "one@example.com"), self.cfg("claude_team2_config", "two@example.com"), \
            self.cfg("claude_extra_config", "ONE@example.com ")
        home = os.path.join(self.t, "second"); self.cfg(os.path.join("second", "config"), "two@example.com")
        self.launcher("a.ps1", LAUNCHER.format(name="Alpha", cfg=c1))
        self.launcher("b.ps1", LAUNCHER.format(name="Beta", cfg=c2))
        self.launcher("c.ps1", LAUNCHER.format(name="Gamma", cfg=c3))
        self.launcher("k.ps1", LAUNCHER_VAR.format(home=home, name="Kappa"))
        self.launcher("h.ps1", HELPER)
        self.sources()
        audit = B.rebuild(self.ctl)
        raw = self.registry()
        self.assertNotIn("@", raw, "an email reached the registry")
        self.assertFalse(any("@" in a or "example" in a for a in audit), audit)
        reg = A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))       # accepted by the real reader
        acc = {w: v["account"] for w, v in reg["workers"].items()}
        self.assertEqual(set(acc), {"Alpha", "Beta", "Gamma", "Kappa"})
        self.assertEqual(acc["Alpha"], acc["Gamma"], "one login (case/space differ) must be one account")
        self.assertEqual(acc["Beta"], acc["Kappa"], "a $var-built folder on the same login must join it")
        self.assertNotEqual(acc["Alpha"], acc["Beta"])
        self.assertEqual(len(reg["accounts"]), 2)
        self.assertTrue(any("2 папок конфига" in a for a in audit), audit)

    def test_changes_against_the_old_registry_are_audited_and_settings_kept(self):
        c1, c2 = self.cfg("claude_team_config", "one@example.com"), self.cfg("claude_team2_config", "two@example.com")
        with open(os.path.join(self.ctl, B.REGISTRY_FILE), "w", encoding="utf-8") as f:
            json.dump({"accounts": [{"name": "team", "config_dir": c1, "plan": "max20"}, {"name": "team2", "config_dir": c2, "plan": "max5"}],
                       "workers": {"Alpha": {"account": "team", "pinned": True}, "Gone": {"account": "team"}}, "threshold_pct": 70}, f)
        self.launcher("a.ps1", LAUNCHER.format(name="Alpha", cfg=c2))          # Alpha moved to team2
        self.sources()
        audit = B.rebuild(self.ctl)
        reg = A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))
        self.assertEqual(reg["workers"]["Alpha"], {"account": "team2", "pinned": True})
        self.assertEqual(reg["threshold_pct"], 70)
        self.assertEqual(reg["accounts"]["team2"]["plan"], "max5")
        self.assertTrue(any("Alpha" in a and "team -> team2" in a for a in audit), audit)
        self.assertTrue(any("Gone" in a and "убран" in a for a in audit), audit)

    def test_bad_launchers_are_audit_lines_not_guesses(self):
        c1 = self.cfg("claude_team_config", "one@example.com")
        self.launcher("two_names.ps1", LAUNCHER.format(name="Alpha", cfg=c1) + "claude --name Other\n")
        self.launcher("unknown_var.ps1", "`$env:CLAUDE_CONFIG_DIR = '$nowhere\\config'\nclaude --name Delta\n")
        self.launcher("ok.ps1", LAUNCHER.format(name="Beta", cfg=c1))
        self.sources()
        audit = B.rebuild(self.ctl)
        reg = A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))
        self.assertEqual(set(reg["workers"]), {"Beta"})
        self.assertTrue(any("two_names.ps1" in a and "--name" in a for a in audit), audit)
        self.assertTrue(any("unknown_var.ps1" in a and "переменн" in a for a in audit), audit)

    def test_no_login_is_its_own_account_and_said(self):
        c1 = self.cfg("claude_team_config", None)
        self.launcher("a.ps1", LAUNCHER.format(name="Alpha", cfg=c1))
        self.sources()
        audit = B.rebuild(self.ctl)
        self.assertTrue(any("вход не найден" in a for a in audit), audit)
        self.assertEqual(A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))["workers"]["Alpha"]["account"], "team")

    def test_without_a_sources_file_nothing_is_touched(self):
        with open(os.path.join(self.ctl, B.REGISTRY_FILE), "w", encoding="utf-8") as f:
            f.write("HAND")
        self.assertEqual(B.rebuild(self.ctl), [])
        self.assertEqual(self.registry(), "HAND")

    def test_a_refused_build_keeps_the_old_registry(self):
        old = {"accounts": [{"name": "team", "config_dir": "x"}], "workers": {}}
        with open(os.path.join(self.ctl, B.REGISTRY_FILE), "w", encoding="utf-8") as f:
            json.dump(old, f)
        self.sources()                                         # no launchers at all -> empty accounts -> refused by load_registry
        audit = B.rebuild(self.ctl)
        self.assertTrue(any("отклонён" in a for a in audit), audit)
        self.assertEqual(json.loads(self.registry()), old)
        self.assertFalse([p for p in os.listdir(self.ctl) if p.endswith(".tmp")])

    def test_credentials_file_is_never_opened(self):
        c1 = self.cfg("claude_team_config", "one@example.com")
        with open(os.path.join(c1, ".credentials.json"), "w") as f:
            f.write("{}")
        opened = []
        real = B.A._read_json_file
        B.A._read_json_file = lambda p: (opened.append(os.path.basename(p)), real(p))[1]
        try:
            self.assertIsNotNone(B.account_key(c1))
        finally:
            B.A._read_json_file = real
        self.assertEqual(opened, [".claude.json"])

    def test_both_folders_of_one_login_are_counted(self):
        c1, c2 = self.cfg("claude_team2_config", "two@example.com"), self.cfg("k_config", "two@example.com")
        now = time.time()
        for cd, w in ((c1, "Alpha"), (c2, "Kappa")):
            pd = os.path.join(cd, "projects", "p"); os.makedirs(pd)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 60))
            with open(os.path.join(pd, "s_%s.jsonl" % w), "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "agent-name", "agentName": w}) + "\n")
                f.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": "m_" + w, "usage": {
                    "input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}}}) + "\n")
            self.launcher(w + ".ps1", LAUNCHER.format(name=w, cfg=cd))
        self.sources()
        B.rebuild(self.ctl)
        reg = A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))
        rows, audit = A.collect_tokens(reg, now)
        self.assertEqual(sorted({r["worker"] for r in rows}), ["Alpha", "Kappa"], (rows, audit))
        self.assertTrue(all(r.get("account") == reg["workers"]["Alpha"]["account"] for r in rows), rows)
        self.assertFalse(any("общая" in a for a in audit), audit)

    def test_a_comment_with_a_claude_line_is_not_a_second_name(self):     # review finding 1
        c1 = self.cfg("claude_team_config", "one@example.com")
        self.launcher("a.ps1", "# old: claude --name Gamma\n<#\nclaude --name Delta\n#>\n" + LAUNCHER.format(name="Alpha", cfg=c1))
        self.sources()
        audit = B.rebuild(self.ctl)
        self.assertEqual(set(A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))["workers"]), {"Alpha"}, audit)

    def test_a_troubled_launcher_keeps_the_old_rows(self):                # review finding 2
        c1, c2 = self.cfg("claude_team_config", "one@example.com"), self.cfg("claude_team2_config", "two@example.com")
        with open(os.path.join(self.ctl, B.REGISTRY_FILE), "w", encoding="utf-8") as f:
            json.dump({"accounts": [{"name": "team", "config_dir": c1}, {"name": "team2", "config_dir": c2, "plan": "max5"}],
                       "workers": {"Alpha": {"account": "team"}, "Beta": {"account": "team2", "pinned": True}}}, f)
        self.launcher("a.ps1", LAUNCHER.format(name="Alpha", cfg=c1))
        self.launcher("b.ps1", LAUNCHER.format(name="Beta", cfg=c2) + "claude --name Other\n")   # now 2 names: a problem
        self.sources()
        audit = B.rebuild(self.ctl)
        reg = A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))
        self.assertEqual(reg["workers"]["Beta"], {"account": "team2", "pinned": True}, audit)
        self.assertEqual(reg["accounts"]["team2"]["plan"], "max5")
        self.assertTrue(any("Beta" in a and "оставлена прежняя" in a for a in audit), audit)
        self.assertFalse(any("Beta" in a and "убран" in a for a in audit), audit)

    def test_a_too_deep_login_file_is_no_login_not_a_crash(self):        # review finding 3
        c1 = self.cfg("claude_team_config", None)
        with open(os.path.join(c1, ".claude.json"), "w") as f:
            f.write("[" * 200000)
        self.assertIsNone(B.account_key(c1))

    def test_run_once_rebuilds_first(self):
        c1 = self.cfg("claude_team_config", "one@example.com")
        self.launcher("a.ps1", LAUNCHER.format(name="Alpha", cfg=c1))
        self.sources()
        props, audit = A.run_once(self.ctl, time.time())
        self.assertEqual(props, [])
        self.assertIn("Alpha", A.load_registry(os.path.join(self.ctl, B.REGISTRY_FILE))["workers"])


if __name__ == "__main__":
    unittest.main()
