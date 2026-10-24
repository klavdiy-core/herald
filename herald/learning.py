"""Self-learning from a Claude Code transcript: what repeated, what fixed it, what the human corrected.

The idea is Hermes Agent's (Nous Research, MIT): after work, turn experience into skills and memory. Here it is done
by reading the transcript, without a model: deterministic, checkable, and nothing is written by itself.

  analyze(path)  -> {"repeated_errors", "recoveries", "corrections"}
  propose(report, private=False) -> proposals: {"kind": "skill" | "memory", "title", "evidence", "draft"}
      private=True keeps the mechanics and never copies the human's own words (for transcripts with personal content).
  render_md(proposals) -> a Markdown page to read and approve by hand.
"""
import json
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
WITHHELD = "(output without an error line)"
WRITES = re.compile(r"<<|(?<![0-9&])>>?(?!&)\s*[\w./\\$\"']|Set-Content|Out-File|Add-Content|\btee\b|WriteAllText", re.I)


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
        if len(w) >= 4 and w[0].isalnum():
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

    # a recovery: step X failed, later the same step X succeeded -> what happened in between is the fix.
    # Nothing in between = a transient failure (Hermes: do not capture). Fallback: another tool succeeded on the same
    # file (a heredoc kept failing, the Write tool did it).
    recoveries, seen = [], set()
    for i, c in enumerate(calls):
        if c["ok"] or c["expected"]:
            continue
        ident = _identity(c["tool"], c["input"])
        window = calls[i + 1:i + 1 + RECOVERY_WINDOW]
        fix = None
        for k, nxt in enumerate(window):
            if nxt["ok"] and _identity(nxt["tool"], nxt["input"]) == ident:
                between = [b for b in window[:k] if b["ok"]]
                fix = between[0] if between else None
                if fix and fix["tool"] in EDITS:      # fail, edit the code, pass: that is the job, not a lesson
                    fix = None
                break
        else:
            tokens = set(re.findall(r"[\w.-]+\.\w{1,5}", _head(c["input"], 400)))
            fix = next((n for n in window if n["ok"] and n["tool"] != c["tool"]
                        and tokens & set(re.findall(r"[\w.-]+\.\w{1,5}", _head(n["input"], 400)))), None)
            # an edit after a failed run is fixing the code; it is "instead" only if the shell was writing that file
            if fix and fix["tool"] in EDITS and not WRITES.search(_head(c["input"], 400)):
                fix = None
        if fix is None:
            continue
        key = (signature(c["text"]), fix["tool"], _head(fix["input"]))
        if key not in seen:
            seen.add(key)
            recoveries.append({"signature": key[0], "failed_tool": c["tool"], "failed_input": _head(c["input"]),
                               "fixed_by": fix["tool"], "fix_input": _head(fix["input"]), "ts": fix["ts"]})

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
    return {"repeated_errors": repeated, "recoveries": recoveries, "corrections": corrections, "typed": typed,
            "calls": len(calls), "failed_calls": sum(1 for c in calls if not c["ok"])}


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


def propose(report, private=False):
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
            f"\n## `{g['signature']}`\n\nSeen {g['count']} times. It went away after `{fix['fix_input']}` "
            f"(via {fix['fixed_by']}). Do that before retrying `{fix['failed_input']}`.\n" for g, fix, _ in items)
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
    for c in report["corrections"]:
        if private:
            # neither side's words: the assistant's claim often quotes the human
            draft = (f"- A claim was corrected by the human ({c['ts']}). Re-read that exchange and write the rule in "
                     f"your own words; the check that would have caught it belongs at the point of action.")
        else:
            draft = f"- Claimed: \"{c['claim'][:160]}\" -> corrected: \"{c['correction'][:200]}\""
        props.append({"kind": "memory", "title": "A claim the human corrected", "evidence": c["ts"], "draft": draft})
    return props


def render_md(props, source=""):
    out = ["# Learning proposals", ""]
    if source:
        out += [f"From: {source}. Nothing here is applied by itself: read, then approve or drop.", ""]
    for i, p in enumerate(props, 1):
        out += [f"## {i}. [{p['kind']}] {p['title']}", "", f"Evidence: {p['evidence']}", "", "```", p["draft"], "```", ""]
    if not props:
        out.append("Nothing repeated, nothing was corrected.")
    return "\n".join(out) + "\n"
