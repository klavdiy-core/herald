"""What herald has learned, approved by the owner: a capped memory and class-level skills.

Lives under control/learned/, which the model never sees as files and cannot write. Before every turn it is put into
the system prompt (idea from Hermes Agent: memory and skills in the prompt; the difference here is that nothing gets
in without the owner's /approve).
"""
import json
import os
import re

MEMORY_CAP = 2200          # Hermes keeps MEMORY.md near this; the prompt stays small
SKILLS_CAP = 8000
SEP = "\n§\n"
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class Full(Exception):
    pass


def mangle(path):
    """Claude Code's folder name for a cwd under <config>/projects/."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def find_transcript(workspace, session_id, roots):
    for r in roots:
        p = os.path.join(r, "projects", mangle(workspace), f"{session_id}.jsonl")
        if os.path.exists(p):
            return p
    return None


def default_roots():
    roots = [os.path.join(os.path.expanduser("~"), ".claude")]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        roots.insert(0, os.environ["CLAUDE_CONFIG_DIR"])
    return roots


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _sections(body):
    """'## ' sections of a skill body, keyed by their heading line."""
    out, cur = {}, None
    for line in body.splitlines(keepends=True):
        if line.startswith("## "):
            cur = line.strip()
            out[cur] = ""
        if cur:
            out[cur] += line
    return out


class Notebook:
    def __init__(self, path):
        self.dir = path
        self.mem_path = os.path.join(path, "MEMORY.md")
        self.pending_path = os.path.join(path, "pending.json")

    # ---------- memory ----------
    def memory(self):
        if not os.path.exists(self.mem_path):
            return []
        with open(self.mem_path, encoding="utf-8") as f:
            return [e.strip() for e in f.read().split(SEP) if e.strip()]

    def add_memory(self, line):
        entries = self.memory() + [line.strip()]
        if len(SEP.join(entries)) > MEMORY_CAP:
            raise Full(f"memory is full ({MEMORY_CAP} chars): /forget something first")
        _write(self.mem_path, SEP.join(entries) + "\n")

    def forget(self, n):
        entries = self.memory()
        if not 1 <= n <= len(entries):
            raise IndexError(n)
        gone = entries.pop(n - 1)
        _write(self.mem_path, SEP.join(entries) + ("\n" if entries else ""))
        return gone

    # ---------- skills ----------
    def skills(self):
        root = os.path.join(self.dir, "skills")
        out = {}
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                p = os.path.join(root, name, "SKILL.md")
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        out[name] = f.read()
        return out

    def add_skill(self, draft):
        m = re.match(r"---\nname: (.+)\ndescription: .+?\n---\n", draft, re.S)
        if not m or not NAME.match(m.group(1).strip()):
            raise ValueError("a skill needs front matter with a plain lowercase name")
        name = m.group(1).strip()
        old = self.skills().get(name)
        if old:                                   # an umbrella grows: keep what is there, add new sections
            have = _sections(old)
            new = [s for h, s in _sections(draft).items() if h not in have]
            draft = old.rstrip("\n") + "\n" + "".join("\n" + s.rstrip("\n") + "\n" for s in new)
        _write(os.path.join(self.dir, "skills", name, "SKILL.md"), draft)
        return name

    # ---------- proposals waiting for the owner ----------
    def pending(self):
        if not os.path.exists(self.pending_path):
            return []
        with open(self.pending_path, encoding="utf-8") as f:
            return json.load(f)

    def set_pending(self, props):
        _write(self.pending_path, json.dumps(props, ensure_ascii=False))

    # ---------- into the prompt ----------
    def compose(self, persona=""):
        mem, sk = self.memory(), self.skills()
        if not mem and not sk:
            return persona
        parts = [persona.rstrip(), "", "# What you learned (approved by the owner; follow it)"]
        if mem:
            parts += ["", "## Memory", SEP.join(mem)]
        body = "\n\n".join(sk.values())
        if len(body) > SKILLS_CAP:
            body = body[:SKILLS_CAP] + "\n[... skills cut at the prompt limit ...]"
        if sk:
            parts += ["", "## Skills", body]
        return "\n".join(parts).lstrip() + "\n"
