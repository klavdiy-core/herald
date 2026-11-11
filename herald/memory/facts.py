"""Organ 6: the facts file as an adapter into the one memory store (interface agreed in team review, 11.11.2026).

The source is an append-only facts file written by its own tool: below a marker line, one line = one fact,
    - [YYYY-MM-DD HH:MM] (her|us|me) <fact> · src=<pointer> · #<sha8>
where sha8 = the first 8 hex of sha256 of the line without its " · #..." tail, and the time is local at a fixed offset.
Marks inside the fact: a check mark = the person's own words, a "~" (not followed by a digit) = an inference.

One line -> one evidence (the line's bytes) -> one semantic record:
    check mark AND src starting "msg:" -> user_message, canonical, authority quoted_words (her words, with the message)
    "~"                                -> model_inference, hypothesis, authority inference
    check mark without a msg: src      -> assistant_asserted, hypothesis, authority quoted_unsourced
    no mark                            -> assistant_asserted, hypothesis, authority fact_file
No line becomes canonical without a link to its message. (her) lines are highly_private, (us)/(me) private; main-only.
The archive above the marker has no ids: it is only counted, never imported, never printed.
The file is only read; no path to any live file is in this code (--file is required).

python -m herald mem facts import --file F --store S --subject X   one line per fact, "archive lines n", last "IMPORTED a SKIPPED b REFUSED c"
python -m herald mem facts check  --file F --store S               MISSING/EXTRA/BODY_MISMATCH/BAD_LINE/ERASED, last "CHECK ..."
python -m herald mem facts tail N --store S                         "<id>\\t<date time>\\t<tag>", newest first (no text)
Exit 0 ok, 4 a refused line or a difference, 7 nothing to work on ("FACTS UNAVAILABLE: ...", no ids, no text, no traceback)."""
import argparse
import hashlib
import os
import re
import sys

MARK = "## ЖИВЫЕ ФАКТЫ (append-only, с 19.08.2026)"
OFFSET = "+03:00"                   # the facts tool writes local time at this fixed offset: the one place it is named
TICK = "✔"
TILDE = re.compile(r"~(?!\s*\d)")    # "~ an inference" is a mark; "~21:55" is an approximate time, not one
LINE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\] \((her|us|me)\) (.+)$")
SOURCE = "facts:"
PREDICATE = "stated_fact"
ACTOR = {"id": "ENT-herald-facts", "role": "system"}     # the adapter observes the line; the fact is the author's


class Unavailable(Exception):
    pass


def _line_hash(raw):
    return hashlib.sha256(raw).hexdigest()[:12]


def read(path):
    """(facts, bad line hashes, archive line count, mismatched ids). Nothing is opened for writing."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise Unavailable("the facts file cannot be read") from None
    lines = data.split(b"\n")
    try:
        start = next(i for i, raw in enumerate(lines) if raw.rstrip(b"\r").decode("utf-8", "replace") == MARK)
    except StopIteration:
        return [], [], len([x for x in lines if x.strip()]), []
    archive = len([x for x in lines[:start] if x.strip()])
    facts, bad, mismatched, seen = [], [], [], set()
    for raw in lines[start + 1:]:
        raw = raw.rstrip(b"\r")
        if not raw.strip() or raw.startswith(b">"):     # the marker's own explanation line, blank lines
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(_line_hash(raw))
            continue
        body, sep, sha8 = text.rpartition(" · #")
        m = LINE_RE.match(body) if sep else None
        if not m or not re.fullmatch(r"[0-9a-f]{8}", sha8) or " · src=" not in m.group(4):
            bad.append(_line_hash(raw))
            continue
        if hashlib.sha256(body.encode("utf-8")).hexdigest()[:8] != sha8:
            mismatched.append(sha8)                     # a silent edit after the line was written
            continue
        if sha8 in seen:
            continue                                    # the same line twice is one fact
        seen.add(sha8)
        fact, _, src = m.group(4).rpartition(" · src=")
        facts.append({"id": sha8, "body": body, "data": raw, "date": m.group(1), "time": m.group(2),     # the whole line
                      "tag": m.group(3), "text": fact, "src": src})
    return facts, bad, archive, mismatched


def classify(fact):
    """(authorship_channel, status, authority). Conservative: an inference mark wins over a check mark."""
    if TILDE.search(fact["text"]):
        return "model_inference", "hypothesis", "inference"
    if TICK in fact["text"]:
        if fact["src"].strip().startswith("msg:"):
            return "user_message", "canonical", "quoted_words"
        return "assistant_asserted", "hypothesis", "quoted_unsourced"
    return "assistant_asserted", "hypothesis", "fact_file"


def memory_id_of(fact):
    """Derived from the WHOLE line (8 hex of the line's own id are too few for an erasure key)."""
    return "MEM-" + hashlib.sha256((SOURCE + fact["body"]).encode("utf-8")).hexdigest()[:16]


def record(fact, evidence_id, subject):
    channel, status, authority = classify(fact)
    return {"memory_id": memory_id_of(fact), "record_type": "semantic", "subject_id": subject, "predicate": PREDICATE,
            "object": {"fact_id": fact["id"], "tag": fact["tag"], "text": fact["text"], "src": fact["src"]},
            "status": status, "authority": authority, "authorship_channel": channel, "actor": dict(ACTOR),
            "valid_time": {"from": "%sT%s:00%s" % (fact["date"], fact["time"], OFFSET), "to": None},
            "evidence_ids": [evidence_id], "sensitivity": "highly_private" if fact["tag"] == "her" else "private",
            "retention_policy": "until_corrected_or_erased", "resurfacing_policy": "when_relevant"}


def held(store):
    """{fact id} for the facts evidence that a live (not tombstoned) record cites."""
    rows = store.db.execute("SELECT e.source FROM evidence e JOIN evidence_refs r ON r.evidence_id = e.id "
                            "JOIN records rec ON rec.memory_id = r.memory_id "
                            "WHERE e.source LIKE ? AND rec.tombstoned = 0", (SOURCE + "%",)).fetchall()
    return {source[len(SOURCE):] for (source,) in rows}


def import_file(store, path, emit, subject):
    from . import PolicyRefused
    facts, bad, archive, mismatched = read(path)
    counts = {"imported": 0, "skipped": 0, "refused": len(bad) + len(mismatched), "archive": archive}
    for h in bad:
        emit(h, "bad_line")
    for fact_id in mismatched:
        emit(fact_id, "body_mismatch")
    have = held(store)
    for fact in facts:
        if store.erased_id(memory_id_of(fact), reader_scope="main"):      # before any evidence is written
            emit(fact["id"], "erased")      # main's own decision: said, never a refusal that keeps the run red
            continue
        if fact["id"] in have:
            emit(fact["id"], "skipped")
            counts["skipped"] += 1
            continue
        evidence_id = store.put_evidence(fact["data"], SOURCE + fact["id"], "assistant_asserted", dict(ACTOR),
                                         reader_scope="main")
        memory_id = store.add(record(fact, evidence_id, subject), "facts import", reader_scope="main")
        if store.get(memory_id, reader_scope="main") is None:              # read-after-write
            raise PolicyRefused("read-after-write failed for an imported fact")
        have.add(fact["id"])
        emit(fact["id"], "imported")
        counts["imported"] += 1
    return counts


def check_file(store, path, emit):
    facts, bad, archive, mismatched = read(path)
    disk, unsourced = set(), 0
    for h in bad:
        emit("BAD_LINE", h)
    for fact_id in mismatched:
        emit("BODY_MISMATCH", fact_id)
    for fact in facts:
        if store.erased_id(memory_id_of(fact), reader_scope="main"):
            emit("ERASED", fact["id"])
            continue
        disk.add(fact["id"])
        unsourced += classify(fact)[2] == "quoted_unsourced"
    have = held(store)
    differences = len(bad) + len(mismatched)
    for fact_id in sorted(disk - have):
        emit("MISSING", fact_id)
        differences += 1
    for fact_id in sorted(have - disk):
        emit("EXTRA", fact_id)
        differences += 1
    tail = "CHECK disk %d store %d archive %d" % (len(disk), len(have), archive)
    for word, n in (("bad_line", len(bad)), ("body_mismatch", len(mismatched)), ("quoted_unsourced", unsourced)):
        if n:
            tail += " %s %d" % (word, n)
    return tail, differences


def tail(store, n):
    rows = []
    for (memory_id,) in store.db.execute("SELECT memory_id FROM records WHERE tombstoned = 0").fetchall():
        doc = store.get(memory_id, reader_scope="main")
        if doc and doc["predicate"] == PREDICATE and doc["actor"].get("id") == ACTOR["id"]:
            rows.append(doc)
    rows.sort(key=lambda d: (d["valid_time"]["from"], d["object"]["fact_id"]), reverse=True)
    return [(d["object"]["fact_id"], d["valid_time"]["from"][:16].replace("T", " "), d["object"]["tag"]) for d in rows[:n]]


def cli(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="herald mem facts", add_help=False)
    parser.add_argument("command", choices=["import", "check", "tail"])
    parser.add_argument("n", nargs="?", type=int, default=20)
    parser.add_argument("--file")
    parser.add_argument("--store")
    parser.add_argument("--subject")
    parser.error = lambda message: (_ for _ in ()).throw(Unavailable("bad arguments"))
    try:
        args = parser.parse_args(argv)
        if not args.store:
            raise Unavailable("no store")
        if args.command == "import" and not (args.subject or "").strip():
            raise Unavailable("no --subject")
        if args.command != "import" and not os.path.isfile(os.path.join(args.store, "memory.sqlite")):
            raise Unavailable("no store")
        if args.command != "tail":
            if not args.file or not os.path.isfile(args.file):
                raise Unavailable("no facts file")
            facts, bad, _, mismatched = read(args.file)
            if not (facts or bad or mismatched):
                raise Unavailable("no fact under the marker")
        return _run(args, out)
    except Unavailable as why:
        print("FACTS UNAVAILABLE: %s" % why, file=out)
        return 7


def _run(args, out):
    from . import ReadOnlyStore, Store, StoreNeedsRepair

    def emit(a, b):
        print("%s\t%s" % (a, b), file=out, flush=True)

    if args.command == "import":
        store = Store(args.store)
        try:
            c = import_file(store, args.file, emit, args.subject)
        finally:
            store.close()
        print("archive lines %d" % c["archive"], file=out)
        print("IMPORTED %d SKIPPED %d REFUSED %d" % (c["imported"], c["skipped"], c["refused"]), file=out)
        return 4 if c["refused"] else 0
    try:
        store = ReadOnlyStore(args.store)
    except StoreNeedsRepair:
        raise Unavailable("the store needs a repair; open it once with herald first") from None
    try:
        if args.command == "check":
            line, differences = check_file(store, args.file, lambda kind, x: print("%s %s" % (kind, x), file=out, flush=True))
            print(line, file=out)
            return 4 if differences else 0
        for fact_id, when, tag in tail(store, max(1, args.n)):
            print("%s\t%s\t%s" % (fact_id, when, tag), file=out)
        return 0
    finally:
        store.close()
