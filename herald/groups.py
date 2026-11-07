"""groups.json: several Telegram groups with their own members in one herald.

{"her_id": <user id>, "groups": [{"chat_id": <negative id>, "name": "...", "kind": "hall" | "train",
                                  "owners": [user ids], "peer_bots": [bot ids], "persona": <file or "">, "notes": ""}]}

Strict like the main config: an unknown key, a chat_id that is not a whole negative number, a duplicate group or a
her_id that is also an owner refuses the start with a line that names the problem.
"""
import json
import os
import re

TOP_KEYS = {"her_id", "groups"}
GROUP_KEYS = {"chat_id", "name", "kind", "owners", "peer_bots", "persona", "notes", "members", "bots"}   # members, bots: the gatekeeper's, kept here
KINDS = ("hall", "train")


def _whole(v):
    return not isinstance(v, bool) and (isinstance(v, int) or (isinstance(v, str) and re.fullmatch(r"-?[0-9]+", v) is not None))


def _ids(v, what, where):
    if not isinstance(v, list) or not all(_whole(x) for x in v):
        raise ValueError(f"{where}: {what} must be a list of numeric ids, got {v!r}")
    return [int(x) for x in v]


def inside_workspace(path, workspace):
    """True if path is the workspace or lies in it, or if that cannot be told. Three guards, because a string compare of
    realpaths is fooled by aliases (r'\\\\?\\C:\\ws', r'\\\\localhost\\C$\\ws'): (1) a path that starts with two backslashes
    (UNC, extended, device) is refused outright; (2) the real strings; (3) file identity: walk path and its existing
    parents and ask os.path.samefile against the workspace."""
    if not all(isinstance(x, str) and x for x in (path, workspace)):
        raise ValueError(f"inside_workspace wants two paths, got {path!r} and {workspace!r}")
    if any(os.path.abspath(x).startswith("\\\\") for x in (path, workspace)):
        return True
    ws = os.path.normcase(os.path.realpath(workspace))
    p = os.path.normcase(os.path.realpath(path))
    if p == ws or p.startswith(ws.rstrip(os.sep) + os.sep):
        return True
    cur = os.path.abspath(path)
    while True:
        try:
            if os.path.exists(cur) and os.path.exists(workspace) and os.path.samefile(cur, workspace):
                return True
        except OSError:
            return True      # cannot tell: refuse
        up = os.path.dirname(cur)
        if up == cur:
            return False
        cur = up


def check_personas(glist, workspace):
    """A persona feeds system prompts, so it never lives where the model can write (the same rule as the single bot's F3)."""
    for g in glist:
        if not g["persona"]:
            continue
        if inside_workspace(g["persona"], workspace):
            raise ValueError(f"group {g['chat_id']}: persona must be outside the workspace: the model could rewrite it")


def load_groups(path):
    """-> (her_id or None, [group dict with int ids]); raises ValueError naming what is wrong"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"groups_file {path!r} cannot be read: {e}") from None
    if not isinstance(raw, dict):
        raise ValueError("groups_file must hold a JSON object")
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise ValueError(f"groups_file: unknown keys (refusing to guess): {sorted(unknown)}")
    her = raw.get("her_id")
    if her is not None:
        if not _whole(her) or int(her) <= 0:
            raise ValueError(f"groups_file: her_id must be a user id (a positive number), got {her!r}")
        her = int(her)
    groups = raw.get("groups", [])
    if not isinstance(groups, list):
        raise ValueError("groups_file: groups must be a list")
    out, seen = [], set()
    for i, g in enumerate(groups):
        where = f"groups[{i}]"
        if not isinstance(g, dict):
            raise ValueError(f"{where} must be an object")
        unknown = set(g) - GROUP_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys (refusing to guess): {sorted(unknown)}")
        cid = g.get("chat_id")
        if not _whole(cid) or int(cid) >= 0:
            raise ValueError(f"{where}: chat_id must be the group's negative id (a number like -100123...), got {cid!r}")
        cid = int(cid)
        if cid in seen:
            raise ValueError(f"{where}: chat_id {cid} is listed twice")
        seen.add(cid)
        kind = g.get("kind", "train")
        if kind not in KINDS:
            raise ValueError(f"{where}: kind must be one of {list(KINDS)}, got {kind!r}")
        owners = _ids(g.get("owners", []), "owners", where)
        if not owners:
            raise ValueError(f"{where}: owners must not be empty (a group nobody leads is refused)")
        if her is not None and her in owners:
            raise ValueError(f"{where}: her_id {her} must not be in owners (her rights are above an owner's)")
        persona = g.get("persona", "") or ""
        if not isinstance(persona, str):
            raise ValueError(f"{where}: persona must be a file path or empty")
        out.append({"chat_id": cid, "name": str(g.get("name", "")), "kind": kind, "owners": owners,
                    "peer_bots": _ids(g.get("peer_bots", []), "peer_bots", where),
                    "members": _ids(g.get("members", []), "members", where), "bots": _ids(g.get("bots", []), "bots", where),
                    "persona": os.path.abspath(persona) if persona else "", "notes": str(g.get("notes", ""))})
    return her, out
