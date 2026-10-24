"""Learning for a team of Claude Code sessions: one proposals file per role, plus what the whole team keeps hitting.

A role is the session name (`claude --name X`, stored in the transcript as a custom-title record). Unnamed
sessions are skipped. Private by default: proposals carry mechanics, never quoted messages. Nothing is applied;
a person reads the folder and decides.
"""
import json
import os
import time
from collections import OrderedDict

from . import learning


def title(path):
    """The session's last name, or None."""
    name = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"custom-title"' in line:
                try:
                    name = json.loads(line).get("customTitle") or name
                except ValueError:
                    pass
    return name


def _merge(reports):
    groups = OrderedDict()
    out = {"repeated_errors": [], "recoveries": [], "corrections": []}
    for r in reports:
        for g in r["repeated_errors"]:
            m = groups.setdefault(g["signature"], dict(g, count=0, tools=[]))
            m["count"] += g["count"]
            m["tools"] = sorted(set(m["tools"]) | set(g["tools"]))
            m["first"], m["last"] = min(m["first"], g["first"]), max(m["last"], g["last"])
        out["recoveries"] += r["recoveries"]
        out["corrections"] += r["corrections"]
    out["all_errors"] = list(groups.values())
    out["repeated_errors"] = [g for g in groups.values() if g["count"] >= 2]
    return out


def run(projects_dir, out_dir, since_hours=None, private=True, ignore=None):
    cutoff = time.time() - since_hours * 3600 if since_hours else 0
    by_role = OrderedDict()
    for name in sorted(os.listdir(projects_dir)):
        p = os.path.join(projects_dir, name)
        if not name.endswith(".jsonl") or os.path.getmtime(p) < cutoff:
            continue
        role = title(p)
        if role:
            by_role.setdefault(role, []).append(p)
    os.makedirs(out_dir, exist_ok=True)
    res = {"roles": OrderedDict()}
    seen = {}                                   # signature -> {role: count}
    for role, paths in by_role.items():
        rep = _merge([learning.analyze(p, ignore, min_repeat=1) for p in paths])
        for g in rep["all_errors"]:
            sig = learning.redact(g["signature"], private)
            if sig == learning.WITHHELD:
                continue
            seen.setdefault(sig, {})[role] = seen.get(sig, {}).get(role, 0) + g["count"]
        props = learning.propose(rep, private=private)
        with open(os.path.join(out_dir, f"{role}.md"), "w", encoding="utf-8") as f:
            f.write(learning.render_md(props, source=f"{role}, {len(paths)} session(s)"))
        res["roles"][role] = {"sessions": len(paths), "proposals": len(props)}
    shared = sorted(((s, r) for s, r in seen.items() if len(r) >= 2), key=lambda x: -sum(x[1].values()))
    lines = ["# Team learning", "", "| role | sessions | proposals |", "|---|---|---|"]
    lines += [f"| [{k}]({k}.md) | {v['sessions']} | {v['proposals']} |" for k, v in res["roles"].items()]
    lines += ["", "## Shared: errors more than one role hit", ""]
    lines += [f"- `{s}` — " + ", ".join(f"{k} {n}x" for k, n in r.items()) for s, r in shared] or ["(none)"]
    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    res["shared"] = shared
    return res
