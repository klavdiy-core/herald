"""Read a kcore journal (organ 1): what the next safe action is and what the last record says, without writing a byte.

    python -m herald mem journal status --root <journal root> --kcore <folder that holds the kcore package>
    (or HERALD_JOURNAL_ROOT / HERALD_KCORE; with neither, it refuses - it never falls back to a live root)

The verdict is kcore's own `recover.status`, called as a library, so it cannot drift from `python -m kcore status`. One
difference, on purpose: kcore's status takes an exclusive lock that creates state/.events.lock; here that lock is replaced
by nothing, so the journal root and kcore's outside anchor stay untouched. Without the lock a writer may be mid-line: a torn
last line is read exactly once more; damage anywhere else is reported at once.
"""
import contextlib
import importlib
import json
import os
import sys


class JournalUnavailable(RuntimeError):
    """No journal root given, no kcore at the given place, or no journal in the root."""


def _kcore(kcore_dir):
    if not kcore_dir or not os.path.isdir(os.path.join(kcore_dir, "kcore")):
        raise JournalUnavailable("no kcore package under %r (pass --kcore or HERALD_KCORE)" % (kcore_dir,))
    want = os.path.normcase(os.path.realpath(os.path.join(kcore_dir, "kcore")))
    loaded = sys.modules.get("kcore")
    if loaded is not None and os.path.normcase(os.path.realpath(os.path.dirname(loaded.__file__))) != want:
        raise JournalUnavailable("another kcore is already loaded in this process: %s" % loaded.__file__)
    if kcore_dir not in sys.path:
        sys.path.insert(0, kcore_dir)
    eventlog = importlib.import_module("kcore.eventlog")
    recover = importlib.import_module("kcore.recover")
    if os.path.normcase(os.path.realpath(os.path.dirname(eventlog.__file__))) != want:
        raise JournalUnavailable("kcore was loaded from %s, not from %s" % (eventlog.__file__, kcore_dir))
    return eventlog, recover


@contextlib.contextmanager
def _no_lock(eventlog):
    real = eventlog.file_lock
    eventlog.file_lock = lambda _path, *a, **k: contextlib.nullcontext()     # reading takes no lock: no file is created
    try:
        yield
    finally:
        eventlog.file_lock = real


def _codes(result):
    return {e.get("code") for e in (result.get("log") or {}).get("errors", [])}


def status(root, kcore_dir, _between_passes=None):
    """kcore's verdict on `root`, read without a lock. Raises JournalUnavailable when there is nothing to read."""
    if not root:
        raise JournalUnavailable("no journal root given (pass --root or HERALD_JOURNAL_ROOT)")
    if not os.path.isfile(os.path.join(root, "state", "events.jsonl")):
        raise JournalUnavailable("no journal at %s (state/events.jsonl is missing)" % root)
    eventlog, recover = _kcore(kcore_dir)
    with _no_lock(eventlog):
        result = recover.status(root)
        if _codes(result) == {"torn_tail"}:      # a writer mid-line, perhaps: one more pass, exactly one
            if _between_passes is not None:
                _between_passes()
            result = recover.status(root)
        last = None
        if result.get("ok") or not _codes(result):
            with contextlib.suppress(Exception):
                events = eventlog.EventLog(root).read()
                if events:
                    last = {k: events[-1].get(k) for k in ("seq", "type", "task_id", "timestamp")}
    log = result.get("log") or {}
    return {"ok": bool(result.get("ok")), "next_safe_action": result.get("next_safe_action"),
            "events": log.get("events"), "head_hash": log.get("head_hash"), "errors": log.get("errors", []),
            "last": last, "root": str(root)}


def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="herald mem journal status")
    ap.add_argument("what", choices=["journal"])
    ap.add_argument("verb", choices=["status"])
    ap.add_argument("--root", default=os.environ.get("HERALD_JOURNAL_ROOT", ""))
    ap.add_argument("--kcore", default=os.environ.get("HERALD_KCORE", ""))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        s = status(a.root, a.kcore)
    except JournalUnavailable as err:
        print("JOURNAL UNAVAILABLE: %s" % err)
        return 7
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        print("%s  next safe action: %s" % ("JOURNAL OK  " if s["ok"] else "JOURNAL NOT OK", s["next_safe_action"]))
        print("last record: %s events, head %s" % (s["events"], s["head_hash"]))
        if s["last"]:
            print("             %s" % json.dumps(s["last"], ensure_ascii=False))
        for e in s["errors"]:
            print("error: %s" % json.dumps(e, ensure_ascii=False))
    return 0 if s["ok"] else 4
