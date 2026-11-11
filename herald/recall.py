"""Search past sessions: an SQLite FTS5 index over what was said (the human's messages and the model's text), no
model, no cost. Tool outputs are not indexed (they hold file contents and secrets). Indexing is incremental: a file
is read from the line where the last run stopped. Idea from Hermes Agent's session search (Nous Research, MIT).

Known limits (reviewed, not fixed):
- A worker's index keeps the name of a file closed by one labelled line, its closed mark and an unkeyed hash of it;
  readable only by someone who can already open the file itself.
- Words left in free pages by a forget under code older than secure_delete stay until a later run drops something; the
  first update after upgrading does not clear them. A one-off clean is needed before the first private row.
"""
import hashlib
import json
import os
import sqlite3
import unicodedata

from .memory import sources as memory_sources
from .memory.store import _tokens

READERS = ("worker", "main")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, lines INTEGER, size INTEGER, labels TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session TEXT, role TEXT, ts TEXT, text TEXT,
                                     scope TEXT NOT NULL DEFAULT 'worker', source TEXT NOT NULL DEFAULT '');
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, content='messages', content_rowid='id',
                                                 tokenize='unicode61 remove_diacritics 2');
"""


def _connect(db):
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA secure_delete=ON")      # before anything is dropped
    columns = [c[1] for c in con.execute("PRAGMA table_info(messages)")]
    if columns and "source" not in columns:     # an index from before scopes held everything: it can only be rebuilt
        con.executescript("DROP TABLE IF EXISTS fts; DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS files;")
    con.executescript(SCHEMA)
    return con


def _connect_read_only(db):
    """No schema, no pragma that writes: a write through this connection is an error. An index from before scopes (no
    source column) is refused here, where _connect would drop it."""
    con = sqlite3.connect(__import__("pathlib").Path(os.path.abspath(db)).as_uri() + "?mode=ro", uri=True, timeout=60)
    try:
        if "source" not in [c[1] for c in con.execute("PRAGMA table_info(messages)")]:
            raise sqlite3.DatabaseError("not a current recall index")
    except BaseException:
        con.close()
        raise
    return con


def _built_under(con):
    row = con.execute("SELECT value FROM meta WHERE key='sources'").fetchone()
    return row[0] if row else None


def _wipe(con):
    con.execute("INSERT INTO fts(fts) VALUES ('delete-all')")
    con.execute("DELETE FROM messages")
    con.execute("DELETE FROM files")


def _drop_file(con, path):
    """Everything indexed from this file. By path: two files in different folders can share a name."""
    rows = con.execute("SELECT id, text FROM messages WHERE source=?", (path,)).fetchall()
    for mid, old in rows:
        con.execute("INSERT INTO fts(fts, rowid, text) VALUES ('delete', ?, ?)", (mid, old))
    con.execute("DELETE FROM messages WHERE source=?", (path,))
    return bool(rows)


def _forget_vanished(con, also):
    """Files gone from disk, wherever they were indexed from, plus `also` (seen by this run as moved or closed)."""
    dropped = False
    for (path,) in con.execute("SELECT path FROM files").fetchall():
        if path in also or not os.path.exists(path):
            dropped = _drop_file(con, path) or dropped
            con.execute("DELETE FROM files WHERE path=?", (path,))
    return dropped


def _labels(known, path, upto):
    """(does anything close the file, its state now, its state when `upto` lines had been read). Any record a worker may not
    read closes whatever in this file has no label of its own (a reply quoting it). The state covers the labels and the
    text of the whole lines, so a line rewritten in place is noticed even when the file did not shrink."""
    closes, content, then, count = False, hashlib.sha256(), "", 0
    if upto == 0:
        then = content.hexdigest()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            closes = closes or any(known.scope_of(path, label) == "main" for label in memory_sources.line_labels(line))
            if not line.endswith("\n"):
                break
            content.update(line.encode("utf-8", "replace"))
            count += 1
            if count == upto:
                then = content.hexdigest()
    mark = "%d:" % closes
    return closes, mark + content.hexdigest(), mark + then


def _check_scope(reader_scope):
    if reader_scope not in READERS:
        raise ValueError("unknown reader scope")


def scope_notice(reader_scope="worker"):
    """One fixed line per scope. A worker's answer never says that something was withheld: that would tell it there was."""
    _check_scope(reader_scope)
    return "Searches everything." if reader_scope == "main" else "Searches what this scope may read; private memory is excluded."


def _said(rec):
    """(role, text) for a record that holds something a person or the model said, else None."""
    if not isinstance(rec, dict) or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    att = rec.get("attachment")
    if rec.get("type") == "attachment" and isinstance(att, dict) and att.get("type") == "queued_command":
        t = str(att.get("prompt") or "").strip()     # a message sent mid-turn is stored as an attachment
        return ("human", t) if t and not t.startswith("<") else None
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    content = msg.get("content")
    if rec.get("type") == "user":
        if isinstance(content, str):
            t = content
        elif isinstance(content, list):
            t = "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            return None
        t = t.strip()
        return ("human", t) if t and not t.startswith("<") else None
    if rec.get("type") == "assistant" and isinstance(content, list):
        t = "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
        return ("model", t) if t else None
    return None


def index(db, folder, reader_scope="worker"):
    """Index new lines of every *.jsonl under folder. Returns how many messages were added. A worker's index never receives
    private text: the database on disk is itself a copy. The main scope needs its own database."""
    _check_scope(reader_scope)
    if not os.path.isdir(folder):
        if os.path.exists(db):
            con = _connect(db)
            try:
                con.execute("BEGIN IMMEDIATE")      # as below: rows read outside the write lock may be gone when the delete runs
                if _forget_vanished(con, set()):
                    con.execute("INSERT INTO fts(fts) VALUES ('optimize')")     # as below: a deleted row stays in its segment until a merge
                con.commit()
            finally:
                con.close()
        return 0
    folder = os.path.realpath(folder)        # one spelling, or the same folder would be indexed twice
    known = memory_sources.load()
    con, added, dropped, seen = _connect(db), 0, False, set()
    try:
        con.execute("BEGIN IMMEDIATE")      # from the first read of `files`: a second indexer waits and then finds nothing new
        if _built_under(con) != known.fingerprint():
            dropped = con.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is not None
            _wipe(con)                      # the list changed: a scope stamped under the old one means nothing now
            con.execute("INSERT OR REPLACE INTO meta VALUES ('sources', ?)", (known.fingerprint(),))
        for dirpath, _, names in os.walk(folder):
            for name in sorted(n for n in names if n.endswith(".jsonl")):
                path = os.path.join(dirpath, name)
                try:
                    path.encode("utf-8")
                except UnicodeEncodeError:
                    continue                # a name SQLite cannot hold must not cost the rest of the run
                if reader_scope == "worker" and known.scope_of(path) == "main":
                    continue                # not even its name reaches a worker's index; a row from before goes below
                seen.add(path)
                size = os.path.getsize(path)
                row = con.execute("SELECT lines, size, labels FROM files WHERE path=?", (path,)).fetchone()
                closes, digest, then = _labels(known, path, row[0] if row else 0)
                done = row[0] if row and size >= row[1] and row[2] == then else 0       # shrank or rewritten: start over
                session, n = name[:-6], 0
                if row and not done:                                  # ...and its old messages go (they were duplicated)
                    dropped = _drop_file(con, path) or dropped
                with open(path, encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if not line.endswith("\n"):
                            n -= 1                      # still being written: read it next time, once it is whole
                            break
                        if n <= done:
                            continue
                        try:
                            rec = json.loads(line.lstrip("\ufeff"))
                            said = _said(rec)
                        except (ValueError, TypeError, AttributeError, RecursionError):
                            continue            # one odd line never costs the rest of the run
                        labels = memory_sources.line_labels(line)       # every label on the line, not only its top one
                        if labels:
                            scope = "main" if any(known.scope_of(path, x) == "main" for x in labels) else "worker"
                        else:
                            scope = "main" if closes else known.scope_of(path)
                        if said and (reader_scope == "main" or scope == "worker"):
                            # a lone surrogate (half an emoji) cannot be stored: replace it, keep the message
                            t = unicodedata.normalize("NFC", said[1][:4000].encode("utf-8", "replace").decode("utf-8"))
                            cur = con.execute("INSERT INTO messages(session, role, ts, text, scope, source) VALUES (?,?,?,?,?,?)",
                                              (session, said[0], str(rec.get("timestamp", ""))[:19].encode("utf-8", "replace").decode("utf-8"),
                                               t, scope, path))
                            con.execute("INSERT INTO fts(rowid, text) VALUES (?,?)", (cur.lastrowid, t))
                            added += 1
                con.execute("INSERT OR REPLACE INTO files(path, lines, size, labels) VALUES (?,?,?,?)",
                            (path, max(n, done), size, digest))
        under = os.path.join(folder, "")
        dropped = _forget_vanished(con, {p for (p,) in con.execute("SELECT path FROM files") if p.startswith(under) and p not in seen}) or dropped
        if dropped:
            con.execute("INSERT INTO fts(fts) VALUES ('optimize')")     # a deleted row stays in its segment until a merge
        con.commit()
    finally:
        con.close()
    return added


def search(db, query, limit=5, reader_scope="worker", with_source=False, read_only=False):
    """Messages matching every word of the query, best first, each with the next message of its session. A worker's search
    of a main database still returns only what a worker may read, and the following message is held to the same rule.
    with_source=True adds the full path of the session file to each hit (two files may share a session name).
    read_only=True opens the index with mode=ro and creates or drops nothing: an index that is not a current one
    raises sqlite3.Error instead of being rebuilt (the golden runner)."""
    _check_scope(reader_scope)
    only = "" if reader_scope == "main" else " AND m.scope='worker'"
    words = _tokens(query or "")            # the same split as the index, or a glued symbol can never be found
    if not words or not os.path.exists(db):
        return []
    q = " ".join('"' + w + '"' for w in words[:12])     # only quoted words reach FTS5 syntax; a quote is always a separator
    con = _connect_read_only(db) if read_only else _connect(db)
    try:
        if reader_scope == "worker" and _built_under(con) != memory_sources.load().fingerprint():
            return []                       # built under another list: until it is re-indexed, it proves nothing
        rows = con.execute("SELECT m.id, m.session, m.role, m.ts, m.text, m.source FROM fts JOIN messages m ON m.id = fts.rowid "
                           "WHERE fts MATCH ?" + only + " ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        out = []
        for mid, session, role, ts, txt, source in rows:
            nxt = con.execute("SELECT role, text FROM messages m WHERE session=? AND id>?" + only + " ORDER BY id LIMIT 1",
                              (session, mid)).fetchone()
            body = txt[:400] + (f"\n→ {nxt[0]}: {nxt[1][:400]}" if nxt else "")
            out.append({"session": session, "role": role, "ts": ts, "text": body})
            if with_source:
                out[-1]["source"] = source
        return out
    finally:
        con.close()


def render(hits, query):
    if not hits:
        return f"Nothing in past sessions matches «{query}»."
    return "\n\n".join(f"{h['ts'][:16].replace('T', ' ')} · {h['role']}: {h['text']}" for h in hits)
