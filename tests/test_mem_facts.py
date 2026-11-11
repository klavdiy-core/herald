"""Organ 6, the facts adapter: own tests on synthetic lines written the way the facts tool writes them. 11.11.2026."""
import hashlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald.memory import Store  # noqa: E402
from herald.memory import facts  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}
SUBJECT = "ENT-test-author"
TICK = "✔"


def line(fact, tag="us", src="note:1", when="2026-11-01 10:15", sha=None):
    body = "- [%s] (%s) %s · src=%s" % (when, tag, fact, src)
    return body + " · #" + (sha or hashlib.sha256(body.encode("utf-8")).hexdigest()[:8])


def fid(text):
    return text.rsplit("#", 1)[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "facts.md")
        self.store_root = os.path.join(self.tmp.name, "store")
        Store(self.store_root).close()

    def write(self, lines, archive=("# an old archive line", "another archive line")):
        with open(self.file, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(list(archive) + ["", "", facts.MARK, "> one line = one fact"] + list(lines)) + "\n")

    def run_cli(self, *args):
        out = io.StringIO()
        code = facts.cli(list(args), out)
        return code, out.getvalue().splitlines()

    def imp(self):
        return self.run_cli("import", "--file", self.file, "--store", self.store_root, "--subject", SUBJECT)

    def check(self):
        return self.run_cli("check", "--file", self.file, "--store", self.store_root)

    def records(self):
        s = Store(self.store_root)
        try:
            ids = [r[0] for r in s.db.execute("SELECT memory_id FROM records WHERE tombstoned=0")]
            return {d["object"]["fact_id"]: d for d in (s.get(i, **MAIN) for i in ids)}
        finally:
            s.close()


class Import(Base):
    def test_four_kinds_of_line_get_four_channels(self):
        a = line(TICK + " words: the kettle is blue", tag="her", src="msg:101")
        b = line(TICK + " words: the kettle is blue", tag="her", src="note:7", when="2026-11-01 10:16")
        c = line("~ an inference: likes quiet mornings", tag="her", when="2026-11-01 10:17")
        d = line("the lamp was moved", tag="me", when="2026-11-01 10:18")
        self.write([a, b, c, d])
        code, lines = self.imp()
        self.assertEqual((code, lines[-2:]), (0, ["archive lines 2", "IMPORTED 4 SKIPPED 0 REFUSED 0"]))
        r = self.records()
        got = {k: (v["authorship_channel"], v["status"], v["authority"], v["sensitivity"]) for k, v in r.items()}
        self.assertEqual(got[fid(a)], ("user_message", "canonical", "quoted_words", "highly_private"))
        self.assertEqual(got[fid(b)], ("assistant_asserted", "hypothesis", "quoted_unsourced", "highly_private"))
        self.assertEqual(got[fid(c)], ("model_inference", "hypothesis", "inference", "highly_private"))
        self.assertEqual(got[fid(d)], ("assistant_asserted", "hypothesis", "fact_file", "private"))
        self.assertEqual(r[fid(d)]["subject_id"], SUBJECT)
        self.assertEqual(r[fid(d)]["object"], {"fact_id": fid(d), "tag": "me", "text": "the lamp was moved", "src": "note:1"})

    def test_an_inference_mark_wins_over_a_check_mark(self):
        a = line(TICK + " words, then ~ my reading of them", tag="her", src="msg:5")
        self.write([a])
        self.imp()
        self.assertEqual(self.records()[fid(a)]["status"], "hypothesis")

    def test_a_tilde_before_a_time_is_not_an_inference_mark(self):
        a = line(TICK + " said at ~21:55: come back", tag="her", src="msg:6")
        self.write([a])
        self.imp()
        self.assertEqual(self.records()[fid(a)]["status"], "canonical")

    def test_the_time_carries_the_fixed_offset_across_midnight(self):
        a = line("late fact", when="2026-11-11 23:59")
        self.write([a])
        self.imp()
        self.assertEqual(self.records()[fid(a)]["valid_time"]["from"], "2026-11-11T23:59:00" + facts.OFFSET)

    def test_the_evidence_is_the_line_itself(self):
        a = line("the door is green")
        self.write([a])
        self.imp()
        s = Store(self.store_root)
        try:
            [doc] = [s.get(r[0], **MAIN) for r in s.db.execute("SELECT memory_id FROM records")]
            row = s.db.execute("SELECT sha256, source FROM evidence WHERE id=?", (doc["evidence_ids"][0],)).fetchone()
        finally:
            s.close()
        self.assertEqual(tuple(row), (hashlib.sha256(a.encode("utf-8")).hexdigest(), "facts:" + fid(a)))     # the whole line

    def test_a_second_import_skips(self):
        self.write([line("one"), line("two", when="2026-11-01 10:16")])
        self.imp()
        code, lines = self.imp()
        self.assertEqual((code, lines[-1]), (0, "IMPORTED 0 SKIPPED 2 REFUSED 0"))

    def test_a_silent_edit_is_body_mismatch_and_not_imported(self):
        good = line("the window faces east")
        edited = good.replace("east", "west")
        self.write([edited])
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (4, fid(good) + "\tbody_mismatch"))
        self.assertEqual(self.records(), {})

    def test_a_bad_line_prints_a_hash_only_and_the_rest_goes_in(self):
        good = line("the cup is on the shelf")
        self.write(["- not a fact line about a secret kettle", good])
        code, lines = self.imp()
        self.assertEqual(code, 4)
        self.assertIn(fid(good) + "\timported", lines)
        self.assertNotIn("kettle", "\n".join(lines))
        self.assertTrue(any(x.endswith("\tbad_line") for x in lines))

    def test_the_archive_is_counted_never_imported_never_printed(self):
        self.write([line("one")], archive=("archive words about a kettle", "", "more archive"))
        code, lines = self.imp()
        self.assertEqual(lines[-2:], ["archive lines 2", "IMPORTED 1 SKIPPED 0 REFUSED 0"])
        self.assertNotIn("kettle", "\n".join(lines))
        self.assertEqual(len(self.records()), 1)

    def test_an_erased_fact_never_comes_back_and_no_byte_returns(self):
        a = line("erase me")
        self.write([a])
        self.imp()
        s = Store(self.store_root)
        [mid] = [r[0] for r in s.db.execute("SELECT memory_id FROM records")]
        s.erase(mid, "asked", OP, **MAIN)
        before = s.db.execute("SELECT count(*) FROM evidence").fetchone()[0], sorted(os.listdir(s.archive))
        s.close()
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (0, fid(a) + "\terased"))      # main erased it: said, not a refusal
        s = Store(self.store_root)
        after = s.db.execute("SELECT count(*) FROM evidence").fetchone()[0], sorted(os.listdir(s.archive))
        s.close()
        self.assertEqual(after, before)

    def test_the_file_is_only_read(self):
        self.write([line("one")])
        with open(self.file, "rb") as f:
            before = f.read()
        mtime = os.path.getmtime(self.file)
        self.imp()
        self.check()
        with open(self.file, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.path.getmtime(self.file), mtime)


class Check(Base):
    def test_equal_is_exit_0_with_the_counts(self):
        self.write([line("one"), line(TICK + " q", tag="her", src="note:2", when="2026-11-01 10:16")])
        self.imp()
        self.assertEqual(self.check(), (0, ["CHECK disk 2 store 2 archive 2 quoted_unsourced 1"]))

    def test_missing_extra_and_mismatch_are_named(self):
        a, b = line("a"), line("b", when="2026-11-01 10:16")
        self.write([a])
        self.imp()
        self.write([b, line("c", when="2026-11-01 10:17").replace("c ·", "z ·")])
        code, lines = self.check()
        self.assertEqual(code, 4)
        self.assertIn("MISSING " + fid(b), lines)
        self.assertIn("EXTRA " + fid(a), lines)
        self.assertTrue(any(x.startswith("BODY_MISMATCH ") for x in lines))
        self.assertTrue(lines[-1].endswith(" body_mismatch 1"))


class MutantsLeftAlive(Base):
    """The first mutant run of organ 6 left these unguarded."""

    def test_read_after_write_is_required(self):
        from unittest import mock
        self.write([line("one")])
        s = Store(self.store_root)
        self.addCleanup(s.close)
        with mock.patch.object(Store, "get", return_value=None):
            with self.assertRaises(Exception):
                facts.import_file(s, self.file, lambda *a: None, SUBJECT)

    def test_the_subject_is_the_callers_whatever_it_is(self):
        self.write([line("one")])
        self.run_cli("import", "--file", self.file, "--store", self.store_root, "--subject", "ENT-someone-else")
        self.assertEqual(list(self.records().values())[0]["subject_id"], "ENT-someone-else")

    def test_a_missing_line_alone_turns_the_check_red(self):
        a, b = line("a"), line("b", when="2026-11-01 10:16")
        self.write([a])
        self.imp()
        self.write([a, b])
        self.assertEqual(self.check(), (4, ["MISSING " + fid(b), "CHECK disk 2 store 1 archive 2"]))

    def test_check_never_writes_the_store(self):
        self.write([line("one")])
        self.imp()
        s = Store(self.store_root)
        archive = s.archive
        s.close()
        snap = lambda: sorted((p, os.path.getsize(os.path.join(self.store_root, p)))
                              for p in os.listdir(self.store_root) if not p.endswith(("-wal", "-shm")))
        import hashlib as _h
        digest = lambda: {p: _h.sha256(open(os.path.join(self.store_root, p), "rb").read()).hexdigest()
                          for p in os.listdir(self.store_root)
                          if os.path.isfile(os.path.join(self.store_root, p)) and not p.endswith(("-wal", "-shm"))}
        before = digest()
        self.check()
        self.assertEqual(digest(), before)

    def test_the_src_is_kept_apart_from_the_fact_text(self):
        a = line("the kettle is blue", src="msg:77")
        self.write([a])
        self.imp()
        doc = self.records()[fid(a)]
        self.assertEqual((doc["object"]["text"], doc["object"]["src"]), ("the kettle is blue", "msg:77"))


class Refusals(Base):
    def test_nothing_to_work_on_is_exit_7_without_text(self):
        self.write([line("secret kettle fact")])
        for args in (("import", "--file", self.file, "--store", self.store_root),
                     ("import", "--file", self.file, "--store", self.store_root, "--subject", " "),
                     ("check", "--file", os.path.join(self.tmp.name, "none.md"), "--store", self.store_root),
                     ("check", "--file", self.file, "--store", os.path.join(self.tmp.name, "nostore")),
                     ("import", "--store", self.store_root, "--subject", SUBJECT),
                     ("bogus", "kettle")):
            code, lines = self.run_cli(*args)
            self.assertEqual(code, 7, args)
            self.assertTrue(lines[0].startswith("FACTS UNAVAILABLE"), args)
            self.assertNotIn("kettle", "\n".join(lines))

    def test_without_a_subject_no_store_is_made(self):
        self.write([line("one")])
        fresh = os.path.join(self.tmp.name, "fresh")
        self.run_cli("import", "--file", self.file, "--store", fresh)
        self.assertFalse(os.path.exists(fresh))

    def test_a_file_without_the_marker_has_no_fact(self):
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("just an archive\n")
        self.assertEqual(self.imp(), (7, ["FACTS UNAVAILABLE: no fact under the marker"]))

    def test_tail_is_newest_first(self):
        old, new = line("older"), line("newer", when="2026-11-02 08:00")
        self.write([old, new])
        self.imp()
        code, lines = self.run_cli("tail", "1", "--store", self.store_root)
        self.assertEqual((code, lines), (0, [fid(new) + "\t2026-11-02 08:00\tus"]))      # no text in the tail

    def test_the_cli_route(self):
        import subprocess
        self.write([line("one")])
        r = subprocess.run([sys.executable, "-X", "utf8", "-m", "herald", "mem", "facts", "import", "--file", self.file,
                            "--store", self.store_root, "--subject", SUBJECT], capture_output=True, text=True,
                           encoding="utf-8", cwd=os.path.join(os.path.dirname(__file__), ".."))
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1]), (0, "IMPORTED 1 SKIPPED 0 REFUSED 0"), r.stderr)


if __name__ == "__main__":
    unittest.main()
