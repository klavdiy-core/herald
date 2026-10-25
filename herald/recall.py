"""Search past sessions: an SQLite FTS5 index over what was said (the human's messages and the model's text), no
model, no cost. Tool outputs are not indexed (they hold file contents and secrets). Indexing is incremental: a file
is read from the line where the last run stopped. Idea from Hermes Agent's session search (Nous Research, MIT).
"""
import json
import os
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, lines INTEGER, size INTEGER);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session TEXT, role TEXT, ts TEXT, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, content='messages', content_rowid='id',
                                                 tokenize='unicode61 remove_diacritics 2');
"""


def _connect(db):
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    return con


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


def index(db, folder):
    """Index new lines of every *.jsonl under folder. Returns how many messages were added."""
    if not os.path.isdir(folder):
        return 0
    con, added = _connect(db), 0
    try:
        for dirpath, _, names in os.walk(folder):
            for name in sorted(n for n in names if n.endswith(".jsonl")):
                path = os.path.join(dirpath, name)
                size = os.path.getsize(path)
                row = con.execute("SELECT lines, size FROM files WHERE path=?", (path,)).fetchone()
                done = row[0] if row and size >= row[1] else 0       # a file that shrank was rewritten: start over
                session, n = name[:-6], 0
                if row and not done:                                  # ...and its old messages go (they were duplicated)
                    for mid, old in con.execute("SELECT id, text FROM messages WHERE session=?", (session,)).fetchall():
                        con.execute("INSERT INTO fts(fts, rowid, text) VALUES ('delete', ?, ?)", (mid, old))
                    con.execute("DELETE FROM messages WHERE session=?", (session,))
                with open(path, encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if not line.endswith("\n"):
                            n -= 1                      # still being written: read it next time, once it is whole
                            break
                        if n <= done:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        said = _said(rec)
                        if said:
                            # a lone surrogate (half an emoji) cannot be stored: replace it, keep the message
                            t = said[1][:4000].encode("utf-8", "replace").decode("utf-8")
                            cur = con.execute("INSERT INTO messages(session, role, ts, text) VALUES (?,?,?,?)",
                                              (session, said[0], str(rec.get("timestamp", ""))[:19], t))
                            con.execute("INSERT INTO fts(rowid, text) VALUES (?,?)", (cur.lastrowid, t))
                            added += 1
                con.execute("INSERT OR REPLACE INTO files(path, lines, size) VALUES (?,?,?)", (path, max(n, done), size))
        con.commit()
    finally:
        con.close()
    return added


def search(db, query, limit=5):
    """Messages matching every word of the query, best first, each with the next message of its session."""
    words = re.findall(r"\w+", query or "")
    if not words or not os.path.exists(db):
        return []
    q = " ".join('"' + w.replace('"', "") + '"' for w in words[:12])     # only quoted words reach FTS5 syntax
    con = _connect(db)
    try:
        rows = con.execute("SELECT m.id, m.session, m.role, m.ts, m.text FROM fts JOIN messages m ON m.id = fts.rowid "
                           "WHERE fts MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        out = []
        for mid, session, role, ts, txt in rows:
            nxt = con.execute("SELECT role, text FROM messages WHERE session=? AND id>? ORDER BY id LIMIT 1",
                              (session, mid)).fetchone()
            body = txt[:400] + (f"\n→ {nxt[0]}: {nxt[1][:400]}" if nxt else "")
            out.append({"session": session, "role": role, "ts": ts, "text": body})
        return out
    finally:
        con.close()


def render(hits, query):
    if not hits:
        return f"Nothing in past sessions matches «{query}»."
    return "\n\n".join(f"{h['ts'][:16].replace('T', ' ')} · {h['role']}: {h['text']}" for h in hits)
