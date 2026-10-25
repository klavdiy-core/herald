"""Participants of a room. Two kinds, both with .name and .speak(prompt) -> (text, cost_usd):

- "claude": a role (its own character and model) that answers through herald's confined Claude Code turn, one fresh
  turn per reply, in its own empty folder, with Read as the only tool.
- "command": any program that reads the prompt on stdin and prints the answer (another model's CLI, a script).

A participant that fails says so in its reply; it never takes the room down.
"""
import os
import re
import shutil
import subprocess
import sys

from . import engine, team_skills

NAME = re.compile(r"^[A-Za-zЀ-ӿ][\w \-Ѐ-ӿ]{0,30}$")


def _append(home, line):
    with open(os.path.join(home, "skills.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


class ClaudeRole:
    def __init__(self, name, character, model, workdir, claude_cmd=None, max_budget_usd=0.2, skills_repo=None,
                 skills_role=None):
        self.name = name
        home = os.path.join(workdir, "agents", re.sub(r"\W+", "_", name))
        self.ws = os.path.join(home, "ws")
        os.makedirs(self.ws, exist_ok=True)
        skills = team_skills.setup(skills_repo, skills_role, self.ws, log=lambda line: _append(home, line))
        char = os.path.join(home, "character.md")
        with open(char, "w", encoding="utf-8") as f:
            f.write((f"You are {name}. {character}".strip() + ("\n\n" + skills if skills else "")))
        kw = {"claude_cmd": claude_cmd} if claude_cmd else {}
        self.reply_cap = max_budget_usd               # a room keeps this much in hand before letting it speak
        self.cfg = engine.EngineConfig(model=model, tools=["Read"], max_turns=2, max_budget_usd=max_budget_usd,
                                       timeout_s=180, append_system_prompt_file=char, **kw)

    def speak(self, prompt):
        r = engine.run_turn(prompt, self.ws, self.cfg)
        return (r.text if r.ok else f"({self.name} could not answer: {r.error})"), r.cost_usd


class Command:
    def __init__(self, name, command, timeout_s=180, prompt_arg=False, cwd=None, pass_env=(), cost_per_call=0.0):
        self.name, self.command, self.timeout_s = name, list(command), timeout_s
        # an outside program's price is unknown here; "cost_per_call_usd" lets the room budget count it anyway
        self.cost_per_call = cost_per_call
        self.prompt_arg, self.cwd = prompt_arg, cwd    # some CLIs take the question as an argument, not on stdin
        if prompt_arg and sys.platform == "win32":
            # a .cmd/.bat runs through cmd.exe, which parses & | > in its arguments: the prompt (which other agents
            # and the owner write) would run as commands. There is no safe quoting for cmd.exe, so it is refused.
            target = shutil.which(self.command[0]) or self.command[0]
            if os.path.splitext(target)[1].lower() in (".cmd", ".bat"):
                raise ValueError(f"agent {name}: {target} is a batch file, so its arguments go through cmd.exe; "
                                 "with prompt_arg the prompt would be run as commands. Point it at the real .exe, "
                                 "or drop prompt_arg and send the prompt on stdin")
        # an outside program gets the same allow-listed environment as a claude turn: no bot token, no Jev key;
        # a CLI that needs its own key names it in "pass_env"
        keep = engine.ENV_ALLOW | {k.upper() for k in pass_env}
        self.env = {k: v for k, v in os.environ.items() if k.upper() in keep}

    def speak(self, prompt):
        argv = self.command + ([prompt] if self.prompt_arg else [])
        if self.cwd:
            os.makedirs(self.cwd, exist_ok=True)
        try:
            p = subprocess.run(argv, input=None if self.prompt_arg else prompt, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout_s, cwd=self.cwd,
                               env=self.env)
        except (OSError, subprocess.SubprocessError) as e:
            return f"({self.name} could not answer: {type(e).__name__})", 0.0
        out = p.stdout.strip()
        if p.returncode != 0 or not out:
            return f"({self.name} could not answer: exit {p.returncode})", 0.0
        return out, self.cost_per_call


def from_config(entry, workdir, claude_cmd=None, skills_repo=None):
    name, kind = str(entry.get("name") or ""), entry.get("kind")
    if not NAME.match(name):
        raise ValueError(f"agent name {name!r}: letters, digits, spaces, up to 31 characters")
    if kind == "claude":
        return ClaudeRole(name, entry.get("character", ""), entry.get("model", "claude-haiku-5-5"), workdir,
                          claude_cmd, skills_repo=skills_repo, skills_role=entry.get("skills_role", name))
    if kind == "command":
        if not entry.get("command"):
            raise ValueError(f"agent {name}: a command agent needs \"command\": [program, args...]")
        cwd = os.path.join(workdir, "agents", re.sub(r"\W+", "_", name), "cwd")   # an empty folder of its own
        return Command(name, entry["command"], entry.get("timeout_s", 180), bool(entry.get("prompt_arg")), cwd,
                       entry.get("pass_env") or (), cost_per_call=float(entry.get("cost_per_call_usd") or 0))
    raise ValueError(f"agent {name}: kind must be \"claude\" or \"command\"")
