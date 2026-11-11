"""Organ 2: the diary as an adapter into the one memory store (interface agreed in team review, 11.11.2026).

One diary file -> one evidence (the file's bytes, whole) -> one episodic record. The record states an OBSERVATION: "a file
with these bytes exists under this id, written at T" (authorship_channel system_observed, authority diary_file, canonical).
The prose inside stays the author's own assertion in the evidence and is never lifted into canonical facts.

The diary tree is only read (opened "rb"); the format is the one diary.py writes and checks - diary.py is the oracle:
the FILE NAME is the identity (an id: line in the frontmatter is ignored), body_sha256 = sha256(body.strip("\\n")), an entry
without frontmatter or without body_sha256 is legacy, an adopted entry has no written_at (its date comes from the name).
Everything is main-only: ids carry a slug of the title, so import/check/last print only in the main console.

python -m herald mem diary import --root R --store S --subject ENT-x   (--subject required)
                                                       one line per entry, last "IMPORTED a SKIPPED b REFUSED c LEGACY d"
python -m herald mem diary check  --root R --store S   MISSING/EXTRA/CHANGED lines, last "CHECK disk n store m legacy l"
python -m herald mem diary last N --store S            "<id>\\t<date>\\t<title>", newest first
Exit 0 ok, 4 a refused entry or a difference, 7 nothing to work on ("DIARY UNAVAILABLE: ...", no ids, no traceback).
11.11.2026."""
import argparse
import datetime
import hashlib
import os
import re
import sys

ID_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-\d{4}(?:\d{2})?--[a-z0-9-]+$")      # diary.py ID_RE: v1 4-digit time, now 6
INDEX = "INDEX.md"
SOURCE = "diary:"
PREDICATE = "wrote_diary_entry"
ACTOR = {"id": "ENT-herald-diary", "role": "system"}       # the adapter observes the file; the prose is the author's


class Unavailable(Exception):
    pass


class BadEntry(Exception):
    pass


def _path_hash(root, path):
    rel = os.path.relpath(path, root).replace("\\", "/")
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


def walk(root):
    """(every *.md under root except INDEX.md, folders that could not be listed), in a stable order. A folder that cannot
    be listed is said (control round wf_5a6010fe), never skipped in silence. Nothing is opened for writing."""
    found, unlisted = [], []
    for base, dirs, names in os.walk(root, onerror=lambda err: unlisted.append(err.filename)):
        dirs.sort()
        for name in sorted(names):
            if name.endswith(".md") and name != INDEX:
                found.append(os.path.join(base, name))
    return found, unlisted


def parse(root, path):
    """One entry as diary.py reads it. BadEntry only for what diary.py itself could not hold as an entry; None for a
    companion file (an undated name and no frontmatter, like COVERS.md), which diary.py does not count as an entry."""
    stem = os.path.basename(path)[:-3]
    m = ID_RE.match(stem)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise BadEntry("unreadable") from None          # never a traceback: it would carry the path and the slug
    if not m and not data.startswith(b"---"):
        return None
    rel_dir = os.path.relpath(os.path.dirname(path), root).replace("\\", "/")
    if not m or rel_dir != "%s/%s" % (m.group(1), m.group(2)):
        raise BadEntry("name")
    try:
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")     # as diary.py's read_text sees it
    except UnicodeDecodeError:
        raise BadEntry("encoding") from None
    meta, body, has_frontmatter = {}, text, text.startswith("---")
    if has_frontmatter:
        end = text.find("\n---", 3)
        if end <= 0:
            raise BadEntry("frontmatter")
        for line in text[3:end].splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                meta[key.strip()] = [x.strip() for x in value[1:-1].split(",") if x.strip()]
            else:
                meta[key.strip()] = value
        body = text[end + 4:]
    written = meta.get("written_at")
    if written:
        try:
            moment = datetime.datetime.fromisoformat(str(written).replace("Z", "+00:00"))
        except ValueError:
            raise BadEntry("written_at") from None
        if moment.tzinfo is None:
            raise BadEntry("written_at")
        valid_from = moment.date().isoformat()      # the day as it was lived, in written_at's own offset
    else:
        valid_from = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))     # adopted / v1: the name carries the day
    declared = meta.get("body_sha256")
    legacy = not has_frontmatter or not declared
    if not legacy and hashlib.sha256(body.strip("\n").encode("utf-8")).hexdigest() != declared:
        return {"id": stem, "refused": "body_mismatch", "sha": hashlib.sha256(data).hexdigest()}
    obj = {"entry_id": stem, "title": meta.get("title") or stem, "tags": meta.get("tags") or [],
           "supersedes": meta.get("supersedes") or None, "legacy": legacy}
    if isinstance(obj["tags"], str):
        obj["tags"] = [obj["tags"]]
    if meta.get("feeling"):
        obj["feeling"] = meta["feeling"]
    return {"id": stem, "data": data, "sha": hashlib.sha256(data).hexdigest(), "object": obj, "valid_from": valid_from,
            "legacy": legacy}


def parse_quiet(root, path):
    """True when the file is an entry or tried to be one (a companion file is neither)."""
    try:
        return parse(root, path) is not None
    except BadEntry:
        return True


def held(store):
    """{entry id: [file sha, ...]} for the diary evidence that a live (not tombstoned) record cites."""
    out = {}
    rows = store.db.execute("SELECT e.source, e.sha256 FROM evidence e JOIN evidence_refs r ON r.evidence_id = e.id "
                            "JOIN records rec ON rec.memory_id = r.memory_id "
                            "WHERE e.source LIKE ? AND rec.tombstoned = 0", (SOURCE + "%",)).fetchall()
    for source, sha in rows:
        out.setdefault(source[len(SOURCE):], []).append(sha)
    return out


def memory_id_of(entry_id):
    """One memory_id per diary id, derived: the store never gives an erased id out again (ErasedIdReused), so an erasure
    holds for this entry whatever later happens to its title, tags or frontmatter (control round wf_5a6010fe)."""
    return "MEM-" + hashlib.sha256((SOURCE + entry_id).encode("utf-8")).hexdigest()[:16]


def record(entry, evidence_id, subject):
    return {"memory_id": memory_id_of(entry["id"]),
            "record_type": "episodic", "subject_id": subject, "predicate": PREDICATE, "object": entry["object"],
            "status": "canonical", "authority": "diary_file", "authorship_channel": "system_observed",
            "actor": dict(ACTOR), "valid_time": {"from": entry["valid_from"], "to": None},
            "evidence_ids": [evidence_id], "sensitivity": "private",
            "retention_policy": "until_corrected_or_erased", "resurfacing_policy": "when_relevant"}


def import_tree(store, root, emit, subject):
    """Returns (imported, skipped, refused, legacy). The store is the caller's, opened as main."""
    from . import PolicyRefused
    counts = {"imported": 0, "skipped": 0, "refused": 0, "legacy": 0}
    have = held(store)
    paths, unlisted = walk(root)
    for folder in unlisted:
        emit(_path_hash(root, folder), "bad_entry")
        counts["refused"] += 1
    for path in paths:
        try:
            entry = parse(root, path)
        except BadEntry:
            emit(_path_hash(root, path), "bad_entry")
            counts["refused"] += 1
            continue
        if entry is None:               # a companion file: not an entry, as in diary.py
            continue
        if store.erased_id(memory_id_of(entry["id"]), reader_scope="main"):    # before any evidence is written
            emit(entry["id"], "erased")
            counts["refused"] += 1
            continue
        known = have.get(entry["id"], [])
        if entry["sha"] in known:
            emit(entry["id"], "skipped")
            counts["skipped"] += 1
            continue
        if known:                       # write-once broken on disk: the store keeps what it holds, nothing is overwritten;
            emit(entry["id"], "changed")    # said before body_mismatch, which an edit of a held entry also causes
            counts["refused"] += 1
            continue
        if "refused" in entry:
            emit(entry["id"], entry["refused"])
            counts["refused"] += 1
            continue
        evidence_id = store.put_evidence(entry["data"], SOURCE + entry["id"], "assistant_asserted", dict(ACTOR),
                                         reader_scope="main")
        memory_id = store.add(record(entry, evidence_id, subject), "diary import", reader_scope="main")
        if store.get(memory_id, reader_scope="main") is None:      # read-after-write (canon 16.1)
            raise PolicyRefused("read-after-write failed for an imported diary entry")
        have.setdefault(entry["id"], []).append(entry["sha"])
        emit(entry["id"], "imported")
        counts["imported"] += 1
        counts["legacy"] += entry["legacy"]
    return counts


def check_tree(store, root, emit):
    """The equality test: the set of (id, file sha) on disk == in the store. Returns (disk, store, legacy, differences)."""
    disk, legacy, differences, mismatched, bad = {}, 0, 0, set(), 0
    paths, unlisted = walk(root)
    for folder in unlisted:
        emit("BAD_ENTRY", _path_hash(root, folder))
        bad += 1                                    # counted in the bad_entry tail too (review of d4bb999)
        differences += 1
    for path in paths:
        try:
            entry = parse(root, path)
        except BadEntry:
            emit("BAD_ENTRY", _path_hash(root, path))   # a file the store cannot hold is a divergence, not silence
            bad += 1
            differences += 1
            continue
        if entry is None:
            continue
        if store.erased_id(memory_id_of(entry["id"]), reader_scope="main"):
            emit("ERASED", entry["id"])     # main's own decision: said, never a difference that keeps check red
            continue
        disk[entry["id"]] = entry["sha"]
        legacy += entry.get("legacy", False)
        if "refused" in entry:
            mismatched.add(entry["id"])
    have = held(store)
    mismatch_only = mismatched - set(have)                  # a held entry edited on disk is CHANGED, said below
    for entry_id in sorted(mismatch_only):
        emit("BODY_MISMATCH", entry_id)                     # counted apart and not in disk n
        differences += 1
        del disk[entry_id]
    for entry_id in sorted(disk):
        if entry_id not in have:
            emit("MISSING", entry_id)
            differences += 1
        elif disk[entry_id] not in have[entry_id]:
            emit("CHANGED", entry_id)
            differences += 1
    for entry_id in sorted(set(have) - set(disk)):
        emit("EXTRA", entry_id)
        differences += 1
    return len(disk), sum(len(v) for v in have.values()), legacy, differences, bad, len(mismatch_only)


def last(store, n):
    rows = []
    for (memory_id,) in store.db.execute("SELECT memory_id FROM records WHERE tombstoned = 0").fetchall():
        doc = store.get(memory_id, reader_scope="main")
        if doc and doc["predicate"] == PREDICATE and doc["actor"].get("id") == ACTOR["id"]:
            rows.append(doc)
    rows.sort(key=lambda d: (d["object"]["entry_id"]), reverse=True)        # the id starts with the date and time
    return [(d["object"]["entry_id"], d["valid_time"]["from"][:10], d["object"].get("title", "")) for d in rows[:n]]


def cli(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="herald mem diary", add_help=False)
    parser.add_argument("command", choices=["import", "check", "last"])
    parser.add_argument("n", nargs="?", type=int, default=10)
    parser.add_argument("--root")
    parser.add_argument("--store")
    parser.add_argument("--subject")     # who wrote the diary: required, named by the caller, never by the code
    parser.error = lambda message: (_ for _ in ()).throw(Unavailable("bad arguments"))
    try:
        args = parser.parse_args(argv)
        if not args.store:
            raise Unavailable("no store")
        if args.command == "import" and not (args.subject or "").strip():
            raise Unavailable("no --subject")    # canonical records: a silent default would split from organs 3 and 6
        if args.command != "import" and not os.path.isfile(os.path.join(args.store, "memory.sqlite")):
            raise Unavailable("no store")       # check and last only read: they never create one; import makes its own
        if args.command != "last":
            if not args.root or not os.path.isdir(args.root):
                raise Unavailable("no diary root")
            if not any(parse_quiet(args.root, p) for p in walk(args.root)[0]):
                raise Unavailable("no diary entry under the root")      # a zero is not a green: nothing was looked at
        return _run(args, out)
    except Unavailable as why:
        print("DIARY UNAVAILABLE: %s" % why, file=out)
        return 7


def _run(args, out):
    from . import ReadOnlyStore, Store, StoreNeedsRepair

    def emit(a, b):
        print("%s\t%s" % (a, b), file=out, flush=True)

    if args.command == "import":
        store = Store(args.store)
        try:
            c = import_tree(store, args.root, emit, args.subject)
        finally:
            store.close()
        print("IMPORTED %d SKIPPED %d REFUSED %d LEGACY %d" % (c["imported"], c["skipped"], c["refused"], c["legacy"]),
              file=out)
        return 4 if c["refused"] else 0
    try:
        store = ReadOnlyStore(args.store)
    except StoreNeedsRepair:
        raise Unavailable("the store needs a repair; open it once with herald first") from None
    try:
        if args.command == "check":
            disk, held_n, legacy, differences, bad, mism = check_tree(
                store, args.root, lambda kind, x: print("%s %s" % (kind, x), file=out, flush=True))
            print("CHECK disk %d store %d legacy %d" % (disk, held_n, legacy) + (" bad_entry %d" % bad if bad else "")
                  + (" body_mismatch %d" % mism if mism else ""), file=out)
            return 4 if differences else 0
        for entry_id, day, title in last(store, max(1, args.n)):
            print("%s\t%s\t%s" % (entry_id, day, title), file=out)
        return 0
    finally:
        store.close()
