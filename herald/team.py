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
    out = {"repeated_errors": [], "recoveries": [], "corrections": [],
           "typed": sorted({w for r in reports for w in r.get("typed", [])})}
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
    reps = OrderedDict((role, _merge([learning.analyze(p, ignore, min_repeat=1) for p in paths]))
                       for role, paths in by_role.items())
    typed = sorted({w for r in reps.values() for w in r["typed"]}) if private else []   # every role's typed words
    res = {"roles": OrderedDict()}
    seen, fixes = {}, {}                         # signature -> {role: count}; signature -> (fix, role)

    def show(s):
        return learning._scrub(learning.redact(s, private), typed)

    for role, rep in reps.items():
        rep["typed"] = typed
        for g in rep["all_errors"]:
            if learning.redact(g["signature"], private) == learning.WITHHELD:
                continue
            sig = show(g["signature"])
            seen.setdefault(sig, {})[role] = seen.get(sig, {}).get(role, 0) + g["count"]
        for r in rep["recoveries"]:
            fixes.setdefault(show(r["signature"]), (learning._scrub(learning._short(r["fix_input"], private), typed),
                                                    role))
        props = learning.propose(rep, private=private)
        with open(os.path.join(out_dir, f"{role}.md"), "w", encoding="utf-8") as f:
            f.write(learning.render_md(props, source=f"{role}, {len(by_role[role])} session(s)"))
        res["roles"][role] = {"sessions": len(by_role[role]), "proposals": len(props)}
    shared = sorted(((s, r) for s, r in seen.items() if len(r) >= 2), key=lambda x: -sum(x[1].values()))
    lines = ["# Team learning", "", "| role | sessions | proposals |", "|---|---|---|"]
    lines += [f"| [{k}]({k}.md) | {v['sessions']} | {v['proposals']} |" for k, v in res["roles"].items()]
    lines += ["", "## Shared: errors more than one role hit (with a fix one of them found)", ""]
    lines += [f"- `{s}` — " + ", ".join(f"{k} {n}x" for k, n in r.items())
              + (f" — fixed in {fixes[s][1]} by `{fixes[s][0]}`" if s in fixes else "") for s, r in shared] or ["(none)"]
    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    res["shared"] = shared
    return res
