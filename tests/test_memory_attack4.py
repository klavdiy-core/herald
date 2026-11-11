"""Confirmed defects from a fourth independent attack on the memory core and its readers."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from herald import learning, notebook, recall, team  # noqa: E402
from herald.memory import PolicyRefused, Store, ValidationError, sources  # noqa: E402
from herald.notebook import Notebook  # noqa: E402
from test_memory_core import OP, Base, rec  # noqa: E402

W = {"id": "ENT-w", "role": "worker"}
MAIN = {"reader_scope": "main"}
TOOL = {"authorship_channel": "tool_output"}
ENV = sources.ENV


def line(text, role="user", **extra):
    if role == "user":
        body = {"type": "user", "timestamp": "2026-01-01T00:00:00", "message": {"content": text}}
    else:
        body = {"type": "assistant", "timestamp": "2026-01-01T00:00:01", "message": {"content": [{"type": "text", "text": text}]}}
    body.update(extra)
    return json.dumps(body) + "\n"


class Env(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp())
        os.environ.pop(ENV, None)
        self.addCleanup(os.environ.pop, ENV, None)

    def roots(self, *pairs):
        path = os.path.join(self.d, "sources.json")
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"roots": [{"path": p, "class": c} for p, c in pairs]}, out)
        os.environ[ENV] = path

    def session(self, name, *lines, folder="s"):
        target = os.path.join(self.d, folder)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, name), "a", encoding="utf-8") as out:
            out.writelines(lines)
        return target

    @property
    def db(self):
        return os.path.join(self.d, "db", "r.sqlite")


class Sources(Env):
    def test_a_junction_under_an_open_root_stays_closed(self):
        closed, opened = os.path.join(self.d, "closed"), os.path.join(self.d, "open")
        os.makedirs(closed)
        os.makedirs(opened)
        link = os.path.join(opened, "j")
        subprocess.run(["cmd", "/c", "mklink", "/J", link, closed], capture_output=True, check=True)
        self.roots((closed, "main-only"), (opened, "team"))
        with open(os.path.join(closed, "t.jsonl"), "w", encoding="utf-8") as out:
            out.write(line("ZQJUNCTION words"))
        self.assertEqual(sources.load().class_of(os.path.join(link, "t.jsonl")), "main-only")
        recall.index(self.db, opened)
        self.assertEqual(recall.search(self.db, "ZQJUNCTION"), [])

    def test_empty_and_relative_roots_are_a_broken_list(self):
        for bad in ("", "docs"):
            with self.subTest(bad):
                self.roots((bad, "public"))
                self.assertEqual(sources.load().class_of(os.path.join(os.getcwd(), "x.jsonl")), "main-only")

    def test_a_deep_list_is_a_broken_list(self):
        path = os.path.join(self.d, "deep.json")
        with open(path, "w", encoding="utf-8") as out:
            out.write("[" * 200000)
        os.environ[ENV] = path
        self.assertEqual(sources.load().class_of(os.path.join(self.d, "x")), "main-only")

    def test_a_null_or_non_text_label_is_closed(self):
        known = sources.load()
        for bad in (None, ["public"], {"a": 1}, 5):
            with self.subTest(repr(bad)):
                self.assertEqual(known.scope_of(os.path.join(self.d, "x"), bad), "main")

    def test_no_label_at_all_stays_open(self):
        self.assertEqual(sources.load().scope_of(os.path.join(self.d, "x")), "worker")


class Recall(Env):
    def test_a_root_closed_later_is_gone_for_a_worker(self):
        folder = self.session("a.jsonl", line("ZQLATER words"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQLATER"))
        self.roots((folder, "main-only"))
        self.assertEqual(recall.search(self.db, "ZQLATER"), [], "before any re-index")
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQLATER"), [])
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_an_index_from_before_scopes_opens_nothing(self):
        os.makedirs(os.path.dirname(self.db))
        with sqlite3.connect(self.db) as con:
            con.executescript("""CREATE TABLE files (path TEXT PRIMARY KEY, lines INTEGER, size INTEGER);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session TEXT, role TEXT, ts TEXT, text TEXT);
                CREATE VIRTUAL TABLE fts USING fts5(text, content='messages', content_rowid='id');
                INSERT INTO messages(id, session, role, ts, text) VALUES (1, 's', 'human', 't', 'ZQOLDROW words');
                INSERT INTO fts(rowid, text) VALUES (1, 'ZQOLDROW words');""")
        self.assertEqual(recall.search(self.db, "ZQOLDROW"), [])

    def test_a_reply_after_a_private_line_is_closed(self):
        folder = self.session("a.jsonl", line("ZQPRIV asked", sensitivity="private"), line("ZQREPLY noted", role="model"))
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQREPLY"), [])

    def test_a_private_line_closes_what_was_already_indexed_unlabelled(self):
        folder = self.session("a.jsonl", line("ZQEARLIER words"))
        recall.index(self.db, folder)
        self.session("a.jsonl", line("x", sensitivity="private"))
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQEARLIER"), [])

    def test_labelled_public_lines_stay_beside_a_private_one(self):
        folder = self.session("a.jsonl", line("ZQPUB words", sensitivity="public"), line("x", sensitivity="private"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "ZQPUB"))

    def test_a_bad_label_does_not_stop_the_run(self):
        folder = self.session("a.jsonl", line("ZQGOOD words"), line("ZQBAD words", sensitivity=["x"]), line("ZQNULL words", sensitivity=None))
        recall.index(self.db, folder)
        self.assertEqual(recall.search(self.db, "ZQBAD"), [])
        self.assertEqual(recall.search(self.db, "ZQNULL"), [])

    def test_a_decomposed_query_finds_the_composed_word(self):
        folder = self.session("a.jsonl", line("йодза words"))
        recall.index(self.db, folder)
        self.assertTrue(recall.search(self.db, "йодза"))

    def test_two_indexers_do_not_duplicate(self):
        folder = self.session("a.jsonl", *[line("ZQMANY %d" % i) for i in range(3000)])
        threads = [threading.Thread(target=recall.index, args=(self.db, folder)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM messages").fetchone()[0], 3000)


class Notebooks(Env):
    def nb(self):
        return Notebook(os.path.join(self.d, "nb"))

    def skill(self, name, label=None, section="## One\nA\n"):
        extra = "sensitivity: %s\n" % label if label else ""
        return "---\nname: %s\ndescription: d\n%s---\n\n%s" % (name, extra, section)

    def test_a_crlf_separator_cannot_split_a_private_entry(self):
        nb = self.nb()
        nb.add_memory("[sensitivity:private] part one\r\n§\r\nZQSPLIT second")
        self.assertNotIn("ZQSPLIT", " ".join(nb.memory()))
        self.assertNotIn("ZQSPLIT", nb.compose("P"))

    def test_a_private_draft_cannot_merge_into_a_public_skill(self):
        nb = self.nb()
        nb.add_skill(self.skill("x", "public"))
        with self.assertRaises(ValueError):
            nb.add_skill(self.skill("x", "private", "## Two\nZQMERGED\n"))
        self.assertNotIn("ZQMERGED", nb.compose("P"))

    def test_a_draft_with_the_same_label_still_merges(self):
        nb = self.nb()
        nb.add_skill(self.skill("x", "public"))
        nb.add_skill(self.skill("x", "public", "## Two\nZQMORE\n"))
        self.assertIn("ZQMORE", nb.skills()["x"])

    def test_loose_spellings_of_a_label_do_not_open_a_memory_entry(self):
        for entry in ("﻿[sensitivity:private] ZQLOOSE", "[Sensitivity:private] ZQLOOSE", "[ sensitivity : private ] ZQLOOSE"):
            with self.subTest(entry):
                nb = Notebook(tempfile.mkdtemp(dir=self.d))
                with open(nb.mem_path, "w", encoding="utf-8") as out:
                    out.write(entry + "\n")
                self.assertEqual(nb.memory(), [])

    def test_loose_spellings_of_a_label_do_not_open_a_skill(self):
        heads = ("sensitivity: private # keep", "  sensitivity: private", "Sensitivity: private", "sensitivity: 'private'")
        for head in heads:
            with self.subTest(head):
                nb = Notebook(tempfile.mkdtemp(dir=self.d))
                folder = os.path.join(nb.dir, "skills", "s")
                os.makedirs(folder)
                with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as out:
                    out.write("---\nname: s\n%s\n---\n\nbody\n" % head)
                self.assertEqual(nb.skills(), {})

    def test_a_skill_with_a_bom_keeps_its_label(self):
        nb = self.nb()
        folder = os.path.join(nb.dir, "skills", "s")
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as out:
            out.write("﻿---\nname: s\nsensitivity: private" "\n---\n\nbody\n")
        self.assertEqual(nb.skills(), {})

    def test_a_public_label_does_not_override_a_main_only_root(self):
        nb = self.nb()
        os.makedirs(nb.dir)
        with open(nb.mem_path, "w", encoding="utf-8") as out:
            out.write("[sensitivity:public] ZQROOT\n")
        self.roots((nb.dir, "main-only"))
        self.assertEqual(nb.memory(), [])


class Team(Env):
    def test_a_closed_session_gives_no_lesson_on_disk(self):
        folder = os.path.join(self.d, "projects")
        os.makedirs(folder)
        with open(os.path.join(folder, "a.jsonl"), "w", encoding="utf-8") as out:
            out.write(json.dumps({"type": "custom-title", "customTitle": "zqrole"}) + "\n")
            out.write(line("x", sensitivity="private"))
        result = team.run(folder, os.path.join(self.d, "out"))
        self.assertEqual(list(result["roles"]), [])


class Store4(Base):
    def forge(self, *sql):
        con = sqlite3.connect(os.path.join(self.root, "memory.sqlite"))
        for statement in sql:
            con.execute(statement)
        con.commit()
        con.close()

    def lex(self, name, statement):
        con = sqlite3.connect(os.path.join(self.root, name))
        con.execute(statement)
        con.commit()
        con.close()

    def worker_ev(self, data=b"worker bytes", source="w.md"):
        return self.s.put_evidence(data, source, "tool_output", W)

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, clock=self.clock)

    def test_a_later_write_does_not_forget_a_lost_tombstone(self):
        mid = self.s.add(rec("gone"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.forge("DELETE FROM tombstones")
        self.s.add(rec("other"), "x")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_later_write_does_not_cost_the_blob_of_a_lost_row(self):
        e = self.worker_ev()
        self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x")
        self.forge("DELETE FROM evidence WHERE id='%s'" % e)
        self.s.add(rec("other"), "x")
        self.reopen()
        self.assertEqual(len(os.listdir(os.path.join(self.root, "archive"))), 1)
        self.assertTrue(self.s.verify(**MAIN))

    def test_an_erase_elsewhere_does_not_cost_that_blob_either(self):
        e = self.worker_ev()
        self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x")
        victim = self.s.add(rec("victim"), "x")
        self.forge("DELETE FROM evidence WHERE id='%s'" % e)
        self.s.erase(victim, "asked", OP, **MAIN)
        self.assertEqual(len(os.listdir(os.path.join(self.root, "archive"))), 1)

    def test_a_missing_version_does_not_stop_the_store(self):
        broken = self.s.add(rec("broken"), "x")
        other = self.s.add(rec("other"), "x")
        self.forge("DELETE FROM versions WHERE memory_id='%s'" % broken)
        self.s.rebuild()
        self.s.erase(other, "asked", OP, **MAIN)
        self.reopen()
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_failed_catch_up_is_not_stamped_current(self):
        self.s.add(rec("zqolder words"), "x")
        self.lex("lexical_main.sqlite", "DELETE FROM lex")
        self.lex("lexical_main.sqlite", "UPDATE gen SET value='stale'")
        real = self.s._write_index

        def refuse(name, docs):
            raise PermissionError("held open")

        self.s._write_index = refuse
        self.s.add(rec("zqnewer words"), "x")
        self.assertTrue(self.s.verify(**MAIN), "a stale index must be reported while it is stale")
        self.s._write_index = real
        self.reopen()
        self.assertEqual(self.s.search("zqolder", **MAIN)["outcome"], "found")

    def test_lost_refs_are_reported(self):
        e = self.worker_ev()
        self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x")
        self.forge("DELETE FROM evidence_refs")
        self.assertTrue(self.s.verify(**MAIN))

    def test_erased_text_put_back_is_reported(self):
        mid = self.s.add(rec("gone"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.forge("INSERT INTO versions VALUES ('%s', 1, '{}', NULL, NULL)" % mid)
        self.assertTrue(self.s.verify(**MAIN))

    def test_lost_ledger_rows_are_reported(self):
        mid = self.s.add(rec("gone"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.forge("DELETE FROM erasure_ledger")
        self.assertTrue(self.s.verify(**MAIN))

    def test_a_deleted_worker_record_is_reported_to_a_worker(self):
        mid = self.s.add(rec("x"), "x")
        self.forge("DELETE FROM records WHERE memory_id='%s'" % mid, "DELETE FROM versions WHERE memory_id='%s'" % mid)
        self.assertTrue(self.s.verify())

    def test_a_record_gone_private_is_not_reported_to_a_worker(self):
        mid = self.s.add(rec("x"), "x")
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, **MAIN)
        self.assertEqual(self.s.verify(), [])

    def named(self, word, reader="main"):
        return [p for p in self.s.verify(reader_scope=reader) if word in p]

    def test_a_rewritten_scope_is_named(self):
        mid = self.s.add(rec("x"), "x")
        self.forge("UPDATE records SET scope='main' WHERE memory_id='%s'" % mid)
        self.assertTrue(self.named("scope does not match"))

    def test_an_undone_erasure_flag_is_named(self):
        mid = self.s.add(rec("gone"), "x")
        self.s.erase(mid, "asked", OP, **MAIN)
        self.forge("UPDATE records SET tombstoned=0 WHERE memory_id='%s'" % mid)
        self.assertTrue(self.named("erasure does not match"))

    def test_a_gone_record_is_named_to_both_scopes(self):
        mid = self.s.add(rec("x"), "x")
        self.forge("DELETE FROM records WHERE memory_id='%s'" % mid, "DELETE FROM versions WHERE memory_id='%s'" % mid)
        self.assertTrue(self.named("names a record that is gone"))
        self.assertTrue(self.named("names a record that is gone", "worker"))

    def test_a_stale_index_with_the_right_count_is_named(self):
        self.s.add(rec("zqolder words"), "x")
        self.lex("lexical_main.sqlite", "UPDATE gen SET value='stale'")
        self.lex("lexical_worker.sqlite", "UPDATE gen SET value='stale'")

        def refuse(name, docs):
            raise PermissionError("held open")

        real = self.s._write_index
        self.s._write_index = refuse
        self.s.add(rec("zqnewer words"), "x")
        self.s._write_index = real
        self.assertTrue(self.named("rebuild is pending"))

    def test_a_symbol_beside_a_word_changes_nothing(self):
        mid = self.s.add(rec("zqwords here"), "x")
        self.assertEqual(self.s.search("zqwords +")["hits"][0]["memory_id"], mid)

    def test_an_emptied_index_is_reported(self):
        self.s.add(rec("x"), "x")
        self.lex("lexical_worker.sqlite", "DELETE FROM lex")
        self.assertTrue(self.s.verify())

    def test_an_index_with_a_writer_mid_way_is_no_alarm(self):
        self.s.add(rec("x"), "x")
        self.lex("lexical_worker.sqlite", "DELETE FROM lex")
        self.lex("lexical_worker.sqlite", "UPDATE gen SET value='ahead'")
        self.assertFalse([p for p in self.s.verify() if "index" in p])

    def test_letters_the_index_drops_do_not_refuse_a_write(self):
        text = " ".join(["ᦰ"] * 8) + " zqafter"
        mid = self.s.add(rec(text), "x")
        self.assertEqual(self.s.search("zqafter")["hits"][0]["memory_id"], mid)

    def test_punctuation_separates_query_words(self):
        mid = self.s.add(rec("ZQone x ZQtwo"), "x")
        for query in ("ZQone,ZQtwo", "ZQtwo-ZQone", "ZQone/ZQtwo"):
            with self.subTest(query):
                self.assertEqual(self.s.search(query)["hits"][0]["memory_id"], mid)

    def test_every_character_treated_as_a_separator_is_one_to_the_index(self):
        from herald.memory import store as core
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61 remove_diacritics 2')")
        con.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        wrong = []
        for code in range(0x110000):
            char = chr(code)
            if not core._is_sure_separator(char) or char == "\x00":
                continue
            con.execute("DELETE FROM t")
            con.execute("INSERT INTO t VALUES (?)", ("a" + char + "b",))
            if con.execute("SELECT count(*) FROM v").fetchone()[0] != 2:
                wrong.append(hex(code))
        self.assertEqual(wrong, [])

    def test_non_list_evidence_is_a_validation_error_with_hidden_evidence(self):
        e = self.s.put_evidence(b"m", "m.md", "tool_output", OP, **MAIN)
        mid = self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x", **MAIN)
        for bad in (None, 5, [{}]):
            with self.subTest(repr(bad)):
                with self.assertRaises(ValidationError):
                    self.s.correct(mid, 1, {"evidence_ids": bad}, "r", OP)

    def test_malformed_arguments_get_ordinary_answers(self):
        self.assertIsNone(self.s.get("a\ud800"))
        self.assertEqual(self.s.history("a\ud800"), [])
        self.assertEqual(self.s.events("a\ud800"), [])
        with self.assertRaises(KeyError):
            self.s.correct("a\ud800", 1, {"confidence": 0.5}, "r", OP)
        with self.assertRaises(KeyError):
            self.s.erase("a\ud800", "asked", OP, **MAIN)
        with self.assertRaises(PolicyRefused):
            self.s.get("x", reader_scope=["worker"])
        for args in (("zq\ud800",), (5,)):
            with self.assertRaises(ValidationError):
                self.s.search(*args)
        for limit in (2 ** 70, 0, "1", True):
            with self.assertRaises(ValidationError):
                self.s.search("x", limit=limit)
        with self.assertRaises(ValidationError):
            self.s.put_evidence("text", "a.md", "tool_output", W)
        with self.assertRaises(ValidationError):
            self.s.put_evidence(b"x", "a.md", ["tool_output"], W)

    def test_a_citation_does_not_change_provenance_on_return(self):
        self.s.put_evidence(b"same", "s.md", "model_inference", W)
        native = self.s.put_evidence(b"same", "s.md", "user_document", OP, **MAIN)
        mid = self.s.add(rec("x", sensitivity="private", evidence_ids=[native], status="pending", **TOOL), "x", **MAIN)
        self.s.correct(mid, 1, {"sensitivity": "internal"}, "r", OP, **MAIN)
        self.assertEqual(self.s.get(mid, **MAIN)["evidence_ids"], [native])

    def test_the_ledger_names_worker_bytes_that_remain(self):
        e = self.worker_ev()
        mid = self.s.add(rec("x", evidence_ids=[e], status="pending", **TOOL), "x")
        self.s.correct(mid, 1, {"sensitivity": "private"}, "r", OP, **MAIN)
        report = self.s.erase(mid, "asked", OP, **MAIN)
        self.assertEqual(report["archive"], "done_worker_bytes_remain")
        states = {r["class"]: r["state"] for r in self.s.erasure_ledger(mid, **MAIN)}
        self.assertEqual(states["archive"], "done_worker_bytes_remain")
        self.assertEqual(states["records"], "done")
        self.assertEqual(self.s.verify(**MAIN), [])

    def test_the_ledger_stays_plain_when_nothing_remains(self):
        e = self.s.put_evidence(b"m", "m.md", "tool_output", OP, **MAIN)
        mid = self.s.add(rec("x", sensitivity="private", evidence_ids=[e], status="pending", **TOOL), "x", **MAIN)
        self.assertEqual(self.s.erase(mid, "asked", OP, **MAIN)["archive"], "done")


if __name__ == "__main__":
    unittest.main()
