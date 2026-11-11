"""Golden-set runner: `python -m herald mem golden --set <JSONL> (--store <root> | --recall-db <index>) [--k 10]`.

A set line is {"id": str, "query": str, "expect": [str, ...]}. The set may be private: its queries and expectations never
leave this process. stdout holds only `<id>\\t<pass|fail|unknown>\\t<rank|->` rows and a last `TOTAL <n> PASS <m>`; an error
names a row id and an error class, never a message (a message can carry the query), and no traceback is ever printed.

Nothing is written anywhere. --store opens a ReadOnlyStore (mode=ro): a store that needs a repair, a rebuild or a journal
sync is refused with exit 7, never repaired - open it once with herald first. A hit is a memory_id of the answer in expect.
--recall-db opens the index with mode=ro, so nothing in it is ever created, dropped or rebuilt; an index from before scopes
(no messages.source) is refused (exit 7). Its source fingerprint is not checked: the main scope reads every row. expect holds
FULL paths of session files (absolute, else a broken line), compared whole (normcase(realpath)) with the source of each hit.
An empty expect is the zero class (reviewer M-22224): no abstention exists before organ 3, so it is not searched; the row is
unknown and the total line adds " ZERO_UNCHECKED z" when z > 0.
Exit: 0 the run reached its end (fails included), 4 a broken set line (whole set checked before any query), 7 nothing to run,
1 interrupted (a fixed line, no traceback).
author 11.11.2026, interface reviewer M-21993, red tests test author (organ1/test_golden_runner.py), control round wf_f1a3eafa."""
import argparse
import json
import os
import re
import sqlite3
import sys

ID = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")     # an id is printed: it must never be able to carry a query
BOM = b"\xef\xbb\xbf"


class Unavailable(Exception):
    pass


class BadLine(Exception):
    def __init__(self, where, kind):
        super().__init__(where, kind)
        self.where, self.kind = where, kind


def read_set(path, branch):
    """Every line checked before any query runs: a broken set prints no row at all."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise Unavailable("the set file cannot be read") from None
    if data.startswith(BOM):
        data = data[len(BOM):]
    rows, seen = [], set()
    for number, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        row = None
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            pass
        if row is None:
            raise BadLine("line %d" % number, "not_json")       # outside the except: no chained message about the text
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not ID.fullmatch(row["id"]):
            raise BadLine("line %d" % number, "bad_id")
        rid = row["id"]
        if rid in seen:
            raise BadLine(rid, "duplicate_id")
        seen.add(rid)
        query = row.get("query")
        if not isinstance(query, str) or not any(c.isalnum() for c in query) or not _encodable(query):
            raise BadLine(rid, "bad_query")                     # a query no search can match is a mistake in the set
        expect = row.get("expect")     # [] is the zero class (reviewer M-22224): kept, never searched
        if not isinstance(expect, list) or not all(isinstance(e, str) and e and _encodable(e) for e in expect):
            raise BadLine(rid, "bad_expect")
        if branch == "recall" and not all(os.path.isabs(e) for e in expect):
            raise BadLine(rid, "bad_expect")                    # a relative path would be read against the current folder
        rows.append((rid, query, expect))
    if not rows:
        raise Unavailable("the set holds no line")
    return rows


def _encodable(text):
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _rank(found, expected):
    for position, key in enumerate(found, 1):
        if key in expected:
            return position
    return None


def run_store(root, rows, k, emit, warn):
    from .memory import ReadOnlyStore, StoreNeedsRepair
    failed = None                       # a missing store is refused by ReadOnlyStore itself, which never creates one
    try:
        store = ReadOnlyStore(root)
    except StoreNeedsRepair:
        failed = "the store needs a repair; open it once with herald first"
    except Exception as err:            # noqa: the class only; the message is never printed
        failed = "the store does not open (%s)" % type(err).__name__
    if failed:
        raise Unavailable(failed)
    try:
        for rid, query, expect in rows:
            if not expect:
                emit(rid, "zero", None)
                continue
            answer, error = None, None
            try:
                answer = store.search(query, reader_scope="main", limit=k)
            except Exception as err:    # noqa: recorded here, printed after the except block (no chained traceback)
                error = type(err).__name__
            if error is not None:
                warn(rid, error)
                emit(rid, "unknown", None)
            elif answer["outcome"] == "unknown":
                emit(rid, "unknown", None)
            else:
                emit(rid, *_verdict(_rank([h["memory_id"] for h in answer["hits"]], set(expect))))
    finally:
        store.close()


def _path_key(path):
    return os.path.normcase(os.path.realpath(path))


def run_recall(db, rows, k, emit, warn):
    from . import recall
    if not os.path.isfile(db):
        raise Unavailable("no index at the given path")
    failed = None
    try:
        recall._connect_read_only(db).close()
    except sqlite3.Error:
        failed = "the index is not a current recall index; rebuild it with herald recall first"
    if failed:
        raise Unavailable(failed)
    for rid, query, expect in rows:
        if not expect:
            emit(rid, "zero", None)
            continue
        hits, error = None, None
        try:
            hits = recall.search(db, query, limit=k, reader_scope="main", with_source=True, read_only=True)
        except Exception as err:        # noqa: recorded here, printed after the except block
            error = type(err).__name__
        if error is not None:
            warn(rid, error)
            emit(rid, "unknown", None)
        else:
            emit(rid, *_verdict(_rank([_path_key(h["source"]) for h in hits], {_path_key(e) for e in expect})))


def _verdict(rank):
    return ("pass", rank) if rank is not None else ("fail", None)


def main(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    try:
        return _main(argv, out, err)
    except (KeyboardInterrupt, BrokenPipeError, OSError):
        pass                            # never a traceback: it would carry the exception chain of a row
    try:
        print("GOLDEN INTERRUPTED", file=err)
    except Exception:                   # noqa: stderr itself may be gone
        pass
    return 1


def _main(argv, out, err):
    parser = argparse.ArgumentParser(prog="herald mem golden", add_help=False)
    parser.add_argument("--set", dest="set_path")
    parser.add_argument("--store")
    parser.add_argument("--recall-db")
    parser.add_argument("--k", type=int, default=10)
    parser.error = lambda message: (_ for _ in ()).throw(Unavailable("bad arguments"))     # argparse would echo argv
    try:
        args = parser.parse_args(argv)
        if not args.set_path:
            raise Unavailable("no --set")
        if bool(args.store) == bool(args.recall_db):
            raise Unavailable("give exactly one of --store / --recall-db")
        if not 1 <= args.k <= 1000:
            raise Unavailable("--k must be between 1 and 1000")
        rows = read_set(args.set_path, "store" if args.store else "recall")
        total = {"n": 0, "pass": 0, "zero": 0}

        def emit(rid, outcome, rank):
            total["n"] += 1
            total["pass"] += outcome == "pass"
            if outcome == "zero":       # no abstention before organ 3: a zero row is unknown and counted apart
                total["zero"] += 1
                outcome = "unknown"
            print("%s\t%s\t%s" % (rid, outcome, rank if rank is not None else "-"), file=out, flush=True)

        def warn(rid, kind):
            print("%s\t%s" % (rid, kind), file=err, flush=True)

        if args.store:
            run_store(args.store, rows, args.k, emit, warn)
        else:
            run_recall(args.recall_db, rows, args.k, emit, warn)
    except Unavailable as why:
        print("GOLDEN UNAVAILABLE: %s" % why, file=out)
        return 7
    except BadLine as bad:
        print("%s\t%s" % (bad.where, bad.kind), file=err)
        return 4
    print("TOTAL %d PASS %d" % (total["n"], total["pass"])
          + (" ZERO_UNCHECKED %d" % total["zero"] if total["zero"] else ""), file=out)
    return 0
