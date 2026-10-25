"""Team skills picked up by each role at start (main Klavdiy's task, 25.10). Red on 0.11.0."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import agents, run, speak_first, team_skills  # noqa: E402
from herald.engine import EngineConfig, TurnResult  # noqa: E402
from test_run import OWNER, FakeBot, text  # noqa: E402

FAKE_CLAUDE = os.path.join(os.path.dirname(__file__), "fake_claude.py")


def repo(d):
    r = os.path.join(d, "team-skills")

    def skill(name, desc, body="Do the thing."):
        os.makedirs(os.path.join(r, "skills", name))
        fm = f"---\nname: {name}\ndescription: {desc}\n---\n" if desc else f"---\nname: {name}\n---\n"
        with open(os.path.join(r, "skills", name, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(fm + "\n" + body)
    skill("edit-fresh-read", "Edit refused: read the file again, never go around it.")
    skill("witness-numbers", "Times and counts only from the clock and the run.")
    skill("video-every-scene", "Check every scene of a video.")
    skill("broken-one", "")                                     # no description: skipped, with a warning
    with open(os.path.join(r, "README.md"), "w", encoding="utf-8") as f:     # a file a bad role name could reach
        f.write("# team skills\n\n- `video-every-scene` · listed outside roles/\n")
    os.makedirs(os.path.join(r, "roles"))
    with open(os.path.join(r, "roles", "Voron.md"), "w", encoding="utf-8") as f:
        f.write("# Voron\n\n- `witness-numbers` · why · who\n- `edit-fresh-read` · why · who\n- `broken-one` · x\n"
                "- `no-such-skill` · listed but missing\n")
    return r


class Pickup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_role_gets_its_two_skills_and_not_the_others(self):
        skills, warnings = team_skills.for_role(self.repo, "Voron")
        self.assertEqual([s["name"] for s in skills], ["witness-numbers", "edit-fresh-read"])
        toc = team_skills.toc(skills)
        self.assertIn("edit-fresh-read", toc)
        self.assertIn("read the file again", toc)
        self.assertNotIn("video-every-scene", toc)

    def test_a_broken_or_missing_skill_is_skipped_with_a_warning(self):
        _, warnings = team_skills.for_role(self.repo, "Voron")
        self.assertTrue(any("broken-one" in w for w in warnings))
        self.assertTrue(any("no-such-skill" in w for w in warnings))

    def test_a_role_without_a_file_has_no_skills_and_does_not_fail(self):
        self.assertEqual(team_skills.for_role(self.repo, "Nobody")[0], [])
        self.assertEqual(team_skills.for_role(os.path.join(self.tmp.name, "no-repo"), "Voron")[0], [])

    def test_a_role_name_cannot_walk_out_of_roles(self):
        self.assertEqual(team_skills.for_role(self.repo, "../README")[0], [])

    def test_a_skill_taken_away_from_the_role_is_removed_from_its_folder(self):
        ws = os.path.join(self.tmp.name, "ws")
        team_skills.setup(self.repo, "Voron", ws)
        with open(os.path.join(self.repo, "roles", "Voron.md"), "w", encoding="utf-8") as f:
            f.write("- `witness-numbers` · only this one now\n")
        team_skills.setup(self.repo, "Voron", ws)
        self.assertEqual(sorted(os.listdir(os.path.join(ws, "team-skills"))), ["witness-numbers"])

    def test_pull_failure_keeps_working_from_the_local_copy(self):
        ok, why = team_skills.pull(self.repo)                    # not a git repository here
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_a_claude_role_in_the_room_sees_its_skills_and_can_read_them(self):
        work = os.path.join(self.tmp.name, "work")
        a = agents.from_config({"name": "Ворон", "kind": "claude", "character": "Sharp.", "skills_role": "Voron"},
                               work, claude_cmd=["python", FAKE_CLAUDE, os.path.join(self.tmp.name, "rec.json")],
                               skills_repo=self.repo)
        with open(a.cfg.append_system_prompt_file, encoding="utf-8") as f:
            system = f.read()
        self.assertIn("Sharp.", system)
        self.assertIn("witness-numbers", system)
        self.assertNotIn("video-every-scene", system)
        # the body is inside the role's own folder, where its Read can reach it
        self.assertTrue(os.path.exists(os.path.join(a.ws, "team-skills", "edit-fresh-read", "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(a.ws, "team-skills", "video-every-scene")))


class Config(unittest.TestCase):
    def test_the_config_names_the_repo_and_the_roles(self):
        with tempfile.TemporaryDirectory() as d:
            r = repo(d)
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"owners": [1], "workspace": os.path.join(d, "w"), "control": os.path.join(d, "c"),
                           "team_skills": {"repo": r, "role": "Voron"},
                           "agents": [{"name": "Ворон", "kind": "claude", "skills_role": "Voron"}]}, f)
            cfg = run.load_config(p)
            a = run.room_agents(cfg)["Ворон"]
            with open(a.cfg.append_system_prompt_file, encoding="utf-8") as f:
                self.assertIn("edit-fresh-read", f.read())
            h, _ = run.build(cfg, "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            self.assertIn("witness-numbers", h.skills_toc)

    def test_room_agents_do_not_live_inside_control(self):
        # an outside review (25.10): their cwd was control/agents/<name>/ws, next to learned/, herald.json and
        # decisions.json - held only by --restricted
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            control = os.path.join(d, "control")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"owners": [1], "workspace": os.path.join(d, "w"), "control": control,
                           "agents": [{"name": "Ворон", "kind": "claude"}]}, f)
            a = run.room_agents(run.load_config(p))["Ворон"]
            ws = os.path.abspath(a.ws)
            self.assertFalse(ws.startswith(os.path.abspath(control) + os.sep), ws)
            self.assertFalse(os.path.abspath(a.cfg.append_system_prompt_file).startswith(os.path.abspath(control)))

    def test_unknown_keys_in_team_skills_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"owners": [1], "workspace": os.path.join(d, "w"), "control": os.path.join(d, "c"),
                           "team_skills": {"repo": d, "rolee": "x"}}, f)
            with self.assertRaises(ValueError):
                run.load_config(p)


class MainHerald(unittest.TestCase):
    def test_the_herald_turn_gets_its_roles_skills_too(self):
        with tempfile.TemporaryDirectory() as d:
            r = repo(d)
            control, ws = os.path.join(d, "c"), os.path.join(d, "w")
            os.makedirs(control)
            os.makedirs(ws)
            persona = os.path.join(d, "p.md")
            with open(persona, "w", encoding="utf-8") as f:
                f.write("You are a herald.")
            systems = []

            def engine(prompt, workspace, cfg, session_id=None):
                with open(cfg.append_system_prompt_file, encoding="utf-8") as f:
                    systems.append(f.read())
                return TurnResult(True, "ok", "s", 0.0)
            gate = speak_first.Gate(speak_first.Limits(enabled=False), os.path.join(control, "s.json"),
                                    os.path.join(control, "STOP"))
            h = run.Herald(FakeBot(), engine, EngineConfig(append_system_prompt_file=persona), gate, [], control, ws,
                           {OWNER}, transcript_roots=[], skills_repo=r, skills_role="Voron")
            h.handle(text("hi"))
            self.assertIn("edit-fresh-read", systems[-1])
            self.assertTrue(os.path.exists(os.path.join(ws, "team-skills", "witness-numbers", "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
