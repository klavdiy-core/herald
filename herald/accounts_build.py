"""Build control/accounts.json from the worker LAUNCHERS, so the registry is never a hand list that goes stale.

Source of truth (the owner's word): each launcher script says the worker's name (`--name <Name>` on its `claude` line) and
its config folder (`$env:CLAUDE_CONFIG_DIR = '<dir>'`, '$var' references resolved from `$var = '<literal>'` lines of the
same file). Which ACCOUNT a config folder is logged into is read from <dir>/.claude.json -> oauthAccount.emailAddress.

The email is used only in memory, as a grouping key: config folders logged into one account become ONE account. It is
never written, printed, logged or put into an audit line - the registry holds names and paths only (accounts.py refuses
an email anywhere in it). The account's name is derived from its first config folder's name.

Which launchers to read is NOT in this code: control/accounts_sources.json {"launchers": ["<glob>", ...]}. Without that
file nothing is built and accounts.json stays a hand-kept file, as before.
Thresholds, plans and pinned flags of the previous accounts.json are kept; every difference from it is an audit line.
Only .claude.json is opened in a config folder (one key read); .credentials.json and every other login file never are.
"""
from __future__ import annotations

import glob
import json
import os
import re

from . import accounts as A

SOURCES_FILE = "accounts_sources.json"
REGISTRY_FILE = "accounts.json"
VAR_LIT = re.compile(r"^\s*`?\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']*)'", re.M)
CONFIG_SET = re.compile(r"`?\$env:CLAUDE_CONFIG_DIR\s*=\s*['\"]([^'\"]+)['\"]", re.I)
CLAUDE_NAME = re.compile(r"(?:^|[\s;&|(])claude(?:\.exe)?\s[^\n]*?--name\s+['\"]?([A-Za-z][A-Za-z0-9_-]{0,39})['\"]?(?:\s|$)", re.M)
VAR_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_launcher(text):
    """-> (name, config_dir, problem). One name and one config folder per launcher; anything else is a problem.
    Comments (# lines and <# ... #> blocks) are cut first: an old comment with a claude line is not a second name."""
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    vars_ = {k.lower(): v for k, v in VAR_LIT.findall(text)}
    names = sorted(set(CLAUDE_NAME.findall(text)))
    dirs = []
    for raw in CONFIG_SET.findall(text):
        d = VAR_REF.sub(lambda m: vars_.get(m.group(1).lower(), m.group(0)), raw)
        if "$" in d:
            return None, None, "папка конфига ссылается на неизвестную переменную"
        dirs.append(os.path.normpath(d))
    dirs = sorted(set(dirs), key=str.lower)
    if not names and not dirs:
        return None, None, None                          # not a worker launcher (a helper script): silently skipped
    if len(names) != 1:
        return None, None, "имён --name: %d (нужно одно)" % len(names)
    if len(dirs) != 1:
        return names[0], None, "папок CLAUDE_CONFIG_DIR: %d (нужна одна)" % len(dirs)
    return names[0], dirs[0], None


def account_key(config_dir):
    """the login's email from <config_dir>/.claude.json, only to group folders; None when there is no readable login.
    Only that file and only that key - never .credentials.json."""
    p = os.path.join(config_dir, ".claude.json")
    try:
        d = A._read_json_file(p)
    except Exception:                                    # unreadable, broken, or too deep (RecursionError): no login
        return None
    acc = d.get("oauthAccount") if isinstance(d, dict) else None
    em = acc.get("emailAddress") if isinstance(acc, dict) else None
    return em.strip().lower() if isinstance(em, str) and em.strip() else None


def _label(config_dir):
    """an account's name from its folder name: claude_team2_config -> team2; a name that could carry personal data
    (looks like an email or a token) becomes acc<N> later"""
    b = os.path.basename(os.path.normpath(config_dir)).lower()
    b = re.sub(r"^claude_|_?config$|^\.", "", b) or b
    return b if A.SAFE_NAME.match(b) and not A._secretish(b) else None


def build(launcher_paths, previous=None, key_of=account_key):
    """-> (registry dict to write, audit lines). previous = the old accounts.json dict (or None)."""
    audit, workers_cfg, troubled = [], {}, False
    for path in sorted(launcher_paths, key=str.lower):
        try:
            name, cfg, problem = parse_launcher(_read_text(path))
        except OSError:
            audit.append("лаунчер не читается: %s" % A._shown(os.path.basename(path)))
            troubled = True
            continue
        if problem:
            audit.append("лаунчер %s: %s" % (A._shown(os.path.basename(path)), problem))
            troubled = True
            continue
        if name is None:
            continue
        if name in workers_cfg and os.path.normcase(workers_cfg[name]) != os.path.normcase(cfg):
            audit.append("работник %s в двух лаунчерах с разными папками: оставлена первая" % A._shown(name))
            continue
        workers_cfg.setdefault(name, cfg)
    # group config folders by login; a folder without a readable login is an account of its own (and an audit line)
    groups, order = {}, []
    for cfg in sorted({os.path.normcase(c): c for c in workers_cfg.values()}.values(), key=str.lower):
        k = key_of(cfg)
        if k is None:
            audit.append("папка %s: вход не найден (.claude.json без oauthAccount) - отдельный аккаунт" % A._shown(os.path.basename(cfg)))
            k = "nologin:" + os.path.normcase(cfg)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(cfg)
    prev_acc = {a.get("name"): a for a in (previous or {}).get("accounts", []) if isinstance(a, dict)}
    prev_by_dir = {os.path.normcase(a.get("config_dir", "")): a for a in prev_acc.values() if isinstance(a.get("config_dir"), str)}
    accounts, name_of_dir, used = [], {}, set()
    for i, k in enumerate(order):
        dirs = groups[k]
        old = next((prev_by_dir[os.path.normcase(d)] for d in dirs if os.path.normcase(d) in prev_by_dir), None)
        nm = (old or {}).get("name") or _label(dirs[0]) or "acc%d" % (i + 1)
        if nm in used:
            nm = "%s_%d" % (nm, i + 1)
        used.add(nm)
        acc = {"name": nm, "config_dir": dirs[0], "plan": str((old or {}).get("plan", "?"))}
        if len(dirs) > 1:
            acc["config_dirs"] = dirs
            audit.append("аккаунт %s: %d папок конфига под одним входом" % (A._shown(nm), len(dirs)))
        accounts.append(acc)
        for d in dirs:
            name_of_dir[os.path.normcase(d)] = nm
    prev_w = (previous or {}).get("workers", {}) if isinstance((previous or {}).get("workers"), dict) else {}
    workers = {}
    for w, cfg in sorted(workers_cfg.items()):
        acc = name_of_dir[os.path.normcase(cfg)]
        pinned = bool(prev_w.get(w, {}).get("pinned", False)) if isinstance(prev_w.get(w), dict) else False
        workers[w] = {"account": acc, "pinned": pinned} if pinned else {"account": acc}
        old = prev_w.get(w, {}).get("account") if isinstance(prev_w.get(w), dict) else None
        if previous is not None and old != acc:
            audit.append("работник %s: аккаунт %s -> %s (по лаунчеру)" % (A._shown(w), A._shown(old) if old else "нет", A._shown(acc)))
    for w in sorted(set(prev_w) - set(workers)):
        row = prev_w[w] if isinstance(prev_w[w], dict) else None
        old_acc = prev_acc.get(row.get("account")) if row else None
        if troubled and row and isinstance(old_acc, dict) and old_acc.get("config_dir"):
            # a launcher could not be read or parsed: a missing worker may be exactly that launcher's - keep the old
            # row (and its old account) instead of removing a worker on a transient error or a stray line
            workers[w] = dict(row)
            if not any(a["name"] == old_acc["name"] for a in accounts):
                accounts.append({k: old_acc[k] for k in ("name", "config_dir", "config_dirs", "plan") if k in old_acc})
            audit.append("работник %s: в лаунчерах не найден, но есть лаунчер с ошибкой - оставлена прежняя строка" % A._shown(w))
            continue
        audit.append("работник %s: больше нет в лаунчерах - убран из реестра" % A._shown(w))
    reg = {"accounts": accounts, "workers": workers}
    for k in A.DEFAULTS:
        if previous and k in previous:
            reg[k] = previous[k]
    return reg, audit


def rebuild(control_dir):
    """if control/accounts_sources.json exists: rebuild control/accounts.json from the launchers it names.
    -> audit lines ([] and no write when there is no sources file). The new file is checked by accounts.load_registry
    BEFORE it replaces the old one; a refused build leaves the old file and says why."""
    sp = os.path.join(control_dir, SOURCES_FILE)
    if not os.path.exists(sp):
        return []
    try:
        src = A._read_json_file(sp)
    except ValueError:
        return ["%s нечитаем: реестр не пересобран" % SOURCES_FILE]
    pats = src.get("launchers") if isinstance(src, dict) else None
    if not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
        return ["%s: нужен список launchers: реестр не пересобран" % SOURCES_FILE]
    paths = sorted({os.path.normpath(p) for pat in pats for p in glob.glob(pat)}, key=str.lower)
    rp = os.path.join(control_dir, REGISTRY_FILE)
    previous = None
    if os.path.exists(rp):
        try:
            previous = A._read_json_file(rp)
        except ValueError:
            previous = None
    reg, audit = build(paths, previous if isinstance(previous, dict) else None)
    tmp = "%s.%d.tmp" % (rp, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    try:
        A.load_registry(tmp)
    except ValueError as e:
        os.remove(tmp)
        return audit + ["собранный реестр отклонён (%s): оставлен старый" % e]
    os.replace(tmp, rp)
    return audit
