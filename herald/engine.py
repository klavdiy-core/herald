"""Run one Claude Code turn, confined.

Path protection is Claude Code's own, not ours: `--restricted` removes code-running tools and WebFetch,
confines file tools to the working directory and refuses to approve writes to settings, git and tool
configuration; `--permission-prompts none` denies whatever would ask; `--permission-mode acceptEdits` lets
edits inside the workspace through (without it the model cannot write at all). These flags are fixed here and no
config can drop them. `herald doctor` checks they still hold on the installed version.
"""
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field

# acceptEdits alone lets the model write CLAUDE.md (seen in the live canary); deny rules hold in every mode
DENY = ["Edit(**/CLAUDE.md)", "Edit(**/CLAUDE.local.md)", "Edit(**/.claude/**)", "Edit(**/.git/**)",
        "Edit(**/.mcp.json)"]
FIXED_FLAGS = ["-p", "--restricted", "--permission-prompts", "none", "--permission-mode", "acceptEdits",
               "--strict-mcp-config", "--setting-sources", "", "--output-format", "json",
               "--settings", json.dumps({"permissions": {"deny": DENY}})]

SAFE_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "TodoWrite"}

# flags that would widen what the model can touch, or replace what herald fixes
FORBIDDEN_FLAGS = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--permission-mode",
                   "--permission-prompts", "--permission-prompt-tool", "--add-dir", "--tools", "--allowedTools",
                   "--allowed-tools", "--settings", "--setting-sources", "--mcp-config", "--plugin-dir", "--agents",
                   "--restricted", "--resume", "-r", "--continue", "-c"}

ENV_ALLOW = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
             "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL"}


@dataclass
class EngineConfig:
    claude_cmd: list = field(default_factory=lambda: ["claude"])
    model: str = "claude-sonnet-5-5"
    tools: list = field(default_factory=lambda: ["Read", "Write", "Edit", "Glob", "Grep"])
    max_turns: int = 20
    max_budget_usd: float = 1.0
    timeout_s: float = 600
    append_system_prompt_file: str = ""
    pass_env: list = field(default_factory=list)   # e.g. ["CLAUDE_CODE_OAUTH_TOKEN"], opt-in only
    extra_args: list = field(default_factory=list)

    def __post_init__(self):
        bad = [t for t in self.tools if t not in SAFE_TOOLS]
        if bad:
            raise ValueError(f"tools not allowed in herald: {bad} (no code-running tools, no WebFetch)")
        if self.max_turns < 1 or self.max_budget_usd <= 0 or self.timeout_s <= 0:
            raise ValueError("max_turns, max_budget_usd and timeout_s must be set and positive")
        for a in self.extra_args:
            if a.split("=", 1)[0] in FORBIDDEN_FLAGS:
                raise ValueError(f"extra_args may not contain {a}")


@dataclass
class TurnResult:
    ok: bool
    text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    denials: list = field(default_factory=list)
    error: str = ""
    num_turns: int = 0


def build_argv(cfg, session_id=None):
    argv = list(cfg.claude_cmd) + FIXED_FLAGS + [
        "--model", cfg.model, "--tools", ",".join(cfg.tools),
        "--max-turns", str(cfg.max_turns), "--max-budget-usd", str(cfg.max_budget_usd)]
    if cfg.append_system_prompt_file:
        argv += ["--append-system-prompt-file", cfg.append_system_prompt_file]
    if session_id:
        argv += ["--resume", session_id]
    return argv + list(cfg.extra_args)


def child_env(cfg):
    keep = ENV_ALLOW | set(cfg.pass_env)
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PYTHONUTF8"] = "1"
    return env


def _kill_tree(proc):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def run_turn(prompt, workspace, cfg, session_id=None):
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(build_argv(cfg, session_id), cwd=workspace, env=child_env(cfg),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", **kw)
    try:
        out, err = proc.communicate(prompt, timeout=cfg.timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return TurnResult(False, error=f"timeout after {cfg.timeout_s:g}s, turn killed")
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return TurnResult(False, error=f"claude did not return json (exit {proc.returncode}): {err.strip()[-300:]}")
    denials = [d.get("tool_name", "?") for d in data.get("permission_denials") or []]
    res = TurnResult(not data.get("is_error") and data.get("subtype") == "success",
                     data.get("result") or "", data.get("session_id") or "", float(data.get("total_cost_usd") or 0),
                     denials, num_turns=int(data.get("num_turns") or 0))
    if not res.ok:
        res.error = f"claude turn failed: {data.get('subtype')}"
    return res
