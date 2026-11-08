"""Accounts and limits: a watcher that PROPOSES moving a worker console to a less loaded account. It never moves anyone.

Inputs, and only these:
  control/accounts.json      the registry: accounts {name, config_dir, plan}, workers {name: {account, pinned?}}, thresholds
  control/usage_log.jsonl    percentages per account, written by a separate meter (it reads the login, this file never does)
  <config_dir>/projects/**/*.jsonl   Claude Code transcripts: only "agent-name" records and the token numbers of
                             "assistant" records are read; every other record type is dropped before its content is used
Login files (.credentials.json) are never opened here, links inside projects are never followed, and nothing is written
inside a config_dir.

One API answer is written to a transcript as several lines with the same message id, so tokens are summed per id.
A worker's share of the window is shown as an interval, because how the limit weighs cache reads is not measured:
  all = input + cache_creation + cache_read + output,  b = output + cache_creation,  share = worker / all spenders.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import time
from dataclasses import dataclass

TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
SECRET_KEYS = re.compile(r"token|secret|password|passwd|credential|oauth|api[_-]?key|refresh", re.I)
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# token shapes: Anthropic/OpenAI sk-, GitHub, Slack, AWS, Google, JWT, Telegram bot, any long opaque run
SECRET_VALUE = re.compile(r"sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"
                          r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
                          r"|\d+:[A-Za-z0-9_-]{30,}|[A-Za-z0-9_\-]{40,}")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
STATE_FILE = "accounts_state.json"
STATE_KEEP = 500
DEFAULTS = {"threshold_pct": 80, "free_below_pct": 50, "max_age_min": 30, "window_h": 5}


@dataclass
class Proposal:
    worker: str
    source: str
    target: str
    text: str
    pair_key: str = ""


def _shown(v):
    """a value fit to appear in a message or the audit: a short plain name that is not token-shaped, else a placeholder"""
    if isinstance(v, str) and SAFE_NAME.match(v) and not SECRET_VALUE.search(v) and not EMAIL.search(v):
        return v
    return "<скрыто>"


def _refuse(where, why):
    raise ValueError("accounts.json: %s: %s" % (where, why))


def _secretish(s):
    return bool(SECRET_VALUE.search(s) or EMAIL.search(s))


def _scan_secrets(obj, where):
    """no login material and no email anywhere in the registry, in keys or values; neither is ever echoed"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k)
            if SECRET_KEYS.search(ks):
                _refuse("%s.%s" % (where, _shown(ks)), "a token-like field; tokens never live in the registry")
            if _secretish(ks):
                _refuse(where, "a key that looks like a token or an email; the registry holds names and paths only")
            _scan_secrets(v, "%s.%s" % (where, _shown(ks)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_secrets(v, "%s[%d]" % (where, i))
    elif isinstance(obj, str):
        # config_dir paths may be long; only the token SHAPES with a prefix apply to them, not the generic long run
        long_run_ok = os.sep in obj or "/" in obj
        if EMAIL.search(obj):
            _refuse(where, "an email; the registry holds names and paths only")
        m = SECRET_VALUE.search(obj)
        if m and not (long_run_ok and re.fullmatch(r"[A-Za-z0-9_\-]{40,}", m.group(0))):
            _refuse(where, "a value that looks like a token; tokens never live in the registry")


def _read_json_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    return json.loads(raw.decode("utf-8-sig", errors="replace"))


def load_registry(path):
    try:
        d = _read_json_file(path)
    except ValueError as e:
        _refuse("file", "not valid JSON (%s)" % e.__class__.__name__)
    if not isinstance(d, dict):
        _refuse("file", "the top level must be an object")
    _scan_secrets(d, "root")
    accounts = d.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        _refuse("accounts", "a non-empty list is required")
    names = {}
    for i, a in enumerate(accounts):
        if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not a["name"]:
            _refuse("accounts[%d]" % i, "needs a non-empty name")
        if not isinstance(a.get("config_dir"), str) or not a["config_dir"]:
            _refuse("accounts[%d]" % i, "needs a config_dir")
        if a["name"] in names:
            _refuse("accounts[%d]" % i, "duplicate name %s" % _shown(a["name"]))
        dirs = a.get("config_dirs", [a["config_dir"]])        # several config folders logged into ONE account (accounts_build)
        if not isinstance(dirs, list) or not dirs or not all(isinstance(x, str) and x for x in dirs):
            _refuse("accounts[%d]" % i, "config_dirs must be a non-empty list of paths")
        if a["config_dir"] not in dirs:
            dirs = [a["config_dir"]] + dirs
        names[a["name"]] = {"name": a["name"], "config_dir": a["config_dir"], "config_dirs": dirs, "plan": str(a.get("plan", "?"))}
    workers = d.get("workers")
    if not isinstance(workers, dict):
        _refuse("workers", "an object {name: {account, pinned?}} is required")
    ws = {}
    for w, v in workers.items():
        if not isinstance(v, dict):
            _refuse("workers.%s" % _shown(w), "must be an object {account, pinned?}, not a bare string")
        acc = v.get("account")
        if not isinstance(acc, str):
            _refuse("workers.%s" % _shown(w), "account must be a name (a string)")
        if acc not in names:
            _refuse("workers.%s" % _shown(w), "account %s is not in accounts" % _shown(acc))
        ws[w] = {"account": acc, "pinned": bool(v.get("pinned", False))}
    reg = {"accounts": names, "workers": ws}
    for k, dv in DEFAULTS.items():
        v = d.get(k, dv)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            _refuse(k, "must be a positive number")
        reg[k] = v
    return reg


def _num(v):
    """a token count or a percentage: a finite, non-negative int/float; anything else is not a number"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return v


def parse_transcript_line(line):
    """{"kind": "usage", "id", "timestamp", "usage"} | {"kind": "agent", "name"} | None; nothing else of the line survives"""
    try:
        o = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(o, dict):
        return None
    t = o.get("type")
    if t == "agent-name":
        name = o.get("agentName")
        return {"kind": "agent", "name": name} if isinstance(name, str) and name else None
    if t != "assistant":
        return None
    m = o.get("message")
    if not isinstance(m, dict):
        return None
    u = m.get("usage")
    if not isinstance(u, dict):
        return None
    nums = {k: (_num(u.get(k, 0)) or 0) for k in TOKEN_KEYS}
    mid = m.get("id")
    ts = o.get("timestamp")
    return {"kind": "usage", "id": mid if isinstance(mid, str) else None,
            "timestamp": ts if isinstance(ts, str) else None, "usage": nums}


def _epoch(s):
    """ISO time -> epoch seconds; no offset = UTC; any offset, fractions and 'Z' are read; None if unreadable"""
    if not isinstance(s, str) or not s.strip():
        return None
    v = s.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _latest(usage_rows, reg, now, audit):
    """the freshest readable row per known account; error rows, stale rows and unknown accounts never decide"""
    best = {}
    by_dir = {os.path.normcase(os.path.normpath(a["config_dir"])): n for n, a in reg["accounts"].items()}
    for r in usage_rows:
        if not isinstance(r, dict) or "error" in r:
            continue
        acc = r.get("account")
        if acc is None and isinstance(r.get("config_dir"), str):
            acc = by_dir.get(os.path.normcase(os.path.normpath(r["config_dir"])))
            if acc is None:
                audit.append("замер для неизвестного аккаунта (папка %s)" % _shown(os.path.basename(os.path.normpath(r["config_dir"]))))
                continue
        if not isinstance(acc, str) or acc not in reg["accounts"]:
            audit.append("замер для неизвестного аккаунта %s" % _shown(acc))
            continue
        at = _epoch(r.get("at_utc"))
        if at is None:
            continue
        if acc not in best or at > best[acc][0]:
            best[acc] = (at, r)
    fresh = {}
    for acc, (at, r) in best.items():
        age_min = (now - at) / 60.0
        if age_min > reg["max_age_min"]:
            audit.append("замер устарел: аккаунт %s, %d мин" % (acc, int(age_min)))
            continue
        fresh[acc] = r
    return fresh


def _hot(r, thr):
    s, w = _num(r.get("session_pct")), _num(r.get("week_pct"))
    return (s is not None and s >= thr) or (w is not None and w >= thr)


def _free(r, below):
    """free = the week is measured and under the bar, and the 5-hour window is under it OR not open at all.
    None window = nobody is spending a window there right now (the lead's choice (b), 08.11); an unknown WEEK is
    never free - no measure is not 0 %."""
    s, w = _num(r.get("session_pct")), _num(r.get("week_pct"))
    return w is not None and w < below and (s is None or s < below)


def _shares(reg, account, token_rows, start, now):
    """per worker who spent on `account`: (share_all, share_b), summed over unique message ids inside [start, now].
    A row that carries "account" counts for THAT account (the folder it was found in tells it); a row without
    "account" falls back to the registry's membership. Every spender is in the divisor, registered or not."""
    members = {w for w, v in reg["workers"].items() if v["account"] == account}
    seen, tot_all, tot_b = set(), {}, {}
    for t in token_rows:
        w = t.get("worker")
        if "account" in t:
            if t["account"] != account:
                continue
        elif w not in members:
            continue
        ts = _epoch(t.get("ts"))
        if ts is None or ts < start or ts > now:
            continue
        mid = t.get("id")
        if mid:
            if (w, mid) in seen:
                continue
            seen.add((w, mid))
        n = [_num(t.get(k)) or 0 for k in TOKEN_KEYS]
        tot_all[w] = tot_all.get(w, 0) + sum(n)
        tot_b[w] = tot_b.get(w, 0) + n[3] + n[1]
    sa, sb = sum(tot_all.values()), sum(tot_b.values())
    if sa <= 0:
        return {}
    return {w: (tot_all[w] / sa, (tot_b.get(w, 0) / sb) if sb > 0 else tot_all[w] / sa) for w in tot_all}


def _hm(epoch):
    return time.strftime("%H:%M", time.gmtime(epoch)) if epoch is not None else "?"


def _half_up(x):
    return int(math.floor(x + 0.5))


def propose(registry, usage_rows, token_rows, now, audit=None):
    """pure: the proposals for this moment; no file, no process, no network. Each free target takes one worker per pass."""
    audit = audit if audit is not None else []
    fresh = _latest(usage_rows, registry, now, audit)
    thr, below = registry["threshold_pct"], registry["free_below_pct"]
    targets = sorted((n for n, r in fresh.items() if _free(r, below)),
                     # an unopened window counts as «at the bar»: a measured low window is preferred over a guess
                     key=lambda n: ((below if _num(fresh[n].get("session_pct")) is None
                                     else _num(fresh[n].get("session_pct"))) + _num(fresh[n].get("week_pct")), n))
    used = set()
    out = []
    for src in sorted(fresh):
        r = fresh[src]
        if not _hot(r, thr):
            continue
        resets = _epoch(r.get("session_resets"))
        pct_s = _num(r.get("session_pct"))
        if pct_s is not None:
            if resets is None:
                audit.append("сброс окна не читается: аккаунт %s, предложения нет" % src)
                continue
            if resets <= now:
                audit.append("окно уже сброшено: аккаунт %s, ждём нового замера" % src)
                continue
            start, scope, base = resets - registry["window_h"] * 3600, "окна", pct_s
        else:
            # no 5-hour row: the account is hot by the WEEK; the share is of the week's spend we can see, said so
            week_end = _epoch(r.get("week_resets"))
            if week_end is None or week_end <= now:
                audit.append("сброс недели не читается: аккаунт %s, предложения нет" % src)
                continue
            start, scope, base, resets = week_end - 7 * 86400, "недели", _num(r.get("week_pct")) or 0, week_end
        tgt = next((t for t in targets if t != src and t not in used), None)
        if tgt is None:
            continue
        sh = _shares(registry, src, token_rows, start, now)
        movable = []
        for w, (a, b) in sh.items():
            reg_w = registry["workers"].get(w)
            if reg_w is None or reg_w["pinned"]:
                continue                                  # unregistered and pinned spenders count, but never move
            if reg_w["account"] != src:
                audit.append("%s тратит %s, а в реестре стоит на %s: реестр устарел, перевод не предлагается"
                             % (_shown(w), src, reg_w["account"]))
                continue
            movable.append((max(a, b), w))
        if not movable:
            continue
        movable.sort(key=lambda x: (-x[0], x[1].casefold(), x[1]))
        w = movable[0][1]
        a, b = sh[w]
        lo, hi = _half_up(base * min(a, b)), _half_up(base * max(a, b))
        tr = fresh[tgt]
        plan = registry["accounts"][src]["plan"]
        s_txt = "окно %s%%" % pct_s if pct_s is not None else "окно не открыто"
        t_txt = ("окно %s%%" % tr.get("session_pct")) if _num(tr.get("session_pct")) is not None else "окно не открыто"
        text = ("ПРЕДЛОЖЕНИЕ: перевести %s с %s на %s. %s: %s / неделя %s%% (сброс %s UTC)%s, %s ~%d–%d%% %s. "
                "%s: %s / неделя %s%%. Перевод = перезапуск консоли %s с CLAUDE_CONFIG_DIR=%s — делает владелец."
                % (w, src, tgt, src, s_txt, r.get("week_pct"), _hm(resets),
                   "" if plan != "?" else ", тариф неизвестен", w, lo, hi, scope,
                   tgt, t_txt, tr.get("week_pct"), w, registry["accounts"][tgt]["config_dir"]))
        used.add(tgt)
        out.append(Proposal(w, src, tgt, text, "%s>%s@%d" % (src, tgt, int(resets))))
    return out


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _is_link(p):
    if os.path.islink(p):
        return True
    isj = getattr(os.path, "isjunction", None)          # Python 3.12+
    if isj is not None and isj(p):
        return True
    try:
        return os.path.normcase(os.path.realpath(p)) != os.path.normcase(os.path.abspath(p)) and \
            os.path.normcase(os.path.realpath(os.path.dirname(p))) == os.path.normcase(os.path.abspath(os.path.dirname(p)))
    except OSError:
        return True


def _transcripts(config_dir, since):
    """*.jsonl under <config_dir>/projects, never through a link: a link can lead to a login file or a private folder.
    The projects folder ITSELF may be a link (two accounts sharing one folder): that one is resolved once, on purpose."""
    root = os.path.realpath(os.path.join(config_dir, "projects"))
    if not os.path.isdir(root):
        return
    for dp, dirs, fs in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not _is_link(os.path.join(dp, d))
                   and _inside(os.path.realpath(os.path.join(dp, d)), root)]
        for f in fs:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(dp, f)
            if _is_link(p) or not _inside(os.path.realpath(p), root):
                continue
            try:
                if os.path.getmtime(p) < since:
                    continue
            except OSError:
                continue
            yield p


def _session_owner(path, root, by_session):
    """a subagent transcript (<session>/subagents/[workflows/<wf>/]agent-*.jsonl) spends its parent session's limit:
    the nearest ancestor folder named like a session that has a name wins"""
    stop = os.path.normcase(root)
    d = os.path.dirname(path)
    while os.path.normcase(d) != stop and os.path.dirname(d) != d:
        n = by_session.get(os.path.basename(d))
        if n:
            return n
        d = os.path.dirname(d)
    return None


def _read_transcript(path, start):
    """(name, usage rows at or after `start`); older rows are dropped while reading, so memory follows the window"""
    name, rows = None, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            r = parse_transcript_line(line)
            if r is None:
                continue
            if r["kind"] == "agent":
                name = name or r["name"]
                continue
            ts = _epoch(r["timestamp"])
            if ts is not None and ts >= start:
                rows.append(r)
    return name, rows


def collect_tokens(reg, now):
    """token rows of every account's transcripts: (rows, audit). Rows carry "account" when the folder tells it."""
    audit = []
    # skipping old files by mtime is only a speed-up (each row's timestamp decides); mtime is wall clock, so it is
    # compared with the wall clock too, never with a `now` from somewhere else
    since = min(now, time.time()) - (reg["window_h"] + 24) * 3600
    row_start = now - max(reg["window_h"] * 3600, 7 * 86400)
    token_rows = []
    # two config dirs may point at ONE projects folder (a junction or symlink): read it once; the folder then cannot
    # tell which account spent, so its rows carry no account and follow the registry (never counted on both)
    groups, dir_of = {}, {}
    for acc_name, acc in reg["accounts"].items():
        for cd in acc.get("config_dirs") or [acc["config_dir"]]:     # every folder of the account (one login, several folders)
            real = os.path.normcase(os.path.realpath(os.path.join(cd, "projects")))
            if acc_name not in groups.setdefault(real, []):
                groups[real].append(acc_name)
            dir_of.setdefault(real, cd)
    for real, accs in groups.items():
        cd = dir_of[real]
        shared = len(accs) > 1
        if shared:
            audit.append("папка projects общая у %s: расход по реестру, не по папке" % ", ".join(accs))
        root = os.path.realpath(os.path.join(cd, "projects"))
        files = list(_transcripts(cd, since))
        names, parsed = {}, {}
        for p in files:
            names[p], parsed[p] = _read_transcript(p, row_start)
        by_session = {os.path.splitext(os.path.basename(p))[0]: n for p, n in names.items() if n}
        nameless = []
        for p in files:
            n = names[p] or _session_owner(p, root, by_session)
            if n is None:
                if parsed[p]:
                    nameless.append(p)
                continue
            for r in parsed[p]:
                row = dict(r["usage"], worker=n, id=r["id"], ts=r["timestamp"])
                if not shared:
                    row["account"] = accs[0]
                token_rows.append(row)
        for p in nameless[:5]:
            audit.append("ничей транскрипт: %s" % os.path.basename(p))     # file name only: folders carry project names
        if len(nameless) > 5:
            audit.append("ничей транскрипт: ещё %d в %s" % (len(nameless) - 5, "/".join(accs)))
    return token_rows, audit


def _read_usage_log(path):
    rows = []
    with open(path, "rb") as f:
        data = f.read().decode("utf-8-sig", errors="replace")
    for line in data.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict):
            rows.append(o)
    return rows


def run_once(control_dir, now):
    """read the registry, the usage log and the transcripts; return (proposals, audit lines). Remembers proposals in
    control/accounts_state.json so one pair is proposed once per window."""
    from .accounts_build import rebuild          # the registry is rebuilt from the launchers on every run, if configured
    audit = rebuild(control_dir)
    reg = load_registry(os.path.join(control_dir, "accounts.json"))
    lp = os.path.join(control_dir, "usage_log.jsonl")
    usage_rows = _read_usage_log(lp) if os.path.exists(lp) else []
    token_rows, more = collect_tokens(reg, now)
    audit.extend(more)
    props = propose(reg, usage_rows, token_rows, now, audit)
    return _remember(control_dir, props, audit), audit


def _remember(control_dir, props, audit):
    """one proposal per (source, target, window): {pair_key: epoch proposed} in control/accounts_state.json;
    the oldest keys are trimmed first, a broken file starts over with an audit line"""
    sp = os.path.join(control_dir, STATE_FILE)
    proposed = {}
    if os.path.exists(sp):
        try:
            state = _read_json_file(sp)
        except ValueError:
            state = None
        if not isinstance(state, dict):
            audit.append("состояние предложений нечитаемо: начато заново")
            state = {}
        got = state.get("proposed", {})
        if isinstance(got, list):                       # the first format: a plain list of keys
            got = {k: 0.0 for k in got if isinstance(k, str)}
        if isinstance(got, dict):
            proposed = {k: v for k, v in got.items() if isinstance(k, str) and _num(v) is not None}
    new = [p for p in props if p.pair_key not in proposed]
    if new:
        stamp = time.time()
        for p in new:
            proposed[p.pair_key] = stamp
        keep = sorted(proposed.items(), key=lambda kv: (kv[1], kv[0]))[-STATE_KEEP:]
        tmp = "%s.%d.tmp" % (sp, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"proposed": dict(keep)}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, sp)
    return new
