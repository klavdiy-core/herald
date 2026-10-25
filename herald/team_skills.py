"""A team's shared skills, picked up by each role at start.

The skills live in a git repository: skills/<name>/SKILL.md (front matter with name and description) and
roles/<Role>.md (one line per skill the role has: `name` · why · from where). At start a role pulls the repository,
gets a table of contents of ITS skills in its system prompt, and a copy of those SKILL.md files in its own folder,
where its confined Read can open them when needed. Other roles' skills are not given to it.
"""
import os
import re
import shutil
import subprocess

ROLE = re.compile(r"^[\w\- ]{1,40}$")
LISTED = re.compile(r"`([a-z0-9][a-z0-9._-]{0,63})`")
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
BARE = re.compile(r"[a-z0-9][a-z0-9._]*-[a-z0-9._-]+(?=[\s:·,]|$)")   # «anime-by-code ·» without backticks


def pull(repo, timeout_s=30):
    """git pull --ff-only; (ok, reason). A failure is not fatal: the local copy is used."""
    try:
        p = subprocess.run(["git", "-C", repo, "pull", "--ff-only", "--quiet"], capture_output=True, text=True,
                           timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git pull failed: {type(e).__name__}"
    return (p.returncode == 0), (p.stderr.strip()[:200] or f"exit {p.returncode}")


def _front(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = FRONT.match(f.read())
    except OSError:
        return None
    if not m:
        return None
    fields = dict(re.findall(r"^(name|description):\s*(.+?)\s*$", m.group(1), re.M))
    return fields if fields.get("name") and fields.get("description") else None


def for_role(repo, role):
    """([{name, description, path}], warnings) for the skills the role's file lists, in its order."""
    if not role or not ROLE.match(role):
        return [], [f"role name {role!r} is not a plain name"]
    roles_dir = os.path.join(repo, "roles")
    path = os.path.join(roles_dir, f"{role}.md")
    if not os.path.isfile(path):
        return [], []                                   # a role without a file simply has no team skills
    names, warnings = [], []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.lstrip().startswith("- "):          # list lines only; a «---» rule is not one
                continue
            m = LISTED.search(line)
            if m:
                names.append(m.group(1))
            elif BARE.match(line.lstrip()[2:]):            # starts like a skill name, a note line does not
                # a skill written without backticks used to drop out in silence (Filin got 4 of 6, 25.10)
                warnings.append(f"roles/{role}.md line {n}: no `name` in backticks, not picked up: "
                                f"{line.strip()[:80]}")
    skills = []
    for name in dict.fromkeys(names):
        sk = os.path.join(repo, "skills", name, "SKILL.md")
        meta = _front(sk) if os.path.isfile(sk) else None
        if meta is None:
            warnings.append(f"skill {name}: missing, or its SKILL.md has no name/description; skipped")
            continue
        skills.append({"name": name, "description": meta["description"][:300], "path": sk})
    return skills, warnings


def toc(skills, folder="team-skills"):
    if not skills:
        return ""
    lines = ["# Team skills", f"Read {folder}/<name>/SKILL.md when one fits the task."]
    lines += [f"- {s['name']}: {s['description']}" for s in skills]
    return "\n".join(lines)


def install(skills, workspace, folder="team-skills"):
    """Copy the role's SKILL.md files into its own folder (and only those: stale ones are removed)."""
    dst_root = os.path.join(workspace, folder)
    if os.path.isdir(dst_root):
        shutil.rmtree(dst_root)
    for s in skills:
        dst = os.path.join(dst_root, s["name"])
        os.makedirs(dst, exist_ok=True)
        shutil.copyfile(s["path"], os.path.join(dst, "SKILL.md"))


def setup(repo, role, workspace, log=None):
    """Pull, pick the role's skills, copy them in; returns the table of contents for the system prompt."""
    if not repo or not role:
        return ""
    ok, why = pull(repo)
    if not ok and log:
        log(f"team skills: {why}; using the local copy")
    skills, warnings = for_role(repo, role)
    for w in warnings:
        if log:
            log(f"team skills: {w}")
    install(skills, workspace)
    return toc(skills)
