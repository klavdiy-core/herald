"""The durable core of long-term memory.

One record schema; an immutable content-addressed evidence archive; versions that are never overwritten; hash-chained
event logs; lexical indexes that can always be rebuilt from the records. SQLite only, no server, no model.

Two scopes. A worker sees only what is its own, and every answer it gets is the same whether or not private data exists:
a private id answers like an id that never existed, and worker events live in a chain of their own. The main scope sees
everything; `all_events` gives it the one total order across both chains (by `global_seq`, which no worker ever sees).

An event's hash covers a commitment (an HMAC with a per-event secret) to its reason and document hashes. Erasing a record
deletes those secrets and payloads: the chain still verifies, and what remains commits to nothing that can be guessed.

What it does not do. `reader_scope` is declared by the caller: this guards against mistakes, not a hostile process, and
anyone who can open these files can read them. Erasure removes the bytes from this folder; it cannot reach backups,
snapshots or a disk's unallocated blocks. Timing is not covered. Erasing from the private scope cannot remove text a
worker already knows and writes again: the worker's record stays ordinary, and the main scope sees an `erased_returned`
event on the erased record. Bytes a worker put in as evidence stay until a worker-scope record that cited them is erased,
even if a private record cited them too: removing them would tell the worker a private record had, and the bytes are what
the worker itself wrote and has already seen; there is no call that erases a worker's evidence directly. The ledger and
`erase` say so (`done_worker_bytes_remain`), never a bare done. A blob with no row (a cut put, or a database older than the
archive) waits in quarantine/ for QUARANTINE_DAYS; a cut put of private bytes has no record, so no erasure reaches it there.
Nothing in this module puts a blob back: that is a manual step. Duplicate search, conflict search and independent review (the rest of promotion) are the
caller's layer.

Known limits (reviewed, not fixed):
- Erasures survive a restore through <root>.erasures.jsonl beside the folder. Restoring the folder TOGETHER with that
  journal (or a backup of both) is not healed: erasures made after the copy come back. Erasures made while the journal
  was gone wait in <root>.erasures.pending.jsonl, also beside the folder: restoring the PARENT folder rolls back the
  journal and the pending file with it, and that is not healed either.
- Lines at the very end of the journal that are not journal lines at all, past what the base applied, are taken as an
  unfinished write and cut (verify(main) says so); junk followed by a whole line is corruption and the store does not
  open. A whole last line without its newline is an intent and is kept.
- Price, measured on the dev machine: an ordinary open reads only the last line (10 ms with 1 000 erasures, 45 ms with
  100 000 tombstones). A full pass after a restore: 1 000 erasures applied again 0.4-0.5 s; 100 000 lines for records
  the copy never held 8.3 s (SQLite inserts grow with the table). A full pass over 1 s is logged with its time.
- Evidence a journal line names is dropped only the first time that line meets this base; a line the base applied before
  leaves alone the evidence on disk now, since main may have put the same bytes again (the same source gives the same id).
- A base written before id marks kept their birth (ff047ad) is migrated on open: the birth comes from the journal line
  with that id; a mark no line names keeps an empty birth, and verify(main) counts such marks. The migration trusts the
  journal lines as they are (no chain check); what it cannot read stays "main" and the id stays refused.
- `repair_erasure_journal` rebuilds from what the base holds (ledger, id marks, tombstones) plus the pending file;
  erasures known only to the lost journal are gone. Their evidence bytes are not listed for ledger-rebuilt lines.
- An ordinary open reads only the journal's last line: a line damaged in the middle while the last one still matches
  the anchor is not seen there. `verify_journal()` (main) checks every line.
- `erased_returned` events and the tail of the main chain written after the copy are lost with the restore; applying
  the erasures again does not bring them back. A tombstone brought back for a record the copy never held logs no
  `erased_returned` when its text comes again.
- A restored quarantine/ can bring an erased blob back with no evidence row: nothing reaches it. Do not restore
  quarantine/ from a copy.
- The `erase_reapplied` event keeps no link to its journal line: like every erasure, it drops its payload.
- Procedure: close every process of the store, restore, delete a stale memory.sqlite-journal beside it, open as main,
  read `store.reapplied` and `verify(main)`.
- A private record whose history cites evidence that a later worker erasure removed keeps a dangling evidence id in an
  old version; `verify` does not flag it. The worker row's fate never depends on private records.
- quarantine/ expiry takes no write lock: two opens racing between the age check and the remove can delete a freshly set
  aside row-less blob before its term. Only bytes with no row and no issued id are affected; reproduced only with a
  forced interleaving.
- Not checked: true power loss, a full disk.
"""
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets
import sqlite3
import tempfile
import time
import unicodedata

SCHEMA_VERSION = 2

RECORD_TYPES = frozenset({"episodic", "semantic", "procedural", "preference", "identity", "relationship", "project",
                          "decision", "task/promise", "failure", "source/archive", "governance", "working"})
STATUSES = frozenset({"canonical", "hypothesis", "archive", "conflicted", "pending", "verified_failed", "superseded",
                      "retracted", "quarantined", "tombstoned"})
STATUSES_BY_OPERATION_ONLY = frozenset({"superseded", "tombstoned"})
MODEL_CHANNELS = frozenset({"model_inference", "assistant_asserted", "derived"})
ASSERTION_CHANNELS = frozenset({"user_message", "user_asserted", "jointly_confirmed"})
CHANNELS = MODEL_CHANNELS | ASSERTION_CHANNELS | frozenset({
    "system_observed", "externally_sourced", "user_document", "tool_output", "external_source", "system_event"})
TEAM_LABELS = frozenset({"public", "internal"})
MAIN_LABELS = frozenset({"private", "highly_private", "third_party_restricted"})
SENSITIVITIES = TEAM_LABELS | MAIN_LABELS | {"secret"}
READERS = frozenset({"worker", "main"})

REQUIRED = ("record_type", "subject_id", "predicate", "object", "status", "authority", "authorship_channel", "actor",
            "valid_time", "evidence_ids", "sensitivity", "retention_policy", "resurfacing_policy")
OPTIONAL = {"memory_id": None, "derivation": None, "confidence": None, "conflicts": [], "aliases": [],
            "embedding_version": None, "index_state": "current"}
SET_BY_STORE = ("version", "system_time", "supersedes")
ERASURE_CLASSES = ("records", "versions", "archive", "lexical_worker", "lexical_main")
LEXICAL = {"worker": "lexical_worker.sqlite", "main": "lexical_main.sqlite"}
INDEX_ERRORS = (sqlite3.Error, OSError)
SALT_FILE = "tombstone.salt"
CREATED_FILE = "store.created"
QUARANTINE_DAYS = 7
READ_ERRORS = (ValueError, KeyError, TypeError, AttributeError, RecursionError, sqlite3.Error)
KEPT_PENDING, KEPT_DONE = "pending_worker_bytes_remain", "done_worker_bytes_remain"
JOURNAL_SUFFIX = ".erasures.jsonl"      # beside the store folder, not inside it: restoring the folder does not roll it back
BORN_FILE = "erasures.born"             # beside store.created: the store has had a journal since this mark
PENDING_SUFFIX = ".erasures.pending.jsonl"      # erasures made while there was no journal; beside the folder too
JOURNAL_SLOW_SECONDS = 1.0
JOURNAL_FIELDS = ("id", "birth", "keys", "scope")
STORE_ACTOR = {"id": "ENT-store", "role": "system"}
ERASE_KINDS = ("erase", "erase_reapplied")
LOG = logging.getLogger("herald.memory")

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (memory_id TEXT PRIMARY KEY, current_version INTEGER NOT NULL,
                                    tombstoned INTEGER NOT NULL DEFAULT 0, scope TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (memory_id TEXT NOT NULL, version INTEGER NOT NULL, doc TEXT NOT NULL,
                                     closed_at TEXT, closed_valid_to TEXT, PRIMARY KEY (memory_id, version));
CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, source TEXT NOT NULL,
                                     channel TEXT NOT NULL, actor_id TEXT NOT NULL, actor_role TEXT NOT NULL,
                                     span TEXT, scope TEXT NOT NULL, mac TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_refs (evidence_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                                          PRIMARY KEY (evidence_id, memory_id));
CREATE TABLE IF NOT EXISTS tombstones (content_key TEXT NOT NULL, scope TEXT NOT NULL, memory_id TEXT NOT NULL,
                                       at TEXT NOT NULL, mac TEXT NOT NULL, PRIMARY KEY (content_key, scope));
CREATE TABLE IF NOT EXISTS erasure_ledger (memory_id TEXT NOT NULL, class TEXT NOT NULL, state TEXT NOT NULL,
                                           at TEXT NOT NULL, PRIMARY KEY (memory_id, class));
CREATE TABLE IF NOT EXISTS erasure_blobs (sha256 TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS erased_ids (id_key TEXT NOT NULL, birth TEXT NOT NULL, scope TEXT NOT NULL, blobs TEXT NOT NULL,
                                       PRIMARY KEY (id_key, birth));
CREATE TABLE IF NOT EXISTS births (memory_id TEXT PRIMARY KEY, token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chain_head (scope TEXT PRIMARY KEY, n INTEGER NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                                   scope TEXT NOT NULL CHECK (scope IN ('worker', 'main')), n INTEGER NOT NULL,
                                   memory_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                                   actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, schema_version INTEGER NOT NULL,
                                   rollback_version INTEGER, at TEXT NOT NULL, commitment TEXT NOT NULL,
                                   prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_keys (seq INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, key_hex TEXT NOT NULL,
                                       payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_by_record ON events (memory_id, seq);
CREATE INDEX IF NOT EXISTS keys_by_record ON event_keys (memory_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_replace BEFORE INSERT ON events
    WHEN EXISTS (SELECT 1 FROM events WHERE seq = NEW.seq)
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""
LEX_DDL = ("CREATE VIRTUAL TABLE lex USING fts5(memory_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');"
           "CREATE TABLE gen (value TEXT NOT NULL);")
CHAIN_FIELDS = ("scope", "n", "memory_id", "kind", "status", "actor_id", "actor_role", "schema_version",
                "rollback_version", "at", "commitment", "prev_hash")
EVENT_COLUMNS = CHAIN_FIELDS + ("hash",)


class ValidationError(ValueError):
    """The record is malformed."""


class VersionConflict(RuntimeError):
    """The record changed since the caller read it; nothing was written."""


class PolicyRefused(RuntimeError):
    """The record is well-formed, but policy forbids this."""


class ReadAfterWriteFailed(RuntimeError):
    """The normal search path could not find what was just written, so it was not written."""


class ErasureJournalCorrupt(RuntimeError):
    """The erasure journal beside the store cannot be read as a chain: the store does not open on it."""


class StoreNeedsRepair(RuntimeError):
    """A read-only open found something an ordinary open would repair or write; the reader refuses, never repairs."""


class ErasedIdReused(PolicyRefused):
    """This id was erased (the journal remembers it even where a restored copy does not): it is not given out again."""


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _copy(obj):
    return json.loads(_dumps(obj))


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _moment(text):
    moment = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


def _indexed_text(doc):
    text = " ".join(list(_strings(doc["object"])) + list(_strings(doc.get("aliases"))) + [doc["predicate"]])
    return unicodedata.normalize("NFC", text)


def _is_core_char(char):
    category = unicodedata.category(char)
    return category[0] in "LN" or category == "Co"


def _is_sure_separator(char):
    """Only ranges measured against unicode61: newer punctuation it still keeps inside a token (184 such characters)."""
    category, code = unicodedata.category(char), ord(char)
    return category == "Cc" or (category[0] == "P" and (code < 0x600 or 0x2010 <= code <= 0x2027 or 0x3001 <= code <= 0x3003))


def _tokens(text):
    """Chunks between spaces and punctuation, which the index always splits on too. Inside a chunk the index does the real
    tokenizing, so the two can never disagree."""
    cleaned = "".join(" " if _is_sure_separator(c) else c for c in unicodedata.normalize("NFC", text))
    return cleaned.split()


def _match_expression(tokens):
    # a double quote is always a separator (_is_sure_separator), so no token can hold one
    return " ".join('"%s"' % t for t in tokens)


def _scope_of(doc):
    return "worker" if doc["sensitivity"] in TEAM_LABELS else "main"


def _validate_actor(actor):
    if (not isinstance(actor, dict) or not isinstance(actor.get("id"), str) or not actor.get("id")
            or not isinstance(actor.get("role"), str) or not actor.get("role")):
        raise ValidationError("actor needs an id and a role, both text")
    _check_encodable(actor)


def _check_depth(value, limit=64):
    """Iterative on purpose: this is what stands between deep input and a RecursionError elsewhere."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            raise ValidationError("nested too deeply")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((v, depth + 1) for v in item)


def _check_encodable(value):
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError, RecursionError) as err:
        raise ValidationError("text cannot be encoded") from err


def _need_text(doc, *fields):
    for field in fields:
        if not isinstance(doc[field], str) or not doc[field]:
            raise ValidationError("%s must be text" % field)


def _validate(doc):
    missing = [k for k in REQUIRED if k not in doc]
    if missing:
        raise ValidationError("missing: %s" % ", ".join(missing))
    unknown = sorted(set(doc) - set(REQUIRED) - set(OPTIONAL) - set(SET_BY_STORE))
    if unknown:
        raise ValidationError("unknown field: %s" % ", ".join(unknown))
    for field, allowed in (("record_type", RECORD_TYPES), ("status", STATUSES),
                           ("authorship_channel", CHANNELS), ("sensitivity", SENSITIVITIES)):
        if not isinstance(doc[field], str) or doc[field] not in allowed:
            raise ValidationError("bad %s" % field)
    _validate_actor(doc["actor"])
    _check_depth(doc)
    _check_encodable(doc)
    if not isinstance(doc["predicate"], str) or not any(_is_core_char(c) for c in unicodedata.normalize("NFC", doc["predicate"])):
        raise ValidationError("predicate needs a letter or a digit")
    _need_text(doc, "subject_id", "authority", "retention_policy", "resurfacing_policy", "index_state")
    if doc["object"] is None:
        raise ValidationError("object is required")
    if not isinstance(doc["evidence_ids"], list) or not all(isinstance(e, str) for e in doc["evidence_ids"]):
        raise ValidationError("evidence_ids must be a list of ids")
    if not isinstance(doc["aliases"], list) or not all(isinstance(a, str) for a in doc["aliases"]):
        raise ValidationError("aliases must be a list of text")
    if not isinstance(doc["conflicts"], list):
        raise ValidationError("conflicts must be a list")
    confidence = doc["confidence"]
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not 0 <= confidence <= 1):       # NaN fails the comparison too
        raise ValidationError("confidence must be between 0 and 1")
    if doc["memory_id"] is not None and (not isinstance(doc["memory_id"], str) or not doc["memory_id"]):
        raise ValidationError("memory_id must be text")
    valid = doc["valid_time"]
    if not isinstance(valid, dict) or not valid.get("from") or set(valid) - {"from", "to"}:
        raise ValidationError("valid_time needs a start and nothing else")
    if not isinstance(valid["from"], str) or not isinstance(valid.get("to", None), (str, type(None))) or valid.get("to") == "":
        raise ValidationError("valid_time holds text dates")
    try:
        start = _moment(valid["from"])
        end = _moment(valid["to"]) if valid.get("to") else None
    except (TypeError, ValueError) as err:
        raise ValidationError("valid_time is not a date") from err
    if end is not None and end < start:
        raise ValidationError("valid_time ends before it starts")


def _need_reason(reason):
    if not isinstance(reason, str) or not reason:
        raise ValidationError("a reason is required")
    _check_encodable(reason)


class Store:
    def __init__(self, root, clock=None):
        self.root = os.path.abspath(root)
        if self.root.startswith("\\\\?\\") and not self.root.startswith("\\\\?\\UNC\\"):
            self.root = self.root[4:]           # SQLite cannot open this form as a URI
        self.clock = clock or _utc_now
        self.journal = self.root + JOURNAL_SUFFIX
        self.born = os.path.join(self.root, BORN_FILE)
        self.pending = self.root + PENDING_SUFFIX
        self._witness = None            # "ok", "missing" or "legacy"; a worker is served only on "ok"
        self._journal_notes = []        # what this open found about the journal, for verify(main)
        self.reapplied = 0              # erasures this open applied again from the journal: main reads it, verify does not
        self.archive = os.path.join(self.root, "archive")
        self.quarantine = os.path.join(self.root, "quarantine")     # beside the archive: no walk of it ever meets this
        os.makedirs(self.archive, exist_ok=True)
        database = os.path.join(self.root, "memory.sqlite")
        created = os.path.join(self.root, CREATED_FILE)
        if ((os.listdir(self.archive) or os.path.exists(created))
                and (not os.path.exists(database) or os.path.getsize(database) == 0)):
            raise RuntimeError("this store existed but its database is gone; refusing to start empty")
        self.salt = self._load_salt()       # first: a damaged salt must not leave a connection open behind it
        self.db = sqlite3.connect(os.path.join(self.root, "memory.sqlite"), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(DDL)
        if self._meta("generation") is None:    # a new database: its kept counts are born with it (p62)
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('generation', '0')")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('evidence_count', '0')")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('tombstone_count', '0')")
        self._migrate_erased_ids()
        self._expire_quarantine()       # before the sweep: what the sweep sets aside now starts its full term
        self._sweep_archive()
        try:
            self._open_journal()        # before the repair: the index is rebuilt after an erasure applied again, never before
        except BaseException:
            self.db.close()             # no suppress: a store whose erasures cannot be applied again does not open
            raise
        self._repair_on_open()
        self._finish_erasures()
        if not os.path.exists(created):         # only now: a crash during the first open may simply start again
            with open(created, "w", encoding="ascii") as mark:
                mark.write("1")
                mark.flush()
                os.fsync(mark.fileno())

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def _writing(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            before = self._count("evidence"), self._count("tombstones")
            yield
            # by what this write changed, never from the live table: a recount would forgive rows lost before it
            # only a real change is written: a write that changed nothing (an open of a healthy store) leaves the file as it
            # was. A missing or damaged count is never filled in (p62, reviewer M-22195): it reads as a number no real count
            # equals, a write keeps it that way, and verify names it.
            for key, table, start in (("evidence_count", "evidence", before[0]), ("tombstone_count", "tombstones", before[1])):
                delta = self._count(table) - start
                if delta:
                    self._set_meta(key, self._kept_count(key) + delta)
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _count(self, table):
        return self.db.execute("SELECT count(*) FROM %s" % table).fetchone()[0]

    def _kept_count(self, key):
        value = self._meta(key)
        if value is None:
            return -1 << 40         # gone (p62): the same as damaged, never 0 - a new database is born with both counts
        try:
            return int(value)
        except ValueError:
            return -1 << 40         # damaged: it can never equal a real count, so it keeps being reported

    def _counts_match(self):
        return (self._kept_count("evidence_count") == self._count("evidence")
                and self._kept_count("tombstone_count") == self._count("tombstones"))

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def _load_salt(self):
        """Kept outside the database, so a copy of the database alone cannot confirm a guessed text."""
        path = os.path.join(self.root, SALT_FILE)
        if os.path.exists(path):
            with open(path, encoding="ascii") as existing:
                salt = existing.read().strip()
            if len(salt) == 64:
                return salt
            if os.path.exists(os.path.join(self.root, "memory.sqlite")):
                raise RuntimeError("the tombstone salt is damaged; refusing to forget what was erased")
            os.remove(path)     # a crash before the first write of a store that holds nothing yet
        elif os.path.exists(os.path.join(self.root, "memory.sqlite")):
            raise RuntimeError("the tombstone salt is missing; refusing to forget what was erased")
        salt = secrets.token_hex(32)
        handle, tmp = tempfile.mkstemp(dir=self.root, prefix=".salt")
        with os.fdopen(handle, "w", encoding="ascii") as out:
            out.write(salt)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(tmp, path)      # atomic and refuses to overwrite: the first of two racing opens wins
        except FileExistsError:
            os.remove(tmp)
            with open(path, encoding="ascii") as existing:
                return existing.read().strip()
        os.remove(tmp)
        return salt

    def _keyed(self, *parts):
        return hmac.new(self.salt.encode(), _dumps(parts).encode("utf-8"), hashlib.sha256).hexdigest()

    # ---- evidence ----

    def _sweep_archive(self):
        """Leftovers of a crash: temp files, and blobs whose row never committed. Their bytes must not outlive an erasure."""
        if os.listdir(self.archive) and self._counts_match():      # damaged rows: a blob with no row may be the victim
            with self._writing():
                self._sweep_locked()

    def _sweep_locked(self):
        known = {r["sha256"] for r in self.db.execute("SELECT DISTINCT sha256 FROM evidence")}
        erasing = {r["sha256"] for r in self.db.execute("SELECT sha256 FROM erasure_blobs")}    # only the erasure removes these
        for name in os.listdir(self.archive):
            if name in known or name in erasing:
                continue
            path = os.path.join(self.archive, name)
            with contextlib.suppress(OSError):
                if name.startswith(".tmp"):
                    os.remove(path)                 # a write cut before its rename: never a whole blob
                else:
                    self._set_aside(path, name)

    def _set_aside(self, path, name):
        """A blob with no row is a cut put or a database older than the archive; it cannot be told which, so it waits."""
        os.makedirs(self.quarantine, exist_ok=True)
        target = os.path.join(self.quarantine, name)
        os.utime(path, None)        # the term runs from now, a replace keeps the age; stamped first, so a cut here leaves it in the archive
        os.replace(path, target)

    def _expire_quarantine(self):
        if not os.path.isdir(self.quarantine):
            return
        limit = time.time() - QUARANTINE_DAYS * 86400
        for name in os.listdir(self.quarantine):
            path = os.path.join(self.quarantine, name)
            with contextlib.suppress(OSError):
                if os.path.getmtime(path) < limit:
                    os.remove(path)

    def put_evidence(self, data, source, channel, actor, span=None, reader_scope="worker"):
        self._reader(reader_scope)
        if not isinstance(channel, str) or channel not in CHANNELS:
            raise ValidationError("bad channel")
        if not isinstance(data, (bytes, bytearray)):
            raise ValidationError("evidence is bytes")
        _validate_actor(actor)
        if not isinstance(source, str) or not source:
            raise ValidationError("source must be text")
        _check_depth(span)
        _check_encodable([source, span])
        sha = hashlib.sha256(data).hexdigest()
        evidence_id = "EVD-" + self._keyed("evidence", sha, source, span, reader_scope)[:16]
        with self._writing():               # the blob and its row appear together, and a sweep waits for both
            path = os.path.join(self.archive, sha)
            if not os.path.exists(path):
                handle, tmp = tempfile.mkstemp(dir=self.archive, prefix=".tmp")
                with os.fdopen(handle, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, path)
            self._insert_evidence(evidence_id, sha, source, channel, actor["id"], actor["role"],
                                  None if span is None else _dumps(span), reader_scope)
        return evidence_id

    def _insert_evidence(self, evidence_id, sha, source, channel, actor_id, actor_role, span, scope):
        mac = self._keyed("row", evidence_id, sha, source, channel, actor_id, actor_role, span, scope)
        self.db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?,?)",
                        (evidence_id, sha, source, channel, actor_id, actor_role, span, scope, mac))

    # ---- writing ----

    def _content_key(self, doc):
        return self._keyed("content", unicodedata.normalize("NFC", _dumps(doc["object"])))

    def _adopt_evidence(self, doc):
        """A private record never holds a worker's evidence row: it gets its own row over the same bytes, so what happens
        to the worker's row (and what the worker can observe of it) never depends on private records."""
        if _scope_of(doc) != "main":
            return
        adopted = []
        for evidence_id in doc["evidence_ids"]:
            row = self.db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None or row["scope"] != "worker":
                adopted.append(evidence_id)
                continue
            span = json.loads(row["span"]) if row["span"] is not None else None
            twin = "EVD-" + self._keyed("evidence", row["sha256"], row["source"], span, "main")[:16]
            self._insert_evidence(twin, row["sha256"], row["source"], row["channel"], row["actor_id"], row["actor_role"],
                                  row["span"], "main")
            adopted.append(twin)
        doc["evidence_ids"] = list(dict.fromkeys(adopted))

    def _return_evidence(self, doc):
        """Back to a team label: citations go back to the worker's own rows, where those still exist."""
        returned = []
        for evidence_id in doc["evidence_ids"]:
            row = self.db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            twin = None
            if row is not None and row["scope"] == "main":
                span = json.loads(row["span"]) if row["span"] is not None else None
                candidate = "EVD-" + self._keyed("evidence", row["sha256"], row["source"], span, "worker")[:16]
                own = self.db.execute("SELECT channel, actor_id, actor_role FROM evidence WHERE id=?", (candidate,)).fetchone()
                if own is not None and tuple(own) == (row["channel"], row["actor_id"], row["actor_role"]):
                    twin = candidate        # the same origin only: a citation must not change who it says this came from
            returned.append(twin or evidence_id)
        doc["evidence_ids"] = list(dict.fromkeys(returned))

    def _check_policy(self, doc, reader_scope, known_evidence=(), check_tombstone=True):
        if doc["sensitivity"] == "secret":
            raise PolicyRefused("secrets are not memory")
        if reader_scope == "worker" and _scope_of(doc) != "worker":
            raise PolicyRefused("only the main scope writes private memory")
        for evidence_id in doc["evidence_ids"]:
            if evidence_id in known_evidence:
                continue
            row = self.db.execute("SELECT scope FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None or (reader_scope == "worker" and row["scope"] != "worker"):
                raise ValidationError("no such evidence")
        if doc["status"] == "canonical":
            if doc["authorship_channel"] in MODEL_CHANNELS:
                raise PolicyRefused("a model's claim is not promoted directly")
            usable = set(doc["evidence_ids"]) - (set(self._hidden_evidence(doc)) if reader_scope == "worker" else set())
            if not usable and doc["authorship_channel"] not in ASSERTION_CHANNELS:
                raise PolicyRefused("canonical needs evidence or a direct assertion")
        if not check_tombstone:
            return
        scopes = ("worker",) if reader_scope == "worker" else ("worker", "main")
        if self.db.execute("SELECT 1 FROM tombstones WHERE content_key=? AND scope IN (%s)" % ",".join("?" * len(scopes)),
                           (self._content_key(doc),) + scopes).fetchone():
            raise PolicyRefused("this content was erased and is protected from coming back")

    def _sha_seen_by_worker(self, doc):
        return _sha(_dumps(self._view(_copy(doc), "worker")))

    def _event(self, scope, memory_id, kind, status, old_doc, new_doc, reason, actor, rollback_version, extra=None):
        head = self.db.execute("SELECT * FROM chain_head WHERE scope=?", (scope,)).fetchone()
        previous, number = (head["hash"], head["n"]) if head else ("", 0)
        event_key = secrets.token_hex(32)
        body = {"old_sha": self._sha_seen_by_worker(old_doc) if old_doc else None,      # never shows a hidden citation
                "new_sha": self._sha_seen_by_worker(new_doc) if new_doc else None, "reason": reason,
                "full_sha": _sha(_dumps(new_doc)) if new_doc else None,
                "version": new_doc["version"] if new_doc else None,
                "record_scope": _scope_of(new_doc) if new_doc else None}
        body.update(extra or {})
        payload = _dumps(body)
        row = {"scope": scope, "n": number + 1, "memory_id": memory_id, "kind": kind, "status": status,
               "actor_id": actor["id"], "actor_role": actor["role"], "schema_version": SCHEMA_VERSION,
               "rollback_version": rollback_version, "at": self.clock(),
               "commitment": hmac.new(event_key.encode(), payload.encode("utf-8"), hashlib.sha256).hexdigest(),
               "prev_hash": previous}
        digest = _sha(_dumps([row[k] for k in CHAIN_FIELDS]))
        cursor = self.db.execute("INSERT INTO events (%s) VALUES (%s)" % (", ".join(EVENT_COLUMNS),
                                                                         ", ".join("?" * len(EVENT_COLUMNS))),
                                 [row[k] for k in CHAIN_FIELDS] + [digest])
        self.db.execute("INSERT INTO event_keys VALUES (?,?,?,?)", (cursor.lastrowid, memory_id, event_key, payload))
        self.db.execute("INSERT OR REPLACE INTO chain_head VALUES (?,?,?)", (scope, row["n"], digest))

    def _note_return(self, doc, reader_scope):
        """In the main chain: a worker's own chain and numbering are untouched, so the worker can tell nothing."""
        if reader_scope != "worker":
            return
        row = self.db.execute("SELECT memory_id FROM tombstones WHERE content_key=? AND scope='main'",
                              (self._content_key(doc),)).fetchone()
        # a tombstone brought back from the journal for a record this copy never held has no record to log against
        if row and self.db.execute("SELECT 1 FROM records WHERE memory_id=?", (row["memory_id"],)).fetchone():
            self._event("main", row["memory_id"], "erased_returned", "tombstoned", None, None,
                        "returned as %s" % doc["memory_id"], doc["actor"], None)

    _lex_mode = "rw"                    # ReadOnlyStore: "ro", so a write through an index connection is an error

    def _lex(self, name):
        uri = pathlib.Path(os.path.join(self.root, name)).as_uri() + "?mode=" + self._lex_mode
        return contextlib.closing(sqlite3.connect(uri, uri=True, timeout=30))

    def _next_generation(self):
        """Random, never a counter: after a crash a counter would reuse a number and a wrong index would pass as current."""
        generation = secrets.token_hex(8)
        self._set_meta("generation", generation)
        return generation

    def _catch_up(self):
        """First thing in a write: if another process died with the indexes ahead, rebuild before this write adds anything,
        or the rebuild would index a document this transaction has not committed."""
        if not all(self._index_is_current(n) for n in LEXICAL.values()):
            try:
                self._rebuild_locked()
            except INDEX_ERRORS:            # this write will stamp the stale file current: the flag is what still tells
                self._set_meta("rebuild_needed", 1)

    def _retry_pending_rebuild(self):
        if self._meta("rebuild_needed"):
            self._recover()

    def _index(self, doc, generation):
        for reader_scope, name in LEXICAL.items():
            with self._lex(name) as con, con:
                con.execute("DELETE FROM lex WHERE memory_id=?", (doc["memory_id"],))
                if reader_scope == "main" or doc["sensitivity"] in TEAM_LABELS:
                    con.execute("INSERT INTO lex VALUES (?,?)", (doc["memory_id"], _indexed_text(doc)))
                con.execute("UPDATE gen SET value=?", (generation,))

    def _found_by_search(self, doc):
        tokens = _tokens(_indexed_text(doc))
        query = "SELECT 1 FROM lex WHERE lex MATCH ? AND memory_id=?"
        with self._lex(LEXICAL["main"]) as con:
            if con.execute(query, (_match_expression(tokens[:8]), doc["memory_id"])).fetchone():
                return True
            # Python's letters and the index's are not the same set: a word of the first kind may be nothing to the second
            return any(con.execute(query, (_match_expression([t]), doc["memory_id"])).fetchone() for t in tokens[:20000])

    def _index_and_confirm(self, doc, generation):
        """Inside the open transaction. Any failure leaves the indexes ahead of the database, and `_recover` rebuilds them."""
        try:
            self._index(doc, generation)
            found = self._found_by_search(doc)
        except sqlite3.Error as err:
            raise ReadAfterWriteFailed("index unavailable") from err
        if not found:
            raise ReadAfterWriteFailed("written, but the search could not find it")

    def _recover(self):
        with contextlib.suppress(*INDEX_ERRORS):
            self.rebuild()      # if this fails too, the generations disagree and the next open repairs them

    def _store_refs(self, doc):
        for evidence_id in doc["evidence_ids"]:
            self.db.execute("INSERT OR IGNORE INTO evidence_refs VALUES (?,?)", (evidence_id, doc["memory_id"]))

    def add(self, record, reason, reader_scope="worker"):
        self._reader(reader_scope)
        if not isinstance(record, dict):
            raise ValidationError("a record is a dict")
        if reader_scope == "worker" and record.get("memory_id"):
            raise PolicyRefused("only the main scope chooses an id")
        _check_depth(record)
        _check_encodable(record)
        doc = dict(OPTIONAL)
        doc.update(_copy(record))
        if any(k in record for k in SET_BY_STORE):
            raise ValidationError("version, system_time and supersedes are set by the store")
        _validate(doc)
        _need_reason(reason)
        if doc["status"] in STATUSES_BY_OPERATION_ONLY:
            raise ValidationError("this status comes only from an operation")
        now = self.clock()
        try:
            with self._writing():
                self._catch_up()
                self._check_policy(doc, reader_scope)
                self._adopt_evidence(doc)
                if not doc["memory_id"]:
                    doc["memory_id"] = "MEM-" + secrets.token_hex(8)    # random: a counter would count the private records
                elif self.db.execute("SELECT 1 FROM records WHERE memory_id=?", (doc["memory_id"],)).fetchone():
                    raise ValidationError("this memory_id exists")
                elif self.db.execute("SELECT 1 FROM erased_ids WHERE id_key=?",
                                     (self._journal_id(doc["memory_id"]),)).fetchone():
                    self._journal_notes.append("erasure witness: erased id reused: %s" % doc["memory_id"])
                    raise ErasedIdReused("this memory_id was erased and is not given out again")
                doc["version"] = 1
                doc["supersedes"] = None
                doc["system_time"] = {"created_at": now, "updated_at": now, "superseded_at": None}
                memory_id = doc["memory_id"]
                self.db.execute("INSERT INTO records VALUES (?,1,0,?)", (memory_id, _scope_of(doc)))
                self.db.execute("INSERT OR REPLACE INTO births VALUES (?,?)", (memory_id, secrets.token_hex(16)))
                self.db.execute("INSERT INTO versions VALUES (?,1,?,NULL,NULL)", (memory_id, _dumps(doc)))
                self._store_refs(doc)
                self._event(_scope_of(doc), memory_id, "add", doc["status"], None, doc, reason, doc["actor"], None)
                self._note_return(doc, reader_scope)
                self._index_and_confirm(doc, self._next_generation())
        except (ReadAfterWriteFailed, sqlite3.Error):
            self._recover()
            raise
        self._retry_pending_rebuild()
        return memory_id

    def correct(self, memory_id, expected_version, changes, reason, actor, reader_scope="worker"):
        self._reader(reader_scope)
        _validate_actor(actor)
        if type(expected_version) is not int:
            raise ValidationError("expected_version must be an integer")
        if not isinstance(changes, dict):
            raise ValidationError("changes are a dict")
        _check_depth(changes)
        _check_encodable(changes)
        fixed = sorted(set(changes) & ({"memory_id"} | set(SET_BY_STORE)))
        if fixed:
            raise ValidationError("cannot change: %s" % ", ".join(fixed))
        _need_reason(reason)
        leaves_worker = False
        if not self._id_ok(memory_id):
            raise KeyError(memory_id)
        try:
            with self._writing():
                self._catch_up()
                row = self.db.execute("SELECT * FROM records WHERE memory_id=?", (memory_id,)).fetchone()
                if row is None or (reader_scope == "worker" and row["scope"] != "worker"):
                    raise KeyError(memory_id)       # the same for a private id as for one that never existed
                if row["tombstoned"]:
                    raise PolicyRefused("this record was erased")
                if row["current_version"] != expected_version:
                    raise VersionConflict("expected %s, found %s" % (expected_version, row["current_version"]))
                old = self._doc(memory_id, expected_version)
                now = self.clock()
                new = _copy(old)
                new.update(_copy(changes))
                content_changed = _dumps(new["object"]) != _dumps(old["object"])       # True is not 1
                if "valid_time" not in changes and content_changed:
                    new["valid_time"] = {"from": now, "to": None}
                hidden = self._hidden_evidence(old) if reader_scope == "worker" else []
                if "evidence_ids" in changes and hidden and isinstance(new["evidence_ids"], list) and all(
                        isinstance(e, str) for e in new["evidence_ids"]):
                    new["evidence_ids"] = list(dict.fromkeys(list(new["evidence_ids"]) + hidden))
                _validate(new)
                if new["status"] in STATUSES_BY_OPERATION_ONLY:
                    raise ValidationError("this status comes only from an operation")
                self._check_policy(new, reader_scope, known_evidence=old["evidence_ids"], check_tombstone=content_changed)
                self._adopt_evidence(new)
                if _scope_of(old) == "main" and _scope_of(new) == "worker":
                    self._return_evidence(new)
                new["version"] = expected_version + 1
                new["supersedes"] = expected_version
                new["system_time"] = {"created_at": old["system_time"]["created_at"], "updated_at": now,
                                      "superseded_at": None}
                closed_to = None        # the stored document never changes; its closing lies beside it
                if not old["valid_time"].get("to") and _moment(new["valid_time"]["from"]) != _moment(old["valid_time"]["from"]):
                    closed_to = max(new["valid_time"]["from"], old["valid_time"]["from"], key=_moment)
                self.db.execute("UPDATE versions SET closed_at=?, closed_valid_to=? WHERE memory_id=? AND version=?",
                                (now, closed_to, memory_id, expected_version))
                self.db.execute("INSERT INTO versions VALUES (?,?,?,NULL,NULL)", (memory_id, new["version"], _dumps(new)))
                if _scope_of(new) == "main":
                    self.db.execute("DELETE FROM evidence_refs WHERE memory_id=? AND evidence_id IN "
                                    "(SELECT id FROM evidence WHERE scope='worker')", (memory_id,))
                self.db.execute("UPDATE records SET current_version=?, scope=? WHERE memory_id=?",
                                (new["version"], _scope_of(new), memory_id))
                self._store_refs(new)
                touches_main = _scope_of(old) == "main" or _scope_of(new) == "main"
                self._event("main" if touches_main else "worker", memory_id, "correct", new["status"], old, new, reason,
                            actor, expected_version, {"closed_at": now, "closed_valid_to": closed_to})
                if content_changed:
                    self._note_return(new, reader_scope)
                leaves_worker = _scope_of(old) == "worker" and _scope_of(new) == "main"
                if leaves_worker:
                    self._set_meta("rebuild_needed", 1)     # survives a crash before the rebuild below
                self._index_and_confirm(new, self._next_generation())
        except (ReadAfterWriteFailed, sqlite3.Error):
            self._recover()
            raise
        self._retry_pending_rebuild()   # a deleted FTS row stays in its segment until a merge; a fresh file holds nothing
        return new["version"]

    # ---- reading ----

    def _doc(self, memory_id, version):
        row = self.db.execute("SELECT * FROM versions WHERE memory_id=? AND version=?", (memory_id, version)).fetchone()
        return self._closed(row) if row else None

    @staticmethod
    def _closed(row):
        doc = json.loads(row["doc"])
        doc["system_time"]["superseded_at"] = row["closed_at"]
        if row["closed_valid_to"] is not None:
            doc["valid_time"]["to"] = row["closed_valid_to"]
        return doc

    def _reader(self, reader_scope):
        if not isinstance(reader_scope, str) or reader_scope not in READERS:
            raise PolicyRefused("unknown reader")
        if reader_scope == "worker" and self._witness != "ok":
            # fail closed: without the witness an erased private record may be back, and None would look like "no such"
            raise PolicyRefused("the erasure witness is missing; the main scope must repair it")
        if reader_scope == "worker" and not self._journal_matches_base():
            raise PolicyRefused("the erasure journal is ahead of this base: an erasure main ordered is not applied here yet")

    def _journal_matches_base(self):
        """Round 5: another process may have left an erasure intent (its COMMIT failed after the journal line). An open
        store must not serve that record to a worker. Read-only, and the file is read only when its size or time changed."""
        try:
            st = os.stat(self.journal)
        except OSError:
            self._witness = "missing"
            return False
        sig = (st.st_size, st.st_mtime_ns, self._meta("journal_seq"), self._meta("journal_head"))
        if sig == getattr(self, "_journal_seen", None):
            return True
        tail, _ = self._journal_read(whole=False)       # complete lines only: a torn piece is a write still in flight
        if tail:
            try:
                seq = json.loads(tail[0][2].decode("utf-8")).get("seq")
            except (UnicodeDecodeError, ValueError, RecursionError, AttributeError):
                return False
            ok = self._anchor() == (seq, self._line_hash(tail[0][2]))
        else:
            ok = self._anchor() == (0, "")
        if ok:
            self._journal_seen = sig
        return ok

    @staticmethod
    def _id_ok(memory_id):
        try:
            return isinstance(memory_id, str) and bool(memory_id.encode("utf-8"))
        except UnicodeEncodeError:
            return False

    def _current(self, memory_id):
        if not self._id_ok(memory_id):
            return None
        row = self.db.execute("SELECT * FROM records WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None or row["tombstoned"]:
            return None
        return self._doc(memory_id, row["current_version"])

    @staticmethod
    def _visible(doc, reader_scope):
        return reader_scope == "main" or doc["sensitivity"] in TEAM_LABELS

    def _hidden_evidence(self, doc):
        hidden = []
        for evidence_id in doc["evidence_ids"]:
            row = self.db.execute("SELECT scope FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None or row["scope"] != "worker":
                hidden.append(evidence_id)
        return hidden

    def _view(self, doc, reader_scope):
        if reader_scope == "worker":
            hidden = set(self._hidden_evidence(doc))
            doc["evidence_ids"] = [e for e in doc["evidence_ids"] if e not in hidden]
        return doc

    def erased_id(self, memory_id, reader_scope="worker"):
        """Main only: was this memory_id erased (and so never given out again)? An adapter that derives its ids asks this
        BEFORE it writes any evidence, so the bytes of an erased entry never come back even as an orphan (organ 2)."""
        if reader_scope != "main":
            raise PolicyRefused("only the main scope asks about erasures")
        self._reader(reader_scope)
        if not self._id_ok(memory_id):
            return False
        if self.db.execute("SELECT 1 FROM records WHERE memory_id=? AND tombstoned=1", (memory_id,)).fetchone():
            return True                     # erased here: the row stays as a tombstone
        return self.db.execute("SELECT 1 FROM erased_ids WHERE id_key=?",          # erased elsewhere, only the id mark
                               (self._journal_id(memory_id),)).fetchone() is not None

    def get(self, memory_id, reader_scope="worker", as_of=None):
        self._reader(reader_scope)
        current = self._current(memory_id)
        if current is None or not self._visible(current, reader_scope):
            return None         # the same answer as an id that never existed; today's label guards old versions too
        if as_of is None:
            return self._view(current, reader_scope)
        moment = _moment(as_of)
        rows = self.db.execute("SELECT * FROM versions WHERE memory_id=? ORDER BY version", (memory_id,)).fetchall()
        for row in rows:
            doc = json.loads(row["doc"])        # as it stood then: a later closing is not part of what was known
            if (self._visible(doc, reader_scope) and _moment(doc["system_time"]["updated_at"]) <= moment
                    and (row["closed_at"] is None or moment < _moment(row["closed_at"]))):
                return self._view(doc, reader_scope)
        return None

    def history(self, memory_id, reader_scope="worker"):
        self._reader(reader_scope)
        current = self._current(memory_id)
        if current is None or not self._visible(current, reader_scope):
            return []
        rows = self.db.execute("SELECT * FROM versions WHERE memory_id=? ORDER BY version", (memory_id,)).fetchall()
        out = []
        for index, row in enumerate(rows):
            doc = self._closed(row)
            if self._visible(doc, reader_scope):
                following = rows[index + 1] if index + 1 < len(rows) else None
                if following is not None and not self._visible(json.loads(following["doc"]), reader_scope):
                    doc["valid_time"]["to"] = json.loads(row["doc"])["valid_time"].get("to")     # that date came from a version it cannot see
                out.append(self._view(doc, reader_scope))
        return out

    def evidence_sources(self, memory_id, reader_scope="worker"):
        """Where the current version's evidence came from; empty for a record this scope cannot see."""
        doc = self.get(memory_id, reader_scope=reader_scope)
        if doc is None:
            return []
        found = []
        for evidence_id in doc["evidence_ids"]:
            row = self.db.execute("SELECT source FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is not None:
                found.append(row["source"])
        return found

    def _audit_visible(self, memory_id, reader_scope):
        self._reader(reader_scope)
        if not self._id_ok(memory_id):
            return False
        row = self.db.execute("SELECT scope FROM records WHERE memory_id=?", (memory_id,)).fetchone()
        return row is not None and (reader_scope == "main" or row["scope"] == "worker")

    def _event_view(self, row, reader_scope):
        event = {k: row[k] for k in ("n", "memory_id", "kind", "status", "actor_id", "actor_role", "schema_version",
                                     "rollback_version", "at", "prev_hash", "hash")}
        kept = self.db.execute("SELECT payload FROM event_keys WHERE seq=?", (row["seq"],)).fetchone()
        payload = json.loads(kept["payload"]) if kept else {}
        for key in ("old_sha", "new_sha", "reason"):
            event[key] = payload.get(key)
        if reader_scope == "main":
            event["scope"] = row["scope"]
            event["global_seq"] = row["seq"]
        return event

    def events(self, memory_id, reader_scope="worker"):
        if not self._audit_visible(memory_id, reader_scope):
            return []
        where = "memory_id=?" + ("" if reader_scope == "main" else " AND scope='worker'")
        rows = self.db.execute("SELECT * FROM events WHERE %s ORDER BY seq" % where, (memory_id,)).fetchall()
        return [self._event_view(r, reader_scope) for r in rows]

    def all_events(self, reader_scope="worker"):
        """The total order across both chains, by global_seq. Main only: the order itself counts private writes."""
        self._reader(reader_scope)
        if reader_scope != "main":
            raise PolicyRefused("the total order belongs to the main scope")
        rows = self.db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [self._event_view(r, "main") for r in rows]

    def status_from_events(self, memory_id, reader_scope="worker"):
        """The main scope reads the whole log. A worker gets the record's state, since its chain can miss main-side changes."""
        if not self._audit_visible(memory_id, reader_scope):
            return None
        if reader_scope == "main":
            row = self.db.execute("SELECT status FROM events WHERE memory_id=? ORDER BY seq DESC LIMIT 1",
                                  (memory_id,)).fetchone()
            return row["status"] if row else None
        current = self._current(memory_id)
        return "tombstoned" if current is None else current["status"]

    def search(self, query, reader_scope="worker", limit=20):
        """Outcome: found, not_found, or unknown when the index cannot answer. A worker's answer has the same
        shape and scope whether or not a private match exists: refusing only on a real match would reveal the match."""
        self._reader(reader_scope)
        if not isinstance(query, str):
            raise ValidationError("a query is text")
        _check_encodable(query)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValidationError("limit must be between 1 and 10000")
        answer = {"outcome": "not_found", "hits": [], "scope": "all" if reader_scope == "main" else "excludes_private"}
        tokens = _tokens(query)
        if not tokens:
            return answer
        try:
            with self._lex(LEXICAL[reader_scope]) as con:
                rows = con.execute("SELECT memory_id FROM lex WHERE lex MATCH ? ORDER BY rank, memory_id LIMIT ?",
                                   (_match_expression(tokens), limit)).fetchall()
        except sqlite3.Error:
            return dict(answer, outcome="unknown")
        for (memory_id,) in rows:
            doc = self._current(memory_id)
            if doc is not None and self._visible(doc, reader_scope):
                answer["hits"].append(self._view(doc, reader_scope))
        answer["outcome"] = "found" if answer["hits"] else "not_found"
        return answer

    # ---- indexes ----

    def _write_index(self, name, docs):
        """Build beside, then swap: a reader never sees half an index, and old segments and journals do not survive."""
        target = os.path.join(self.root, name)
        tmp = target + ".new"
        for leftover in (tmp, tmp + "-journal"):
            if os.path.exists(leftover):
                os.remove(leftover)
        with contextlib.closing(sqlite3.connect(tmp)) as con, con:
            con.executescript(LEX_DDL)
            con.executemany("INSERT INTO lex VALUES (?,?)", [(d["memory_id"], _indexed_text(d)) for d in docs])
            con.execute("INSERT INTO gen VALUES (?)", (self._meta("generation"),))
        for suffix in ("-journal", "-wal", "-shm"):     # a stale journal could roll the new file back
            with contextlib.suppress(FileNotFoundError):
                os.remove(target + suffix)
        for attempt in range(6):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:     # Windows: a reader still has the file open for a moment
                if attempt == 5:
                    raise
                time.sleep(0.1)

    def _rebuild_locked(self):
        rows = self.db.execute("SELECT memory_id FROM records WHERE tombstoned=0").fetchall()
        docs = [d for d in (self._indexable(r["memory_id"]) for r in rows) if d is not None]
        self._write_index(LEXICAL["main"], docs)
        self._write_index(LEXICAL["worker"], [d for d in docs if d["sensitivity"] in TEAM_LABELS])
        self.db.execute("DELETE FROM meta WHERE key='rebuild_needed'")

    def _indexable(self, memory_id):
        """None for a record that cannot be read: one damaged document must not stop the store, and verify reports it."""
        try:
            doc = self._current(memory_id)
            if doc is not None:
                _indexed_text(doc)
                _scope_of(doc)
            return doc
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, sqlite3.DataError):
            return None

    def rebuild(self):
        """Holds the write lock from reading the records to the swap, so no write can fall between them."""
        with self._writing():
            self._rebuild_locked()

    def _index_is_current(self, name):
        path = os.path.join(self.root, name)
        if not os.path.exists(path):
            return False
        try:
            with self._lex(name) as con:
                row = con.execute("SELECT value FROM gen").fetchone()
                con.execute("SELECT 1 FROM lex LIMIT 1").fetchall()     # p62: a gen table without its lex is no index
        except sqlite3.Error:
            return False
        return row is not None and row[0] == self._meta("generation")

    def _repair_on_open(self):
        if self._meta("rebuild_needed") or not all(self._index_is_current(n) for n in LEXICAL.values()):
            self._recover()

    # ---- erasure ----

    def erase(self, memory_id, reason, actor, reader_scope="worker"):
        self._reader(reader_scope)
        if reader_scope != "main":
            raise PolicyRefused("only the main scope erases")   # before any look-up: the answer cannot depend on the id
        _validate_actor(actor)
        _need_reason(reason)
        if not self._id_ok(memory_id):
            raise KeyError(memory_id)
        with self._writing():
            if self.db.execute("SELECT 1 FROM records WHERE memory_id=?", (memory_id,)).fetchone() is None:
                raise KeyError(memory_id)
            state = self._journal_sync()        # from the disk, under the lock: not what this process saw at its open
            row = self.db.execute("SELECT * FROM records WHERE memory_id=?", (memory_id,)).fetchone()
            if not row["tombstoned"]:           # read AFTER the sync: it may have just applied this very erasure again
                before = {r["id"] for r in self.db.execute("SELECT id FROM evidence")}
                self._erase_rows(memory_id, row["scope"], actor)
                gone = before - {r["id"] for r in self.db.execute("SELECT id FROM evidence")}
                entry = self._journal_entry(memory_id, row["scope"], gone)
                if state == "ok":
                    self._journal_append([entry])
                else:                           # no journal: the erasure still goes outside the folder, and workers stay out
                    self._pending_append(entry)
        self._witness = state                   # after COMMIT only (round 5): a rolled-back write must not open the door
        self._finish_erasures()     # also when already tombstoned: an earlier attempt may still be pending
        return {r["class"]: r["state"] for r in self.erasure_ledger(memory_id, reader_scope="main")}

    def _erase_rows(self, memory_id, record_scope, actor, kind="erase"):
        docs = [json.loads(r["doc"]) for r in self.db.execute("SELECT doc FROM versions WHERE memory_id=?", (memory_id,))]
        now = self.clock()
        for doc in docs:
            scope = "worker" if record_scope == "worker" and _scope_of(doc) == "worker" else "main"
            content_key = self._content_key(doc)
            self.db.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?,?,?,?)",
                            (content_key, scope, memory_id, now, self._keyed("row", content_key, scope, memory_id, now)))
        refs = [r["evidence_id"] for r in
                self.db.execute("SELECT evidence_id FROM evidence_refs WHERE memory_id=?", (memory_id,))]
        cited = [r["sha256"] for r in self.db.execute(
            "SELECT DISTINCT e.sha256 FROM evidence e JOIN evidence_refs f ON f.evidence_id=e.id WHERE f.memory_id=?", (memory_id,))]
        self.db.execute("DELETE FROM evidence_refs WHERE memory_id=?", (memory_id,))
        for evidence_id in refs:
            self._drop_orphan_evidence(evidence_id)
        # Said out loud in the ledger: removing a worker's own bytes would tell it that a private record had cited them.
        kept = record_scope == "main" and any(self.db.execute(
            "SELECT 1 FROM evidence WHERE sha256=? AND scope='worker'", (sha,)).fetchone() for sha in cited)
        self.db.execute("DELETE FROM versions WHERE memory_id=?", (memory_id,))
        self.db.execute("UPDATE records SET tombstoned=1 WHERE memory_id=?", (memory_id,))
        self._event(record_scope, memory_id, kind, "tombstoned", None, None, None, actor, None)
        self.db.execute("DELETE FROM event_keys WHERE memory_id=?", (memory_id,))     # after the event: it keeps nothing either
        self._next_generation()
        self.db.executemany("INSERT INTO erasure_ledger VALUES (?,?,?,?)",
                            [(memory_id, c, KEPT_PENDING if kept and c == "archive" else "pending", now)
                             for c in ERASURE_CLASSES])

    def _drop_orphan_evidence(self, evidence_id):
        if self.db.execute("SELECT 1 FROM evidence_refs WHERE evidence_id=?", (evidence_id,)).fetchone():
            return
        row = self.db.execute("SELECT sha256, scope FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if row is None:
            return
        twins = self.db.execute("SELECT id FROM evidence WHERE sha256=? AND scope=?", (row["sha256"], row["scope"])).fetchall()
        for twin in twins:
            if not self.db.execute("SELECT 1 FROM evidence_refs WHERE evidence_id=?", (twin["id"],)).fetchone():
                self.db.execute("DELETE FROM evidence WHERE id=?", (twin["id"],))
        if not self.db.execute("SELECT 1 FROM evidence WHERE sha256=?", (row["sha256"],)).fetchone():
            self.db.execute("INSERT OR IGNORE INTO erasure_blobs VALUES (?)", (row["sha256"],))

    def _finish_erasures(self):
        """Idempotent, and run again on every open and every erase: until it has really finished it stays pending."""
        if not self.db.execute("SELECT 1 FROM erasure_ledger WHERE state='pending'").fetchone():
            return
        with contextlib.suppress(*INDEX_ERRORS):
            with self._writing():
                self._rebuild_locked()
                if self._counts_match():
                    self._sweep_locked()
                for row in self.db.execute("SELECT sha256 FROM erasure_blobs").fetchall():
                    if not self.db.execute("SELECT 1 FROM evidence WHERE sha256=?", (row["sha256"],)).fetchone():
                        with contextlib.suppress(FileNotFoundError):
                            os.remove(os.path.join(self.archive, row["sha256"]))
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(os.path.join(self.quarantine, row["sha256"]))
                    self.db.execute("DELETE FROM erasure_blobs WHERE sha256=?", (row["sha256"],))
                now = self.clock()
                self.db.execute("UPDATE erasure_ledger SET state=?, at=? WHERE state=?", (KEPT_DONE, now, KEPT_PENDING))
                self.db.execute("UPDATE erasure_ledger SET state='done', at=? WHERE state='pending'", (now,))

    def erasure_ledger(self, memory_id, reader_scope="worker"):
        if not self._audit_visible(memory_id, reader_scope):
            return []
        rows = self.db.execute("SELECT class, state, at FROM erasure_ledger WHERE memory_id=?", (memory_id,))
        return [dict(r) for r in rows]

    # ---- the erasure journal (p61): erasures survive a restore of the store from a copy ----
    #
    # <root>.erasures.jsonl, beside the folder. One line per erasure: a JSON object, then b"\n". A line's hash is the sha256
    # hex of its bytes without the newline; prev_hash is the hash of the line before ("" for the first); seq runs from 1.
    # meta keeps the anchor: journal_seq and journal_head of the last line this base has applied. An ordinary open reads
    # only the last line; a full pass runs when it does not match the anchor (a copy was restored, or the journal was).
    # No bare ids and no bare hashes in a line: ids, births and content keys are keyed with the salt.

    def _migrate_erased_ids(self):
        """W13: a base written by ff047ad keeps erased_ids(id_key) only. Its birth, scope and evidence are taken from the
        journal lines with that id; an id no line names keeps birth "" and verify(main) names it. Older lines listed bytes
        ("blobs"), not evidence rows: those are not carried over."""
        if "birth" in [r["name"] for r in self.db.execute("PRAGMA table_info(erased_ids)")]:
            return                              # the cheap look outside the lock; the decision is taken inside it
        with self._writing():                   # round 5: the check AND the journal read under the lock, or a second
            if "birth" in [r["name"] for r in self.db.execute("PRAGMA table_info(erased_ids)")]:     # process migrates
                return                                                                               # from a stale read
            self._migrate_erased_ids_locked()

    def _migrate_erased_ids_locked(self):
        known = {}
        if os.path.exists(self.journal):
            with open(self.journal, "rb") as f:
                for raw in f.read().split(b"\n"):
                    try:
                        line = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        continue
                    if isinstance(line, dict) and isinstance(line.get("id"), str) and isinstance(line.get("birth"), str):
                        evidence = line.get("evidence") if isinstance(line.get("evidence"), list) else []
                        scope = line.get("scope") if line.get("scope") in ("worker", "main") else "main"
                        known.setdefault(line["id"], {})[line["birth"]] = (scope, _dumps(evidence))
        old = [r["id_key"] for r in self.db.execute("SELECT id_key FROM erased_ids")]
        self.db.execute("ALTER TABLE erased_ids RENAME TO erased_ids_old")
        self.db.execute("CREATE TABLE erased_ids (id_key TEXT NOT NULL, birth TEXT NOT NULL, scope TEXT NOT NULL, "
                        "blobs TEXT NOT NULL, PRIMARY KEY (id_key, birth))")     # execute, never executescript: no COMMIT here
        for id_key in old:
            for birth, (scope, evidence) in (known.get(id_key) or {"": ("main", "[]")}).items():
                self.db.execute("INSERT OR IGNORE INTO erased_ids VALUES (?,?,?,?)", (id_key, birth, scope, evidence))
        self.db.execute("DROP TABLE erased_ids_old")

    def _journal_id(self, memory_id):
        return self._keyed("journal-id", memory_id)

    def _journal_birth(self, memory_id):
        """A random token written by add, so two records that share an id and a clock still differ; older records fall back
        to the moment of their add event."""
        row = self.db.execute("SELECT token FROM births WHERE memory_id=?", (memory_id,)).fetchone()
        if row is not None:
            return self._keyed("journal-birth", memory_id, row["token"])
        added = self.db.execute("SELECT at FROM events WHERE memory_id=? AND kind='add' ORDER BY seq LIMIT 1",
                                (memory_id,)).fetchone()
        return self._keyed("journal-birth-legacy", memory_id, added["at"] if added else None)

    def _journal_entry(self, memory_id, record_scope, removed_evidence=()):
        keys = [[r["content_key"], r["scope"]] for r in self.db.execute(
            "SELECT content_key, scope FROM tombstones WHERE memory_id=? ORDER BY content_key, scope", (memory_id,))]
        entry = {"id": self._journal_id(memory_id), "birth": self._journal_birth(memory_id), "keys": keys,
                 "scope": record_scope, "at": self.clock()}
        if removed_evidence:    # the evidence ROWS this erasure removed, keyed (never bare, the journal lies outside the
            entry["evidence"] = sorted(self._keyed("journal-evidence", e) for e in removed_evidence)   # folder); by row,
        return entry            # not by whether the bytes lived on: that would tell a worker about private twins (W12)

    def _ledger_entries(self, known=frozenset()):
        """What this base knows of erasures, as journal entries: every erased record of the ledger, and every id the base
        holds only as a mark brought back from a journal (erased_ids, W4), with its tombstones. Ids in `known` are left out."""
        entries = []
        for r in self.db.execute("SELECT memory_id FROM erasure_ledger GROUP BY memory_id ORDER BY min(at), memory_id").fetchall():
            if (self._journal_id(r["memory_id"]), self._journal_birth(r["memory_id"])) not in known:
                entries.append(self._journal_entry(r["memory_id"], self._record_scope(r["memory_id"])))
        for r in self.db.execute("SELECT id_key, birth, scope, blobs FROM erased_ids ORDER BY id_key, birth").fetchall():
            if (r["id_key"], r["birth"]) in known:
                continue
            owner = "JRN-" + r["id_key"][:16]
            keys = [[t["content_key"], t["scope"]] for t in self.db.execute(
                "SELECT content_key, scope FROM tombstones WHERE memory_id=? ORDER BY content_key, scope", (owner,))]
            entry = {"id": r["id_key"], "birth": r["birth"], "keys": keys, "scope": r["scope"], "at": self.clock()}
            if json.loads(r["blobs"]):
                entry["evidence"] = json.loads(r["blobs"])
            entries.append(entry)
        return entries

    @staticmethod
    def _line_hash(raw):
        return hashlib.sha256(raw).hexdigest()

    def _journal_read(self, whole):
        """(lines, torn): lines = [(number, offset, raw bytes)] of complete lines. With whole=False only the last complete
        line is read, from the end of the file, whatever its length. torn = bytes after the last newline."""
        with open(self.journal, "rb") as f:
            if whole:
                data = f.read()
                cut = data.rfind(b"\n") + 1
                lines, offset = [], 0
                for number, raw in enumerate(data[:cut].split(b"\n")[:-1], 1):
                    lines.append((number, offset, raw))
                    offset += len(raw) + 1
                return lines, data[cut:]
            f.seek(0, os.SEEK_END)
            end = f.tell()
            chunk, pos, data = 4096, end, b""
            while pos > 0:
                pos = max(0, pos - chunk)
                f.seek(pos)
                data = f.read(end - pos)
                body = data[:data.rfind(b"\n") + 1] if b"\n" in data else b""
                if body.count(b"\n") >= 2 or (pos == 0 and body):
                    break
                chunk *= 2
            cut = data.rfind(b"\n") + 1
            torn = data[cut:]
            body = data[:cut]
            if not body:
                return [], torn
            start = body.rfind(b"\n", 0, len(body) - 1) + 1
            return [(None, pos + start, body[start:-1])], torn

    def _journal_check(self, lines):
        """Structure and chain of every line: JSON object -> seq -> prev_hash, in that order. Returns parsed lines."""
        parsed, previous = [], ""
        for number, offset, raw in lines:
            where = "erasure journal corrupt at line %d, offset %d" % (number, offset)
            try:
                line = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as err:
                raise ErasureJournalCorrupt("%s: not a JSON line" % where) from err
            if not isinstance(line, dict):
                raise ErasureJournalCorrupt("%s: not a JSON object" % where)
            seq = line.get("seq")
            if type(seq) is not int or seq != number:
                raise ErasureJournalCorrupt("%s: seq %r where %d belongs (a gap, a repeat or a reorder)" % (where, seq, number))
            if not isinstance(line.get("prev_hash"), str) or line["prev_hash"] != previous:
                raise ErasureJournalCorrupt("%s: prev_hash does not match the line before" % where)
            previous = self._line_hash(raw)
            parsed.append((line, previous, number, offset))
        return parsed

    def _anchor(self):
        """(seq, head) or None. A damaged anchor (not a number, below zero, seq 0 with a head, a head that is not a hash)
        is no anchor: the open takes the full pass, and verify(main) says so."""
        seq, head = self._meta("journal_seq"), self._meta("journal_head")
        if seq is None and head is None:
            return None
        try:
            number = int(seq)
        except (TypeError, ValueError):
            number = None
        ok = (number is not None and head is not None and number >= 0 and (number == 0) == (head == "")
              and (head == "" or (len(head) == 64 and all(c in "0123456789abcdef" for c in head))))
        if not ok:
            note = "erasure witness: journal anchor out of range"
            if note not in self._journal_notes:
                self._journal_notes.append(note)
            return None
        return number, head

    def _set_anchor(self, seq, head):
        self._set_meta("journal_seq", seq)
        self._set_meta("journal_head", head)

    def _write_whole(self, path, data, exclusive=False):
        with open(path, "xb" if exclusive else "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())

    def _file_tail(self):
        """Under the write lock: (seq, hash) of the journal's last line, read from the FILE, never from meta. A torn tail
        that is a whole next line (only its newline is missing) is an intent and gets its newline; any other torn tail is
        an unfinished write and is cut off. Another process may have appended since this one looked: the file decides."""
        tail, torn = self._journal_read(whole=False)
        seq, head = 0, ""
        if tail:
            raw = tail[0][2]
            try:
                line = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                line = None
            if not isinstance(line, dict) or type(line.get("seq")) is not int:
                return None             # not a journal line at the end: the full pass decides (cut or corrupt)
            seq, head = line["seq"], self._line_hash(raw)
        if torn:
            try:
                piece = json.loads(torn.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                piece = None
            whole = isinstance(piece, dict) and piece.get("seq") == seq + 1 and piece.get("prev_hash") == head
            with open(self.journal, "r+b") as f:
                f.seek(0, os.SEEK_END)
                if whole:
                    f.write(b"\n")      # main ordered it and the line is whole: the intent stands (TZ p61, 3)
                else:
                    f.truncate(f.tell() - len(torn))
                f.flush()
                os.fsync(f.fileno())
            if whole:
                seq, head = seq + 1, self._line_hash(torn)
        return seq, head

    def _journal_append(self, entries, move_anchor=True):
        """Inside the open write transaction, before COMMIT: the intent is on disk before the base says it happened. seq and
        prev_hash continue the FILE's last line. If the commit then fails, the journal is ahead of the anchor, and the next
        erase or open applies that line again: main already ordered it."""
        tail = self._file_tail()
        if tail is None:
            raise ErasureJournalCorrupt("erasure journal corrupt at its last line: not a journal line")
        seq, previous = tail
        raws = []
        for entry in entries:
            seq += 1
            raw = _dumps(dict(entry, seq=seq, prev_hash=previous)).encode("utf-8")
            raws.append(raw)
            previous = self._line_hash(raw)
        with open(self.journal, "r+b") as out:      # r+b, never ab: a journal deleted meanwhile is not silently re-created
            out.seek(0, os.SEEK_END)
            out.write(b"".join(r + b"\n" for r in raws))
            out.flush()
            os.fsync(out.fileno())
        if move_anchor:
            self._set_anchor(seq, previous)

    def _pending_read(self):
        """Erasures made while there was no journal: <root>.erasures.pending.jsonl, beside the folder, plain entries."""
        if not os.path.exists(self.pending):
            return []
        with open(self.pending, "rb") as f:
            data = f.read()
        entries = []
        pieces = data.split(b"\n")
        for number, raw in enumerate(pieces, 1):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as err:
                if number == len(pieces):
                    continue                # an unfinished last write (no newline after it): its commit cannot have happened
                raise ErasureJournalCorrupt("pending erasures corrupt at line %d" % number) from err
            if isinstance(entry, dict) and all(k in entry for k in JOURNAL_FIELDS):
                entries.append(entry)
            else:
                raise ErasureJournalCorrupt("pending erasures corrupt at line %d: not an erasure entry" % number)
        return entries

    def _pending_append(self, entry):
        if os.path.exists(self.pending):        # W10b: an unfinished last write is cut first, never glued to
            with open(self.pending, "r+b") as f:
                data = f.read()
                if data and not data.endswith(b"\n"):
                    piece = data[data.rfind(b"\n") + 1:]
                    try:
                        whole = json.loads(piece.decode("utf-8"))
                        whole = isinstance(whole, dict) and all(k in whole for k in JOURNAL_FIELDS)
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        whole = False
                    if whole:                   # round 4: one rule with _pending_read - a whole entry is an intent, kept
                        f.seek(0, os.SEEK_END)
                        f.write(b"\n")
                    else:
                        f.truncate(data.rfind(b"\n") + 1)
                    f.flush()
                    os.fsync(f.fileno())
        with open(self.pending, "ab") as out:
            out.write(_dumps(entry).encode("utf-8") + b"\n")
            out.flush()
            os.fsync(out.fileno())

    def _merge_pending(self):
        """Under the write lock, with a journal: pending entries go to the journal's end WITHOUT moving the anchor, so the
        reconcile that follows applies them to this base. Then the pending file goes."""
        entries = self._pending_read()
        if entries:
            lines, torn = self._journal_read(whole=True)
            lines = self._cut_unfinished_tail(lines, torn, self._anchor())    # the same W5 rule as the reconcile (round 4)
            known = {(line.get("id"), line.get("birth")) for line, _, _, _ in self._journal_check(lines)}
            fresh = [e for e in entries if (e["id"], e["birth"]) not in known]     # by id AND birth: a reused id is another erasure
            if fresh:
                self._journal_append(fresh, move_anchor=False)
        if os.path.exists(self.pending):
            os.remove(self.pending)

    def _journal_sync(self):
        """Under the write lock: the witness state from the disk, not from what this process saw at its open. Returns
        'ok' (the journal is applied to this base), 'missing' or 'legacy'."""
        if not os.path.exists(self.journal):
            if os.path.exists(self.born):
                return "missing"            # the mark says there was a journal: it is gone, workers are refused
            if self._count("records") or self._count("erasure_ledger") or os.path.exists(self.pending):
                return "legacy"             # a base with records and no journal: only an explicit migrate decides
            try:
                self._write_whole(self.journal, b"", exclusive=True)    # a new store; xb: never over another's journal
            except FileExistsError:
                return self._journal_sync()
            self._set_anchor(0, "")
            return "ok"
        self._merge_pending()
        self._reconcile()
        return "ok"

    def _open_journal(self):
        with self._writing():
            state = self._journal_sync()
        if state == "ok" and not os.path.exists(self.born):
            self._write_whole(self.born, b"born\n")
        self._witness = state

    def _reconcile(self):
        """Under the write lock. The file's last line equals the anchor -> nothing to do. Otherwise the full pass."""
        anchor = self._anchor()
        if anchor is not None and self._file_tail() == anchor:
            return
        started = time.perf_counter()
        lines, torn = self._journal_read(whole=True)
        lines = self._cut_unfinished_tail(lines, torn, anchor)
        parsed = self._journal_check(lines)
        last_seq, last_head = (parsed[-1][2], parsed[-1][1]) if parsed else (0, "")
        if anchor is None:
            after = parsed                          # a base that never met this journal: every line is news to it
        elif anchor == (0, ""):
            after = parsed
        elif anchor[0] <= last_seq and parsed[anchor[0] - 1][1] == anchor[1]:
            after = parsed[anchor[0]:]              # the base is an older copy: apply what came after it
        else:
            after = None                            # the journal was rolled back, or is not this base's journal
        union = after is None
        if union:
            self._journal_notes.append("erasure witness: journal behind db" if anchor[0] > last_seq
                                       else "erasure witness: journal does not match db")
            after = parsed                          # union: apply what the journal knows, then add what the base knows
        by_key = None
        for line, _, number, offset in after:
            missing = [k for k in JOURNAL_FIELDS if k not in line]
            if missing:
                raise ErasureJournalCorrupt("erasure journal corrupt at line %d, offset %d: no %s"
                                            % (number, offset, ", ".join(missing)))
            if by_key is None:                  # once per pass: one keyed id per record, not one per record per line
                by_key = {self._journal_id(r["memory_id"]): r["memory_id"]
                          for r in self.db.execute("SELECT memory_id FROM records").fetchall()}
            self._reapply(line, number, by_key)
        self._set_anchor(last_seq, last_head)
        behind = self._ledger_entries(known={(line.get("id"), line.get("birth")) for line, _, _, _ in parsed})
        if behind:
            self._journal_append(behind)
            if not any("journal" in n for n in self._journal_notes):
                self._journal_notes.append("erasure witness: journal behind db")
        spent = time.perf_counter() - started
        if spent > JOURNAL_SLOW_SECONDS:
            LOG.warning("erasure journal: full pass took %.2f s over %d lines", spent, len(parsed))

    @staticmethod
    def _is_journal_line(raw):
        try:
            line = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return False
        return isinstance(line, dict) and type(line.get("seq")) is int

    def _cut_unfinished_tail(self, lines, torn, anchor):
        """W5: lines at the very END of the file that are not journal lines at all, all of them past what this base has
        applied, are an unfinished write (its commit cannot have happened): they are cut, and verify(main) says so. Junk
        followed by a whole line stays corrupt (R3): that is damage, not an unfinished end."""
        keep = len(lines)
        while keep > 0 and not self._is_journal_line(lines[keep - 1][2]):
            keep -= 1
        if keep == len(lines) or anchor is None or keep < anchor[0]:
            return lines                # nothing at the end, or it reaches into lines this base applied: corrupt
        cut_at = lines[keep][1]
        with open(self.journal, "r+b") as f:
            f.truncate(cut_at)
            f.flush()
            os.fsync(f.fileno())
        self._journal_notes.append("erasure witness: %d unfinished line(s) cut at the end of the journal"
                                   % (len(lines) - keep + (1 if torn else 0)))
        return lines[:keep]

    def _record_scope(self, memory_id):
        row = self.db.execute("SELECT scope FROM records WHERE memory_id=?", (memory_id,)).fetchone()
        return row["scope"] if row else "main"

    def _reapply(self, line, number, by_key):
        """One journal line after the anchor. The record is erased again only when its birth matches; its tombstones and
        a keyed mark of its id come back in every case, so neither the text nor the id is given out again. The scope is the
        one at erasure (the line's), not the copy's: a record made private after the copy is erased as private."""
        if not isinstance(line["keys"], list) or not all(
                isinstance(k, list) and len(k) == 2 and all(isinstance(x, str) for x in k) for k in line["keys"]):
            raise ErasureJournalCorrupt("erasure journal corrupt at line %d: keys are not pairs of text" % number)
        if not (isinstance(line["id"], str) and line["id"] and isinstance(line["birth"], str)
                and line["scope"] in ("worker", "main")):
            raise ErasureJournalCorrupt("erasure journal corrupt at line %d: id, birth or scope is not what a line holds" % number)
        evidence = line.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
            raise ErasureJournalCorrupt("erasure journal corrupt at line %d: evidence is not a list of text" % number)
        memory_id = by_key.get(line["id"])
        target = None if memory_id is None else self.db.execute(
            "SELECT memory_id, tombstoned, scope FROM records WHERE memory_id=?", (memory_id,)).fetchone()
        foreign = False
        first = False                           # the first time this line meets this base: only then is evidence dropped
        if target is not None and not target["tombstoned"]:
            if self._journal_birth(target["memory_id"]) == line["birth"]:
                first = True
                scope = "main" if "main" in (line["scope"], target["scope"]) else "worker"
                if scope != target["scope"]:
                    self.db.execute("UPDATE records SET scope=? WHERE memory_id=?", (scope, target["memory_id"]))
                if scope == "main":         # round 5: a private record never holds a worker's evidence row, as correct()
                    self.db.execute("DELETE FROM evidence_refs WHERE memory_id=? AND evidence_id IN "   # does it; the copy's
                                    "(SELECT id FROM evidence WHERE scope='worker')", (target["memory_id"],))   # stale refs go
                self._erase_rows(target["memory_id"], scope, STORE_ACTOR, kind="erase_reapplied")
                self.reapplied += 1
            else:
                self._journal_notes.append("erasure witness: id reused (birth mismatch): %s, journal line %d"
                                           % (target["memory_id"], number))
                target = None                   # another record under the same id: it is not touched, but the erased
                foreign = True                  # text still may not come back (W7); the id stays with the live record
        owner = target["memory_id"] if target is not None else "JRN-" + line["id"][:16]
        now = self.clock()
        added = 0
        for content_key, scope in line["keys"]:
            if line["scope"] == "main":
                scope = "main"                  # a key is never less private than its line
            added += self.db.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?,?,?,?)",
                                     (content_key, scope, owner, now, self._keyed("row", content_key, scope, owner, now))).rowcount
        if target is None:                      # remembered with its birth, so a rebuilt journal keeps it whole (W4, round 3)
            marked = self.db.execute("INSERT OR IGNORE INTO erased_ids VALUES (?,?,?,?)",
                                     (line["id"], line["birth"], line["scope"], _dumps(evidence))).rowcount
            added += marked
            first = first or bool(marked)
        # Round 4: by "first time", not by "not a union". A line this base applied before (a tombstoned record, an id mark
        # already there) finds evidence on disk that main put AGAIN since: it is left alone. A foreign line drops too:
        # the rows it names belong to the erased record, and a row the live record cites keeps its refs and stays.
        if first:
            added += self._drop_erased_evidence(evidence)
        if target is None and not foreign and added:
            self.reapplied += 1

    def _drop_erased_evidence(self, keyed_ids):
        """The evidence rows the erasure removed came back with the folder (W6): those that nothing cites go, and their bytes
        go when no row of those bytes is left. By row, never by whether the bytes lived on elsewhere (W12)."""
        if not keyed_ids:
            return 0
        wanted = set(keyed_ids)
        dropped, shas = 0, set()
        for row in self.db.execute("SELECT id, sha256 FROM evidence").fetchall():
            if self._keyed("journal-evidence", row["id"]) not in wanted:
                continue
            if self.db.execute("SELECT 1 FROM evidence_refs WHERE evidence_id=?", (row["id"],)).fetchone():
                continue                        # a record of this copy still cites it
            self.db.execute("DELETE FROM evidence WHERE id=?", (row["id"],))
            shas.add(row["sha256"])
            dropped += 1
        for sha in shas:
            if not self.db.execute("SELECT 1 FROM evidence WHERE sha256=?", (sha,)).fetchone():
                for folder in (self.archive, self.quarantine):
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(os.path.join(folder, sha))
        return dropped

    def _rebuild_journal(self, expected):
        """Under the write lock. Only if the disk still shows the state this process expects: another process may have
        repaired or migrated meanwhile, and its journal is never deleted. Then the ledger's erasures, then the pending
        ones after the anchor, and the reconcile applies those to this base."""
        state = self._journal_sync()
        if state != expected:
            return state
        entries = self._ledger_entries()
        pending = {(e["id"], e["birth"]): e for e in self._pending_read()}
        for entry in entries:               # the pending line may be the only one that lists the evidence (round 3, #2)
            extra = pending.get((entry["id"], entry["birth"]), {}).get("evidence")
            if extra:
                entry["evidence"] = sorted(set(entry.get("evidence", [])) | set(extra))
        self._write_whole(self.journal, b"", exclusive=True)
        self._set_anchor(0, "")
        if entries:
            self._journal_append(entries)
        self._merge_pending()
        self._reconcile()
        return "ok"

    def migrate_legacy(self, actor, reader_scope="worker"):
        """A base from before the journal: main builds the journal from the erasure ledger. Nothing is erased again unless
        it was erased while there was no journal and a restore brought it back."""
        if reader_scope != "main":
            raise PolicyRefused("only the main scope migrates the store")
        _validate_actor(actor)
        if self._witness != "legacy":
            raise PolicyRefused("not a legacy store: it already has an erasure journal")
        with self._writing():
            state = self._rebuild_journal("legacy")
        if state == "ok" and not os.path.exists(self.born):
            self._write_whole(self.born, b"born\n")
        self._witness = state
        return {"journal_lines": self._anchor()[0], "reapplied": self.reapplied}

    def repair_erasure_journal(self, actor, reader_scope="worker"):
        """The journal is gone and the mark says it existed: main rebuilds it from the ledger of this base and the erasures
        made while it was gone. Erasures known only to the lost journal cannot be recovered by this."""
        if reader_scope != "main":
            raise PolicyRefused("only the main scope repairs the erasure journal")
        _validate_actor(actor)
        if self._witness != "missing":
            raise PolicyRefused("the erasure journal is not missing")
        with self._writing():
            state = self._rebuild_journal("missing")
        self._witness = state                   # after COMMIT (round 5): a repair that rolls back leaves workers refused
        return {"journal_lines": self._anchor()[0]}

    def verify_journal(self, reader_scope="worker"):
        """The full check an ordinary open does not do: structure and chain of every line, the anchor, reused ids."""
        self._reader(reader_scope)
        if reader_scope != "main":
            raise PolicyRefused("the erasure journal belongs to the main scope")
        problems = self._journal_state_problems()
        if self._witness != "ok":
            return problems
        try:
            lines, torn = self._journal_read(whole=True)
            parsed = self._journal_check(lines)
        except ErasureJournalCorrupt as err:
            return problems + [str(err)]
        except OSError:
            return problems + ["erasure witness missing"]
        if torn:
            problems.append("erasure journal: an unfinished last line")
        last = (parsed[-1][2], parsed[-1][1]) if parsed else (0, "")
        if self._anchor() != last:
            problems.append("erasure journal: the last line is not this base's anchor")
        return problems + self._reused_ids(parsed)

    def _reused_ids(self, parsed):
        by_id = {}
        for line, _, number, _ in parsed:
            if isinstance(line.get("id"), str):
                by_id[line["id"]] = (line.get("birth"), number)
        found = []
        for row in self.db.execute("SELECT memory_id FROM records WHERE tombstoned=0").fetchall():
            hit = by_id.get(self._journal_id(row["memory_id"]))
            if hit is not None and hit[0] != self._journal_birth(row["memory_id"]):
                found.append("erasure witness: id reused (birth mismatch): %s" % row["memory_id"])
        return found

    def _journal_state_problems(self):
        if self._witness == "missing":
            return ["erasure witness missing"]
        if self._witness == "legacy":
            return ["legacy store: migrate required"]
        return []

    # ---- integrity ----

    def _verify_chain(self, scope):
        problems, previous, number, label = [], "", 0, "events(%s)" % scope
        for row in self.db.execute("SELECT * FROM events WHERE scope=? ORDER BY seq", (scope,)).fetchall():
            number += 1
            if (row["n"] != number or row["prev_hash"] != previous
                    or _sha(_dumps([row[k] for k in CHAIN_FIELDS])) != row["hash"]):
                problems.append("%s: broken at %d" % (label, number))
            kept = self.db.execute("SELECT * FROM event_keys WHERE seq=?", (row["seq"],)).fetchone()
            if kept is not None:      # a key that is gone from a live record shows up as a document with no event
                expected = hmac.new(kept["key_hex"].encode(), kept["payload"].encode("utf-8"), hashlib.sha256).hexdigest()
                if expected != row["commitment"]:
                    problems.append("%s: commitment broken at %d" % (label, number))
            previous = row["hash"]
        head = self.db.execute("SELECT * FROM chain_head WHERE scope=?", (scope,)).fetchone()
        if (head["n"] if head else 0) != number or (head["hash"] if head else "") != previous:
            problems.append("%s: the tail does not match the recorded head" % label)
        return problems

    def _payloads(self, memory_id):
        result = []
        rows = self.db.execute("SELECT e.kind, e.rollback_version, s.payload FROM events e JOIN event_keys s ON s.seq = e.seq "
                               "WHERE e.memory_id=? ORDER BY e.seq", (memory_id,)).fetchall()
        for row in rows:
            result.append((row["kind"], row["rollback_version"], json.loads(row["payload"])))
        return result

    def _verify_record(self, record):
        memory_id, label = record["memory_id"], "versions"
        erased = self.db.execute("SELECT 1 FROM events WHERE memory_id=? AND kind IN (?,?)", (memory_id,) + ERASE_KINDS).fetchone()
        if bool(record["tombstoned"]) != bool(erased):
            return ["records: a record's erasure does not match the log"]
        if record["tombstoned"]:
            problems = []
            if self.db.execute("SELECT 1 FROM versions WHERE memory_id=?", (memory_id,)).fetchone():
                problems.append("versions: an erased record has text again")
            if self.db.execute("SELECT count(*) FROM erasure_ledger WHERE memory_id=?", (memory_id,)).fetchone()[0] != len(ERASURE_CLASSES):
                problems.append("erasure: the ledger is incomplete")
            return problems
        try:
            payloads = self._payloads(memory_id)
            rows = self.db.execute("SELECT * FROM versions WHERE memory_id=? ORDER BY version", (memory_id,)).fetchall()
            written = {p["version"]: p for k, _, p in payloads if k in ("add", "correct")}
            closings = {r: p for k, r, p in payloads if k == "correct"}
            problems = []
            if [r["version"] for r in rows] != list(range(1, record["current_version"] + 1)):
                problems.append("%s: a version is missing" % label)
            for row in rows:
                entry = written.get(row["version"])
                if entry is None or entry["full_sha"] != _sha(row["doc"]):
                    problems.append("%s: a document does not match its event" % label)
                closing = closings.get(row["version"]) or {"closed_at": None, "closed_valid_to": None}
                if (closing["closed_at"], closing["closed_valid_to"]) != (row["closed_at"], row["closed_valid_to"]):
                    problems.append("%s: a closing does not match its event" % label)
            for evidence_id in json.loads(rows[-1]["doc"])["evidence_ids"] if rows else []:
                if not self.db.execute("SELECT 1 FROM evidence WHERE id=?", (evidence_id,)).fetchone():
                    problems.append("evidence: a cited row is missing")
                if not self.db.execute("SELECT 1 FROM evidence_refs WHERE evidence_id=? AND memory_id=?",
                                       (evidence_id, memory_id)).fetchone():
                    problems.append("evidence: a citation has lost its reference")
            if written and max(written) != record["current_version"]:
                problems.append("%s: the newest logged version is not the current one" % label)
            last = written.get(record["current_version"])
            if last is not None and last["record_scope"] != record["scope"]:
                problems.append("records: the scope does not match the log")
            return problems
        except READ_ERRORS:
            return ["%s: unreadable" % label]

    def _verify_rows(self, reader_scope):
        """Evidence and tombstone rows carry a keyed check, and their counts are kept: a lost row cannot go unnoticed."""
        problems = []
        where = "" if reader_scope == "main" else " WHERE scope='worker'"
        for row in self.db.execute("SELECT * FROM evidence" + where).fetchall():
            expected = self._keyed("row", row["id"], row["sha256"], row["source"], row["channel"], row["actor_id"],
                                   row["actor_role"], row["span"], row["scope"])
            if expected != row["mac"]:
                problems.append("evidence: a row does not match its check")
        for row in self.db.execute("SELECT * FROM tombstones" + where).fetchall():
            if self._keyed("row", row["content_key"], row["scope"], row["memory_id"], row["at"]) != row["mac"]:
                problems.append("tombstones: a row does not match its check")
        if reader_scope == "main":
            if not self._counts_match():
                problems.append("evidence or tombstone rows are missing")
            if self.db.execute("SELECT 1 FROM evidence_refs WHERE memory_id NOT IN (SELECT memory_id FROM records) "
                               "OR evidence_id NOT IN (SELECT id FROM evidence)").fetchone():
                problems.append("evidence: a reference points at nothing")
            if self.db.execute("SELECT 1 FROM versions WHERE memory_id NOT IN (SELECT memory_id FROM records)").fetchone():
                problems.append("versions: a version of no record")
            states = ("pending", KEPT_PENDING, "done", KEPT_DONE)
            if self.db.execute("SELECT 1 FROM erasure_ledger WHERE state NOT IN (?,?,?,?)", states).fetchone():
                problems.append("erasure: an unknown ledger state")
        return problems

    def _verify_records(self, reader_scope):
        problems = []
        where = "" if reader_scope == "main" else " WHERE scope='worker'"
        records = self.db.execute("SELECT * FROM records" + where).fetchall()
        for record in records:
            problems.extend(self._verify_record(record))
        known = {r["memory_id"] for r in self.db.execute("SELECT memory_id FROM records").fetchall()}     # every scope: a worker
        chain = "" if reader_scope == "main" else " WHERE scope='worker'"                                   # record may have gone private
        for row in self.db.execute("SELECT DISTINCT memory_id FROM events" + chain).fetchall():
            if row["memory_id"] not in known:
                problems.append("records: the log names a record that is gone")
        return problems

    def _verify_indexes(self, reader_scope):
        """Only an index of the current generation can be judged: any other is a writer mid-way, or the repair on open. This
        counts rows; edited text inside a rebuildable index is not looked for."""
        problems, scopes = [], ("worker", "main") if reader_scope == "main" else ("worker",)
        for scope in scopes:
            name = LEXICAL[scope]
            try:
                with self._lex(name) as con:
                    con.execute("BEGIN")        # the generation and the count from one view of the file
                    stamp = con.execute("SELECT value FROM gen").fetchone()
                    held = con.execute("SELECT count(*) FROM lex").fetchone()[0]
            except sqlite3.Error:
                problems.append("index: %s cannot be read" % scope)
                continue
            if stamp is None or stamp[0] != self._meta("generation"):
                continue
            where = "tombstoned=0" + (" AND scope='worker'" if scope == "worker" else "")
            expected = self.db.execute("SELECT count(*) FROM records WHERE " + where).fetchone()[0]
            if held != expected:
                problems.append("index: %s does not match the records" % scope)
        if reader_scope == "main" and self._meta("rebuild_needed"):
            problems.append("index: a rebuild is pending")
        return problems

    def verify(self, reader_scope="worker"):
        """A list of problems; empty means intact. A worker hears how many blobs are damaged, never which."""
        self._reader(reader_scope)
        self.db.execute("BEGIN")       # one snapshot: a writer elsewhere must not look like damage
        try:
            if reader_scope == "main":      # the witness first: without it nothing below can be trusted about erasures
                unborn = self.db.execute("SELECT count(*) FROM erased_ids WHERE birth=''").fetchone()[0]
                marks = ["erasure witness: %d id mark(s) without a birth (migrated from an older base)" % unborn] if unborn else []
                if self._witness == "ok" and not self._journal_matches_base():
                    marks.append("erasure witness: journal ahead of db - an erasure main ordered is not applied here; reopen")
                return (self._journal_state_problems() + list(dict.fromkeys(self._journal_notes)) + marks
                        + self._verify_snapshot("main"))
            return self._verify_snapshot(reader_scope)
        except READ_ERRORS:
            return ["store: a stored value cannot be read"]
        finally:
            self.db.execute("ROLLBACK")

    def _verify_snapshot(self, reader_scope):
        problems, damaged = [], []
        for row in self.db.execute("SELECT DISTINCT sha256 FROM evidence").fetchall():
            try:
                with open(os.path.join(self.archive, row["sha256"]), "rb") as blob:
                    if hashlib.sha256(blob.read()).hexdigest() != row["sha256"]:
                        damaged.append("archive: changed %s" % row["sha256"])
            except OSError:
                damaged.append("archive: unreadable %s" % row["sha256"])
        if reader_scope == "main":
            problems.extend(damaged)
        elif damaged:
            problems.append("archive: %d damaged" % len(damaged))
        problems.extend(self._verify_chain("worker"))
        if reader_scope == "main":
            problems.extend(self._verify_chain("main"))
        problems.extend(self._verify_records(reader_scope))
        problems.extend(self._verify_rows(reader_scope))
        problems.extend(self._verify_indexes(reader_scope))
        pending = ("SELECT 1 FROM erasure_ledger l JOIN records r ON r.memory_id = l.memory_id WHERE l.state='pending'"
                   + ("" if reader_scope == "main" else " AND r.scope='worker'"))
        if self.db.execute(pending).fetchone():
            problems.append("erasure: pending")
        return problems


class ReadOnlyStore(Store):
    """A store opened only to read, for a tool that must leave every byte as it was (the golden runner). The database
    and both indexes are opened with mode=ro, so a write is an error, never a write. Nothing an ordinary open does is done
    here: a store that would need a repair, a rebuild, a migration, a journal sync or a finished erasure is refused with
    StoreNeedsRepair, never repaired - open it once with Store first. Search and get work as in Store.
    author 11.11.2026, control round on the golden runner (wf_f1a3eafa)."""
    _lex_mode = "ro"

    def __init__(self, root):            # noqa: super().__init__ is the writing open: deliberately not called
        self.root = os.path.abspath(root)
        if self.root.startswith("\\\\?\\") and not self.root.startswith("\\\\?\\UNC\\"):
            self.root = self.root[4:]
        self.clock = _utc_now
        self.journal = self.root + JOURNAL_SUFFIX
        self.born = os.path.join(self.root, BORN_FILE)
        self.pending = self.root + PENDING_SUFFIX
        self._witness = None
        self._journal_notes = []
        self.reapplied = 0
        database = os.path.join(self.root, "memory.sqlite")
        if not os.path.isfile(database) or os.path.getsize(database) == 0:
            raise StoreNeedsRepair("no database")
        try:                                # read, never created: _load_salt would write a new one
            with open(os.path.join(self.root, SALT_FILE), encoding="ascii") as existing:
                self.salt = existing.read().strip()
        except (OSError, UnicodeDecodeError):
            self.salt = ""
        if len(self.salt) != 64:
            raise StoreNeedsRepair("the tombstone salt is missing or damaged")
        self.db = sqlite3.connect(pathlib.Path(database).as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            problems = self._needs()
        except sqlite3.Error:
            problems = ["the database cannot be read"]
        if problems:
            self.db.close()
            raise StoreNeedsRepair("; ".join(problems))
        self._witness = "ok"

    def _needs(self):
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"meta", "records", "versions", "erased_ids", "erasure_ledger"} - tables
        if missing:
            return ["schema: %s missing" % ", ".join(sorted(missing))]
        problems = []
        if "birth" not in [r["name"] for r in self.db.execute("PRAGMA table_info(erased_ids)")]:
            problems.append("erased_ids needs its migration")
        if self._meta("rebuild_needed"):
            problems.append("an index rebuild is pending")
        for name in LEXICAL.values():
            if not self._index_is_current(name):        # p62: this also reads lex, not only the gen table
                problems.append("index %s is not current" % name)
        if not os.path.exists(self.born):
            problems.append("the erasure journal is not born here (legacy or missing)")
        if os.path.exists(self.pending):
            problems.append("erasures wait in the pending file")
        if not self._journal_matches_base():
            problems.append("the erasure journal does not match this base")
        if self.db.execute("SELECT 1 FROM erasure_ledger WHERE state='pending'").fetchone():
            problems.append("an erasure is not finished")
        return problems
