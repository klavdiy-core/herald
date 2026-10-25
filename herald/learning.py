"""Self-learning from a Claude Code transcript: what repeated, what fixed it, what the human corrected.

The idea is Hermes Agent's (Nous Research, MIT): after work, turn experience into skills and memory. Here it is done
by reading the transcript, without a model: deterministic, checkable, and nothing is written by itself.

  analyze(path)  -> {"repeated_errors", "recoveries", "corrections"}
  propose(report, private=False) -> proposals: {"kind": "skill" | "memory", "title", "evidence", "draft"}
      private=True keeps the mechanics and never copies the human's own words (for transcripts with personal content).
  render_md(proposals) -> a Markdown page to read and approve by hand.
"""
import json
import os
import re
from collections import OrderedDict

EXIT_LINE = re.compile(r"^Exit code (\d+)$", re.M)
ERROR_LINE = re.compile(r"error|exception|denied|not found|no such|cannot|can't|failed|reserved|invalid|refused|"
                        r"timed out|ошибк|не найден|не удается|не удаётся|отказано", re.I)
CORRECTION = re.compile(
    r"^\s*(no\b|nope\b|wrong\b|not that\b|that's not\b|нет\b|не так\b|не то\b|неправильно\b)"   # "stop, ..." is an order
    r"|\b(again|you forgot|i said|i told you|still (wrong|broken)|опять|снова|ты забыл|я же (сказала?|говорила?)|"
    r"ещё раз|не работает|сломал)"
    # a plain observation that the claim did not hold ("тесты не запустились", found missing on a real session)
    r"|\bне\s+(запусти\w*|сработа\w*|получил\w*|работа\w*|видно|появил\w*|пришл\w*|открыл\w*|отправил\w*)"
    r"|\bничего не (произошло|пришло|появилось)|\bdid(n't| not) (work|run|start|arrive)|\b(doesn't|does not) work"
    r"|\bisn't (working|running)|\bnothing happened", re.I)
# a failing test fixed by editing code is the job, not a lesson: test runs are kept out of learning
TEST_RUN = re.compile(r"\b(pytest|unittest|jest|vitest|mocha|go test|cargo test|npm (run )?test|node --test|"
                      r"tox|nox|rspec|phpunit|dotnet test|ctest)\b|tests?[/\\]test_\w+\.py|test_\w+\.py", re.I)
RECOVERY_WINDOW = 6
# a failure the agent expected (a red test run on purpose, a planted break) is method, not a mistake
EXPECTED = re.compile(r"\bred\b|expect\w* (red|fail)|mutant|mutation|planted|красн|должен упасть", re.I)
MAX_CORRECTION_CHARS = 500
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# words a human writes with a capital that are not private: tool names and words error lines are made of (masking
# them turned "String to replace not found in file" into "... in <typed>" on a real team run)
NOT_PRIVATE = {"read", "write", "edit", "bash", "powershell", "grep", "glob", "file", "files", "folder", "string",
               "error", "errors", "python", "node", "claude", "code", "test", "tests", "since", "found", "replace",
               "line", "path", "true", "false", "none", "null", "json", "http", "https", "html", "readme", "telegram",
               "github", "windows", "linux", "chrome", "this", "that", "then", "when", "what", "where", "with",
               "from", "into", "have", "make", "done", "same", "again", "after", "before"}
SKIP = re.compile(r"-SkipGate\b|--no-verify\b|--skip[-\w]*|--no-gpg-sign\b|\bskip[_-]?gate\b", re.I)
INLINE = re.compile(r"(?:^|\s)-[ce]\s+[\"']")         # python -c "...", node -e '...'
GENERIC = {"error", "errors", "failed", "module", "named", "found", "file", "line", "true", "false", "exception",
           "string", "replace", "tool_use_error", "traceback", "cannot", "invalid", "detected", "most", "recent",
           "call", "last", "exit", "code"}
WITHHELD = "(output without an error line)"
WRITES = re.compile(r"<<|(?<![0-9&])>>?(?!&)\s*[\w./\\$\"']|Set-Content|Out-File|Add-Content|\btee\b|"
                    r"(Write|Append)All(Text|Lines|Bytes)|Copy-Item|Move-Item|\bcp\s|\bmv\s", re.I)


def _identity(tool, inp):
    """What 'the same step again' means: program + first argument for a shell, tool + path for file tools."""
    if tool in ("Bash", "PowerShell"):
        words = str(inp.get("command", "")).split()
        return f"{tool}:{' '.join(words[:2])}"
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
    return f"{tool}:{path}" if path else f"{tool}:{_head(inp)}"


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _head(inp, n=120):
    if not isinstance(inp, dict):
        return str(inp)[:n]
    for k in ("command", "file_path", "path", "pattern", "url", "query", "prompt"):
        if k in inp:
            return str(inp[k]).strip().splitlines()[0][:n] if str(inp[k]).strip() else ""
    return json.dumps(inp, ensure_ascii=False)[:n]


def _failed(block, text):
    if block.get("is_error") is True:
        return True
    return any(int(m) != 0 for m in EXIT_LINE.findall(text))


def signature(text):
    """One normalized line that names the error: the traceback's last line, else the first line that sounds like an
    error, else the first line; paths, numbers and ids removed so the same mistake in two places matches."""
    lines = [l.strip() for l in str(text).splitlines() if l.strip() and not EXIT_LINE.match(l.strip())]
    if not lines:
        return ""
    if any(l.startswith("Traceback (most recent call last)") for l in lines):
        line = lines[-1]
    else:
        line = next((l for l in lines if ERROR_LINE.search(l)), lines[0])
    line = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/(?:[\w.-]+/)+[\w.-]+", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", line)
    # line 3 -> N; digits glued to letters stay (jinja2, a typed ALPHA47 that private mode must still recognise)
    line = re.sub(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)*(?![A-Za-z_])", "N", line)
    return re.sub(r"\s+", " ", line)[:160]


def _private_words(text):
    """Words in a human message that could be a secret or a name: with a digit, ALL CAPS, CamelCase, or capitalized
    inside a sentence. Plain lowercase words are left alone (masking them garbled every error line on a real run)."""
    out, prev = set(), "."
    for m in re.finditer(r"[^\W_]+|[^\w\s]", text):
        w = m.group(0)
        if len(w) >= 4 and w[0].isalnum() and w.lower() not in NOT_PRIVATE:
            if (any(c.isdigit() for c in w) or (w.isupper() and w.isalpha()) or any(c.isupper() for c in w[1:])
                    or (w[0].isupper() and prev not in ".!?:")):
                out.add(w)
        if not w[0].isspace():
            prev = w if not w[0].isalnum() else "w"
    return out


def _events(path):
    calls, out = {}, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            # injected skill / command text and the summary written after a compaction are not the human
            if not isinstance(rec, dict) or rec.get("isMeta") or rec.get("isCompactSummary"):
                continue
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}   # malformed records are skipped
            content, ts = msg.get("content"), str(rec.get("timestamp", ""))
            att = rec.get("attachment") if rec.get("type") == "attachment" else None
            if (isinstance(att, dict) and att.get("type") == "queued_command" and str(att.get("prompt", "")).strip()
                    and not str(att["prompt"]).lstrip().startswith("<")):     # <task-notification> etc. are system
                # a message the human sent mid-turn: Claude Code stores it as an attachment, not as a user record
                out.append({"kind": "human", "text": str(att["prompt"]), "ts": ts})
                continue
            if rec.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        inp = b.get("input")
                        calls[b.get("id")] = (str(b.get("name")), inp if isinstance(inp, dict) else {"value": str(inp)})
                    elif b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                        out.append({"kind": "say", "text": b["text"], "ts": ts})
            elif rec.get("type") == "user":
                if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            name, inp = calls.get(b.get("tool_use_id"), ("?", {}))
                            t = _text(b.get("content"))
                            out.append({"kind": "call", "tool": name, "input": inp, "ok": not _failed(b, t),
                                        "text": t, "ts": ts,
                                        "expected": bool(EXPECTED.search(str(inp.get("description", ""))))
                                        or (name in ("Bash", "PowerShell")
                                            and bool(TEST_RUN.search(str(inp.get("command", "")))))})
                else:
                    t = _text(content)
                    if t.strip() and not t.lstrip().startswith("<"):        # skip injected system blocks
                        out.append({"kind": "human", "text": t, "ts": ts})
    return out


def analyze(path, ignore=None, min_repeat=2):
    """ignore: a regex for outputs that are signals, not failures (a tool that exits 1 to say 'unread messages')."""
    ev = _events(path)
    calls = [e for e in ev if e["kind"] == "call"]
    if ignore:
        rx = re.compile(ignore)
        for c in calls:
            if not c["ok"] and rx.search(c["text"]):
                c["expected"] = True
    groups = OrderedDict()
    for c in calls:
        if not c["ok"] and not c["expected"]:
            s = signature(c["text"])
            g = groups.setdefault(s, {"signature": s, "count": 0, "tools": [], "first": c["ts"], "last": c["ts"],
                                      "examples": []})
            g["count"] += 1
            g["last"] = c["ts"]
            if c["tool"] not in g["tools"]:
                g["tools"].append(c["tool"])
            if len(g["examples"]) < 3:
                g["examples"].append(_head(c["input"]))
    repeated = [g for g in groups.values() if g["count"] >= min_repeat and g["signature"]]

    # a recovery: step X failed, later the same step X succeeded -> what happened in between is the candidate fix.
    # Nothing in between = a transient failure (Hermes: do not capture). Other shapes: inline code (-c) that worked
    # from a file; another tool that succeeded on the same file. Every candidate is then judged: a cause, a
    # coincidence (a neighbouring step that touched nothing the failure was about) or a bypass (going around a check).
    recoveries, not_lessons, seen = [], [], set()
    session = os.path.splitext(os.path.basename(path))[0]
    for i, c in enumerate(calls):
        if c["ok"] or c["expected"]:
            continue
        ident, head = _identity(c["tool"], c["input"]), _head(c["input"], 400)
        window = calls[i + 1:i + 1 + RECOVERY_WINDOW]
        fix, how = None, None
        for k, nxt in enumerate(window):
            if nxt["ok"] and _identity(nxt["tool"], nxt["input"]) == ident:
                if SKIP.search(_head(nxt["input"], 400)) and not SKIP.search(head):
                    fix, how = nxt, "skip"            # the same step passed only because its check was switched off
                    break
                between = [b for b in window[:k] if b["ok"]]
                fix, how = (between[0] if between else None), "between"
                if fix and fix["tool"] in EDITS:      # fail, edit the code, pass: that is the job, not a lesson
                    fix = None
                break
        else:
            prog = _program(c)
            if prog and INLINE.search(head):          # inline code broke in the shell; the same program ran a file
                fix = next((n for n in window if n["ok"] and _program(n) == prog
                            and not INLINE.search(_head(n["input"], 400))), None)
                how = "changed"
            if fix is None:
                tokens = _files(head)
                fix = next((n for n in window if n["ok"] and n["tool"] != c["tool"]
                            and tokens & _files(_head(n["input"], 400))), None)
                how = "instead"
                # an edit after a failed run is fixing the code; "instead" only if the shell was writing that file
                if fix and fix["tool"] in EDITS and not WRITES.search(head):
                    fix = None
        if fix is None:
            continue
        sig = signature(c["text"])
        why, right = _judge(c, fix, how, sig)
        entry = {"signature": sig, "failed_tool": c["tool"], "failed_input": _head(c["input"]),
                 "fixed_by": fix["tool"], "fix_input": _head(fix["input"]), "ts": fix["ts"], "session": session}
        key = (sig, fix["tool"], _head(fix["input"]))
        if why != "cause":
            not_lessons.append(dict(entry, why=why, right_move=right))
        elif key not in seen:
            seen.add(key)
            recoveries.append(entry)

    corrections, last_say = [], None
    for e in ev:
        if e["kind"] == "say":
            last_say = e
        elif e["kind"] == "human":
            short = len(e["text"].strip()) <= MAX_CORRECTION_CHARS        # a long brief is not a correction
            if last_say is not None and short and CORRECTION.search(e["text"]):
                corrections.append({"claim": last_say["text"].strip()[:300], "correction": e["text"].strip()[:300],
                                    "ts": e["ts"]})
            last_say = None
    # words the human typed that could be private: in private mode they are masked wherever they turn up
    typed = sorted({w.lower() for e in ev if e["kind"] == "human" for w in _private_words(e["text"])})
    for g in repeated:
        g["sessions"] = [session]
    return {"repeated_errors": repeated, "recoveries": recoveries, "corrections": corrections, "typed": typed,
            "not_lessons": not_lessons, "calls": len(calls), "failed_calls": sum(1 for c in calls if not c["ok"])}


def analyze_many(paths, ignore=None):
    """One report over several sessions; each lesson knows in how many different sessions it happened."""
    groups, out = OrderedDict(), {"recoveries": [], "corrections": [], "not_lessons": [], "typed": set(),
                                  "calls": 0, "failed_calls": 0}
    for p in paths:
        r = analyze(p, ignore, min_repeat=1)
        for g in r["repeated_errors"]:
            m = groups.setdefault(g["signature"], dict(g, count=0, tools=[], sessions=[]))
            m["count"] += g["count"]
            m["tools"] = sorted(set(m["tools"]) | set(g["tools"]))
            m["sessions"] = sorted(set(m["sessions"]) | set(g["sessions"]))
            m["first"], m["last"] = min(m["first"], g["first"]), max(m["last"], g["last"])
        for k in ("recoveries", "corrections", "not_lessons"):
            out[k] += r[k]
        out["typed"] |= set(r["typed"])
        out["calls"] += r["calls"]
        out["failed_calls"] += r["failed_calls"]
    out["repeated_errors"] = [g for g in groups.values() if g["count"] >= 2]
    out["typed"] = sorted(out["typed"])
    return out


def _program(call):
    """The program a shell call runs (python, node, git...), else None."""
    if call["tool"] not in ("Bash", "PowerShell"):
        return None
    words = str(call["input"].get("command", "")).replace("&", " ").split()
    return os.path.basename(words[0]).lower().removesuffix(".exe") if words else None


def _files(text):
    return {m.lower() for m in re.findall(r"[\w.-]+\.\w{1,5}", text)}


def _judge(failed, fix, how, sig):
    """('cause' | 'coincidence' | 'bypass', the right move when it is not a cause)."""
    fix_in = _head(fix["input"], 400)
    if how == "skip" or (SKIP.search(fix_in) and not SKIP.search(_head(failed["input"], 400))):
        return "bypass", "fix what the check reports; never switch the check off"
    if failed["tool"] in EDITS and fix["tool"] in ("Bash", "PowerShell") and how == "instead" \
            and _action(fix) == "write":                  # the shell rewrote the file around Edit's check
        return "bypass", "Read the file again (it changed since it was read), then Edit"
    if how == "changed":
        return "cause", ""
    shared = bool(_files(_head(failed["input"], 400)) & _files(fix_in))
    words = {w.lower() for w in re.findall(r"[A-Za-z][\w-]{3,}", sig)} - GENERIC
    about_error = any(w in fix_in.lower() for w in words)
    act_failed, act_fix = _action(failed), _action(fix)
    if how == "instead":
        # another tool did the same thing to the same file (a heredoc kept failing, Write wrote it): a cause.
        # Another tool merely looking at the file is a neighbouring step.
        if shared and act_failed == act_fix:
            return "cause", ""
    elif act_fix in ("read", "write", "install") and (shared or about_error):
        return "cause", ""                    # read the file again, install the missing module, ...
    return "coincidence", "find what really changed; a neighbouring step is not a fix"


READS = re.compile(r"^\s*(cat|head|tail|less|more|type|get-content|gc|select-string|sls|grep|rg|ls|dir|"
                   r"get-childitem|test-path|stat|wc)\b", re.I)
INSTALLS = re.compile(r"\b(pip3?|uv|npm|pnpm|yarn|choco|winget|apt(-get)?|brew)\s+(install|add|i)\b", re.I)


def _action(call):
    """What a step does: write, read, install or run."""
    if call["tool"] in EDITS:
        return "write"
    if call["tool"] in ("Read", "Grep", "Glob"):
        return "read"
    cmd = str(call["input"].get("command", ""))
    last = re.split(r"[;&|]+", cmd)[-1] if cmd else ""
    if INSTALLS.search(cmd):
        return "install"
    if WRITES.search(cmd):
        return "write"
    if READS.search(last) or READS.search(cmd):
        return "read"
    return "run"


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "pattern"


def redact(sig, private=True):
    """In private mode an output line that does not read as an error may be someone's words: say only its shape."""
    return sig if not private or ERROR_LINE.search(sig) else WITHHELD


def _short(inp_head, private):
    """In private mode a command loses its quoted parts (a typed message rides there) and is cut to a few words."""
    if not private:
        return inp_head
    bare = re.sub(r"\"[^\"]*\"|'[^']*'", '"..."', inp_head)
    return " ".join(bare.split()[:6])[:80]


def _scrub(s, typed):
    """Mask every word the human typed (private mode)."""
    if not typed:
        return s
    rx = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(map(re.escape, typed)) + r")(?![A-Za-z0-9])", re.I)
    return rx.sub("<typed>", s)


def _across(recoveries, n):
    """One recovery per (error, fix) that happened in at least n different sessions."""
    groups = OrderedDict()
    for r in recoveries:
        g = groups.setdefault((r["signature"], r["fixed_by"], r["fix_input"]), {"first": r, "sessions": set()})
        g["sessions"].add(r.get("session"))
    return [g["first"] for g in groups.values() if len(g["sessions"]) >= n]


def propose(report, private=False, min_sessions=1):
    if min_sessions > 1:                  # a lesson is what keeps happening, not what happened once
        report = dict(report, recoveries=_across(report["recoveries"], min_sessions),
                      repeated_errors=[g for g in report["repeated_errors"]
                                       if len(g.get("sessions", [])) >= min_sessions])
    if private:
        # a withheld output teaches nothing readable: it is dropped, not shown as a blank lesson
        # and every word the human typed is masked: an error line or a command can echo it
        typed = report.get("typed", [])
        sig = lambda s: _scrub(redact(s), typed)                                       # noqa: E731
        report = dict(report,
                      repeated_errors=[dict(g, signature=sig(g["signature"])) for g in report["repeated_errors"]
                                       if redact(g["signature"]) != WITHHELD],
                      recoveries=[dict(r, signature=sig(r["signature"]),
                                       fix_input=_scrub(_short(r["fix_input"], True), typed),
                                       failed_input=_scrub(_short(r["failed_input"], True), typed))
                                  for r in report["recoveries"] if redact(r["signature"]) != WITHHELD])
    props = []
    fixes = {}
    for r in report["recoveries"]:
        fixes.setdefault(r["signature"], r)
    # one class-level umbrella skill per tool (Hermes: never a skill named after one error string)
    umbrellas = OrderedDict()
    for g in report["repeated_errors"]:
        fix = fixes.get(g["signature"])
        evidence = f"seen {g['count']} times ({', '.join(g['tools'])}), {g['first']} .. {g['last']}"
        if fix:
            tool = g["tools"][0]
            umbrellas.setdefault(tool, []).append((g, fix, evidence))
        else:
            props.append({"kind": "memory", "title": f"Recurring error: {g['signature']}", "evidence": evidence,
                          "draft": f"- Recurring ({g['count']}x): `{g['signature']}`. No fix found in the session; "
                                   f"find the root cause before running {', '.join(g['tools'])} again."})
    for tool, items in umbrellas.items():
        name = f"{_slug(tool)}-pitfalls"
        sections = "".join(
            f"\n## `{g['signature']}`\n\nSeen {g['count']} times. It went away after {_did(fix)}. "
            f"Do that before retrying `{fix['failed_input']}`.\n" for g, fix, _ in items)
        draft = (f"---\nname: {name}\ndescription: Known {tool} pitfalls from real sessions and what fixed each. "
                 f"Use before running {tool} on a step that failed before.\n---\n\n# {tool} pitfalls\n" + sections)
        props.append({"kind": "skill", "title": f"{tool} pitfalls ({len(items)})",
                      "evidence": "; ".join(e for _, _, e in items), "draft": draft})
    repeated = {g["signature"] for g in report["repeated_errors"]}
    for r in report["recoveries"]:
        if r["signature"] in repeated or r["fixed_by"] == r["failed_tool"]:
            continue
        props.append({"kind": "memory", "title": f"Use {r['fixed_by']} instead of {r['failed_tool']} here",
                      "evidence": f"{r['failed_tool']} failed with `{r['signature']}`, then {r['fixed_by']} worked",
                      "draft": f"- When {r['failed_tool']} fails with `{r['signature']}`: {r['fixed_by']} "
                               f"(`{r['fix_input']}`) worked instead."})
    # the same correction is one lesson however often it came (three sessions used to give three proposals);
    # in private mode every draft reads the same, so all corrections are one proposal
    groups = OrderedDict()
    for c in report["corrections"]:
        key = "private" if private else " ".join(c["correction"].lower().split())[:200]
        groups.setdefault(key, []).append(c)
    for cs in groups.values():
        c, n, when = cs[0], len(cs), ", ".join(x["ts"] for x in cs)
        if private:
            # neither side's words: the assistant's claim often quotes the human
            draft = (f"- A claim was corrected by the human ({when}). Re-read that exchange and write the rule in "
                     f"your own words; the check that would have caught it belongs at the point of action.")
        else:
            draft = f"- Claimed: \"{c['claim'][:160]}\" -> corrected: \"{c['correction'][:200]}\""
        title = "A claim the human corrected" + (f" ({n} times)" if n > 1 else "")
        props.append({"kind": "memory", "title": title, "evidence": when, "draft": draft})
    return props


def _did(fix):
    """What the fix was, as an action: a Read fix used to read 'after `/w/app.ts` (via Read)'."""
    tool, what = fix["fixed_by"], fix["fix_input"]
    if tool == "Read":
        return f"reading `{what}` again"
    if tool in ("Write", "Edit", "MultiEdit"):
        return f"writing `{what}` with {tool}"
    if tool in ("Bash", "PowerShell"):
        return f"running `{what}`"
    return f"`{what}` (via {tool})"


def shown_not_lessons(report, private=False):
    """The coincidences and bypasses, safe to show: in private mode error lines and commands are masked as in
    proposals, and an output without an error line is dropped."""
    typed = report.get("typed", []) if private else []
    return [dict(n, signature=_scrub(redact(n["signature"], private), typed),
                 fix_input=_scrub(_short(n["fix_input"], private), typed))
            for n in report.get("not_lessons", []) if redact(n["signature"], private) != WITHHELD]


def render_md(props, source="", not_lessons=None):
    out = ["# Learning proposals", ""]
    if source:
        out += [f"From: {source}. Nothing here is applied by itself: read, then approve or drop.", ""]
    for i, p in enumerate(props, 1):
        out += [f"## {i}. [{p['kind']}] {p['title']}", "", f"Evidence: {p['evidence']}", "", "```", p["draft"], "```", ""]
    if not props:
        out.append("Nothing repeated, nothing was corrected.")
    if not_lessons:
        out += ["", "## Not lessons", "", "Looked like a fix, but was not one; kept here so nobody learns it by mistake.", ""]
        seen = set()
        for n in not_lessons:
            key = (n["signature"], n["why"], n["fixed_by"])
            if key not in seen:
                seen.add(key)
                out.append(f"- **{n['why']}**: after `{n['signature'][:90]}`, {n['fixed_by']} `{n['fix_input'][:60]}` "
                           f"only followed it. Right move: {n['right_move']}.")
    return "\n".join(out) + "\n"
