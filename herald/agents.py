"""Participants of a room. Two kinds, both with .name and .speak(prompt) -> (text, cost_usd):

- "claude": a role (its own character and model) that answers through herald's confined Claude Code turn, one fresh
  turn per reply, in its own empty folder, with Read as the only tool.
- "command": any program that reads the prompt on stdin and prints the answer (another model's CLI, a script).

A participant that fails says so in its reply; it never takes the room down.
"""
import os
import re
import subprocess

from . import engine

NAME = re.compile(r"^[A-Za-zЀ-ӿ][\w \-Ѐ-ӿ]{0,30}$")


class ClaudeRole:
    def __init__(self, name, character, model, workdir, claude_cmd=None, max_budget_usd=0.2):
        self.name = name
        home = os.path.join(workdir, "agents", re.sub(r"\W+", "_", name))
        self.ws = os.path.join(home, "ws")
        os.makedirs(self.ws, exist_ok=True)
        char = os.path.join(home, "character.md")
        with open(char, "w", encoding="utf-8") as f:
            f.write(f"You are {name}. {character}".strip())
        kw = {"claude_cmd": claude_cmd} if claude_cmd else {}
        self.cfg = engine.EngineConfig(model=model, tools=["Read"], max_turns=2, max_budget_usd=max_budget_usd,
                                       timeout_s=180, append_system_prompt_file=char, **kw)

    def speak(self, prompt):
        r = engine.run_turn(prompt, self.ws, self.cfg)
        return (r.text if r.ok else f"({self.name} could not answer: {r.error})"), r.cost_usd


class Command:
    def __init__(self, name, command, timeout_s=180, prompt_arg=False, cwd=None):
        self.name, self.command, self.timeout_s = name, list(command), timeout_s
        self.prompt_arg, self.cwd = prompt_arg, cwd    # some CLIs take the question as an argument, not on stdin

    def speak(self, prompt):
        argv = self.command + ([prompt] if self.prompt_arg else [])
        if self.cwd:
            os.makedirs(self.cwd, exist_ok=True)
        try:
            p = subprocess.run(argv, input=None if self.prompt_arg else prompt, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout_s, cwd=self.cwd)
        except (OSError, subprocess.SubprocessError) as e:
            return f"({self.name} could not answer: {type(e).__name__})", 0.0
        out = p.stdout.strip()
        if p.returncode != 0 or not out:
            return f"({self.name} could not answer: exit {p.returncode})", 0.0
        return out, 0.0              # an outside program's price is not known here


def from_config(entry, workdir, claude_cmd=None):
    name, kind = str(entry.get("name") or ""), entry.get("kind")
    if not NAME.match(name):
        raise ValueError(f"agent name {name!r}: letters, digits, spaces, up to 31 characters")
    if kind == "claude":
        return ClaudeRole(name, entry.get("character", ""), entry.get("model", "claude-haiku-5-5"), workdir,
                          claude_cmd)
    if kind == "command":
        if not entry.get("command"):
            raise ValueError(f"agent {name}: a command agent needs \"command\": [program, args...]")
        cwd = os.path.join(workdir, "agents", re.sub(r"\W+", "_", name), "cwd")   # an empty folder of its own
        return Command(name, entry["command"], entry.get("timeout_s", 180), bool(entry.get("prompt_arg")), cwd)
    raise ValueError(f"agent {name}: kind must be \"claude\" or \"command\"")
