"""What herald has learned, approved by the owner: a capped memory and class-level skills.

Lives under control/learned/, which the model never sees as files and cannot write. Before every turn it is put into
the system prompt (idea from Hermes Agent: memory and skills in the prompt; the difference here is that nothing gets
in without the owner's /approve).
"""
import json
import os
import re
import time
import unicodedata

from .memory import sources as memory_sources

MEMORY_CAP = 2200          # Hermes keeps MEMORY.md near this; the prompt stays small
SKILLS_CAP = 8000
SEP = "\n§\n"
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9_-])?$")      # no trailing dot: Windows drops it, and that is another skill
READERS = ("worker", "main")
ENTRY_LABEL = re.compile(r"^[-*\s]*\[\s*sensitivity\s*:\s*([^\]]*?)\s*\]", re.I)
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
SKILL_LABEL = re.compile(r"sensitivity['\"]?\s*:\s*(.*)$", re.I)
UNLABELLED = memory_sources.UNLABELLED
UNREADABLE = "unreadable"       # the word is there and we cannot read the value: that is a closed label, never no label


def _visible_text(text):
    return "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Mn", "Me"))      # marks that show as nothing


def _entry_label(entry):
    """Only the first line can label an entry. Mentioning the word there closes it unless it is plainly a public label."""
    first = _visible_text(entry).split("\n", 1)[0]
    if "sensitivity" not in first.casefold():
        return UNLABELLED
    found = ENTRY_LABEL.match(first)
    if not found or "sensitivity" in first[found.end():].casefold():
        return UNREADABLE               # a second label must not hide behind a public first one
    return found.group(1)


def _skill_label(text):
    """A spelling we do not recognise must never read as "no label": every line that looks like one counts, and any that is
    not plainly public decides."""
    text = _visible_text(text)
    front = FRONT_MATTER.match(text)
    found = []
    for line in (front.group(1) if front else text).splitlines():
        if "sensitivity" in line.casefold():
            match = SKILL_LABEL.search(line)
            found.append(match.group(1).split("#")[0].strip(" '\"{}[]\t") if match else UNREADABLE)
    if not found:
        return UNLABELLED
    return next((v for v in found if not memory_sources.is_public(v)), found[0])


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


def _write(path, text, errors="strict"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors=errors) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())        # a cut must not replace the file with an empty one: it holds the hidden entries too
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

    def _allowed(self, label, path, reader_scope):
        if reader_scope not in READERS:
            raise ValueError("unknown reader scope")
        if reader_scope == "main":
            return True
        known = memory_sources.load()
        if known.named_main_only(path):
            return False                    # a listed closed root decides, whatever a label inside it says
        if label is not UNLABELLED:
            return memory_sources.is_public(label)              # an unknown value stays hidden
        return known.class_of(path) != memory_sources.HIDDEN

    # ---------- memory ----------
    def _all_entries(self, errors="replace"):
        """Every entry on disk. All writers start from this: rewriting the file from a filtered list would delete the rest."""
        if not os.path.exists(self.mem_path):
            return []
        with open(self.mem_path, encoding="utf-8", errors=errors) as f:
            return [e.strip() for e in f.read().split(SEP) if e.strip()]

    def memory(self, reader_scope="worker"):
        return [e for e in self._all_entries() if self._entry_visible(e, reader_scope)]

    def _entry_visible(self, entry, reader_scope):
        return self._allowed(_entry_label(entry), self.mem_path, reader_scope)

    def add_memory(self, line):
        line = re.sub(r"(?<=\n)§(?=\n)", "(§)", line.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8", "replace").decode("utf-8"))          # the separator inside an entry would split it in two
        kept = self._all_entries(errors="surrogateescape")      # a writer gives back the very bytes it did not understand
        if line.strip() in kept:
            return                        # approving the same lesson twice keeps one line
        entries = kept + [line.strip()]
        if len(SEP.join(entries)) > MEMORY_CAP:
            raise Full(f"memory is full ({MEMORY_CAP} chars): /forget something first")
        _write(self.mem_path, SEP.join(entries) + "\n", errors="surrogateescape")

    def forget(self, n, reader_scope="worker"):
        """n counts the list this scope sees; the entry removed is that one, and every hidden entry stays."""
        entries = self._all_entries(errors="surrogateescape")
        visible = [i for i, e in enumerate(entries) if self._entry_visible(e, reader_scope)]
        if not 1 <= n <= len(visible):
            raise IndexError(n)
        gone = entries.pop(visible[n - 1])
        _write(self.mem_path, SEP.join(entries) + ("\n" if entries else ""), errors="surrogateescape")
        return gone

    # ---------- skills ----------
    def skills(self, reader_scope="worker", errors="replace"):
        root = os.path.join(self.dir, "skills")
        out = {}
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                p = os.path.join(root, name, "SKILL.md")
                if os.path.exists(p):
                    with open(p, encoding="utf-8", errors=errors) as f:
                        text = f.read()
                    if self._allowed(_skill_label(text), p, reader_scope):
                        out[name] = text
        return out

    def add_skill(self, draft):
        m = re.match(r"---\nname: (.+)\ndescription: .+?\n---\n", draft, re.S)
        if not m or not NAME.match(m.group(1).strip()):
            raise ValueError("a skill needs front matter with a plain lowercase name")
        name = m.group(1).strip()
        old = self.skills(reader_scope="main", errors="surrogateescape").get(name)       # the writer sees everything, or it would overwrite
        if old and _skill_label(old) != _skill_label(draft):
            raise ValueError("a skill keeps one label: merging would put this text under the other one")
        if old:                                   # an umbrella grows: keep what is there, add new sections
            have = _sections(old)
            new = [s for h, s in _sections(draft).items() if h not in have]
            draft = old.rstrip("\n") + "\n" + "".join("\n" + s.rstrip("\n") + "\n" for s in new)
        _write(os.path.join(self.dir, "skills", name, "SKILL.md"), draft, errors="surrogateescape")
        return name

    def forget_skill(self, name, reader_scope="worker"):
        """Out of every prompt, but kept: moved to learned/archive/ (Hermes archives, never deletes)."""
        src = os.path.join(self.dir, "skills", name)
        if not NAME.match(name) or not os.path.isdir(src) or name not in self.skills(reader_scope=reader_scope):
            raise KeyError(name)       # the same for a skill this scope cannot see as for one that does not exist
        dst = os.path.join(self.dir, "archive", f"{name}-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        return dst

    def knows(self, prop):
        """True when an approved lesson already holds this proposal."""
        if prop["kind"] == "memory":
            return prop["draft"].strip() in self._all_entries()
        m = re.match(r"---\nname: (\S+)", prop["draft"])
        old = self.skills(reader_scope="main").get(m.group(1)) if m else None
        return bool(old) and set(_sections(prop["draft"])) <= set(_sections(old))

    # ---------- proposals waiting for the owner ----------
    def pending(self):
        if not os.path.exists(self.pending_path):
            return []
        try:
            with open(self.pending_path, encoding="utf-8") as f:
                props = json.load(f)
        except (ValueError, OSError):
            return []                     # unreadable: nothing waiting; the next /learn writes it fresh
        ok = isinstance(props, list) and all(isinstance(p, dict) and {"kind", "draft"} <= set(p) for p in props)
        return props if ok else []

    def set_pending(self, props):
        _write(self.pending_path, json.dumps(props))        # escaped: half an emoji in a draft must not stop the write

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
