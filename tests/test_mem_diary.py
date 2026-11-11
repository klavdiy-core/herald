"""Organ 2, the diary adapter: my own tests on a synthetic tree written the way diary.py writes (no live diary is read).
11.11.2026."""
import hashlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald.memory import Store  # noqa: E402
from herald.memory import diary  # noqa: E402

MAIN = {"reader_scope": "main"}
OP = {"id": "ENT-operator", "role": "operator"}
SUBJECT = "ENT-test-author"


def entry(root, stamp="2026-11-01-101500", slug="heron-day", title="heron day", body="the heron stood still",
          written_at="2026-11-01T10:15:00+02:00", tags=None, frontmatter=True, with_sha=True, extra="", folder=None):
    y, m = stamp[:4], stamp[5:7]
    path = os.path.join(root, *(folder or (y, m)), "%s--%s.md" % (stamp, slug))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not frontmatter:
        text = body + "\n"
    else:
        fm = ["---", "id: %s--%s" % (stamp, slug)]
        if written_at:
            fm.append("written_at: %s" % written_at)
        fm.append("title: %s" % title)
        if tags:
            fm.append("tags: [%s]" % ", ".join(tags))
        if with_sha:
            fm.append("body_sha256: %s" % hashlib.sha256(body.strip("\n").encode("utf-8")).hexdigest())
        fm += ["append_only: true"] + ([extra] if extra else []) + ["---", "", body, ""]
        text = "\n".join(fm)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def snapshot(folder):
    out = {}
    for base, _, names in os.walk(folder):
        for n in names:
            p = os.path.join(base, n)
            with open(p, "rb") as f:
                out[p] = (hashlib.sha256(f.read()).hexdigest(), os.path.getmtime(p))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "diary")
        os.makedirs(self.root)
        self.store_root = os.path.join(self.tmp.name, "store")
        Store(self.store_root).close()

    def run_cli(self, *args):
        out = io.StringIO()
        code = diary.cli(list(args), out)
        return code, out.getvalue().splitlines()

    def imp(self):
        return self.run_cli("import", "--root", self.root, "--store", self.store_root, "--subject", SUBJECT)

    def check(self):
        return self.run_cli("check", "--root", self.root, "--store", self.store_root)

    def records(self):
        s = Store(self.store_root)
        try:
            ids = [r[0] for r in s.db.execute("SELECT memory_id FROM records WHERE tombstoned=0")]
            return [s.get(i, **MAIN) for i in ids]
        finally:
            s.close()


class Import(Base):
    def test_one_file_becomes_one_observed_canonical_private_record(self):
        entry(self.root, tags=["day36", "work"])
        code, lines = self.imp()
        self.assertEqual((code, lines), (0, ["2026-11-01-101500--heron-day\timported", "IMPORTED 1 SKIPPED 0 REFUSED 0 LEGACY 0"]))
        [doc] = self.records()
        self.assertEqual((doc["record_type"], doc["authorship_channel"], doc["authority"], doc["status"], doc["sensitivity"]),
                         ("episodic", "system_observed", "diary_file", "canonical", "private"))
        self.assertEqual(doc["object"], {"entry_id": "2026-11-01-101500--heron-day", "title": "heron day",
                                         "tags": ["day36", "work"], "supersedes": None, "legacy": False})
        self.assertEqual(doc["valid_time"]["from"], "2026-11-01")      # the day of written_at, in its own offset

    def test_the_evidence_is_the_whole_file_byte_for_byte(self):
        path = entry(self.root)
        self.imp()
        s = Store(self.store_root)
        try:
            [doc] = [s.get(r[0], **MAIN) for r in s.db.execute("SELECT memory_id FROM records")]
            row = s.db.execute("SELECT sha256 FROM evidence WHERE id=?", (doc["evidence_ids"][0],)).fetchone()
        finally:
            s.close()
        with open(path, "rb") as f:
            self.assertEqual(row[0], hashlib.sha256(f.read()).hexdigest())

    def test_a_second_import_skips_and_exits_0(self):
        entry(self.root)
        self.imp()
        code, lines = self.imp()
        self.assertEqual((code, lines[-1]), (0, "IMPORTED 0 SKIPPED 1 REFUSED 0 LEGACY 0"))
        self.assertEqual(len(self.records()), 1)

    def test_a_changed_file_is_refused_and_the_store_keeps_the_old_bytes(self):
        path = entry(self.root)
        with open(path, "rb") as f:
            old = hashlib.sha256(f.read()).hexdigest()
        self.imp()
        with open(path, "a", encoding="utf-8") as f:
            f.write("a quiet edit\n")
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (4, "2026-11-01-101500--heron-day\tchanged"))
        self.assertEqual(diary.held(Store(self.store_root))["2026-11-01-101500--heron-day"], [old])

    def test_a_body_that_does_not_match_its_hash_is_not_imported(self):
        path = entry(self.root)
        text = open(path, encoding="utf-8").read().replace("stood still", "flew away")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (4, "2026-11-01-101500--heron-day\tbody_mismatch"))
        self.assertEqual(self.records(), [])

    def test_legacy_entries_are_imported_and_counted(self):
        entry(self.root, stamp="2026-07-29-2100", slug="no-hash", with_sha=False)
        entry(self.root, stamp="2026-07-29-2200", slug="no-frontmatter", frontmatter=False)
        code, lines = self.imp()
        self.assertEqual((code, lines[-1]), (0, "IMPORTED 2 SKIPPED 0 REFUSED 0 LEGACY 2"))
        self.assertTrue(all(d["object"]["legacy"] for d in self.records()))

    def test_a_v1_name_and_an_adopted_entry_take_their_day_from_the_name(self):
        entry(self.root, stamp="2026-07-29-2130", slug="v-one", written_at=None)
        [doc] = (self.imp(), self.records())[1]
        self.assertEqual(doc["valid_time"]["from"], "2026-07-29")

    def test_the_file_name_is_the_identity_not_the_frontmatter_id(self):
        entry(self.root, extra="id: 1999-01-01-000000--forged")
        self.imp()
        self.assertEqual(self.records()[0]["object"]["entry_id"], "2026-11-01-101500--heron-day")

    def test_a_feeling_comes_only_from_the_frontmatter(self):
        entry(self.root, extra="feeling: calm")
        self.imp()
        self.assertEqual(self.records()[0]["object"]["feeling"], "calm")

    def test_bad_entries_print_a_path_hash_and_never_block_the_good_ones(self):
        good = entry(self.root, stamp="2026-11-02-090000", slug="good")
        entry(self.root, stamp="2026-11-02-090100", slug="wrong-folder", folder=("2026", "10"))
        entry(self.root, stamp="2026-11-02-090200", slug="naive-date", written_at="2026-11-02T09:02:00")
        bad_name = os.path.join(self.root, "2026", "11", "Secret Heron Name.md")
        with open(bad_name, "w", encoding="utf-8") as f:
            f.write("---\ntitle: x\n---\nx\n")     # without frontmatter it would be a companion (COVERS.md), not an entry
        unclosed = os.path.join(self.root, "2026", "11", "2026-11-02-090300--unclosed.md")
        with open(unclosed, "w", encoding="utf-8") as f:
            f.write("---\ntitle: never closed\n")
        latin = os.path.join(self.root, "2026", "11", "2026-11-02-090400--latin.md")
        with open(latin, "wb") as f:
            f.write(b"---\ntitle: caf\xe9\n---\nbody\n")
        code, lines = self.imp()
        self.assertEqual(code, 4)
        self.assertIn("2026-11-02-090000--good\timported", lines)
        self.assertEqual(sum(1 for line in lines if line.endswith("\tbad_entry")), 5)
        self.assertEqual(lines[-1], "IMPORTED 1 SKIPPED 0 REFUSED 5 LEGACY 0")
        joined = "\n".join(lines)
        for leak in ("Secret", "wrong-folder", "naive-date", "unclosed", "latin", self.root):
            self.assertNotIn(leak, joined)
        self.assertTrue(os.path.exists(good))

    def test_an_erased_entry_is_never_brought_back_by_an_import(self):
        entry(self.root)
        self.imp()
        s = Store(self.store_root)
        [mid] = [r[0] for r in s.db.execute("SELECT memory_id FROM records")]
        s.erase(mid, "asked", OP, **MAIN)
        s.close()
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (0, "2026-11-01-101500--heron-day\terased"))
        self.assertEqual(self.records(), [])

    def test_read_after_write_is_required(self):
        entry(self.root)
        s = Store(self.store_root)
        self.addCleanup(s.close)
        with mock.patch.object(Store, "get", return_value=None):
            with self.assertRaises(Exception):
                diary.import_tree(s, self.root, lambda *a: None, SUBJECT)

    def test_the_diary_tree_is_only_read(self):
        entry(self.root)
        entry(self.root, stamp="2026-11-01-111500", slug="second", with_sha=False)
        before = snapshot(self.root)
        self.imp()
        self.check()
        self.run_cli("last", "5", "--store", self.store_root)
        self.assertEqual(snapshot(self.root), before)


class Check(Base):
    def test_equal_trees_are_exit_0_with_the_counts(self):
        entry(self.root)
        entry(self.root, stamp="2026-07-29-2100", slug="old", with_sha=False)
        self.imp()
        self.assertEqual(self.check(), (0, ["CHECK disk 2 store 2 legacy 1"]))

    def test_missing_extra_and_changed_are_named(self):
        a = entry(self.root, stamp="2026-11-01-101500", slug="a")
        c = entry(self.root, stamp="2026-11-01-101700", slug="c")
        self.imp()
        entry(self.root, stamp="2026-11-01-101600", slug="b")          # on disk, not imported
        os.remove(a)                                                    # imported, gone from disk
        with open(c, "a", encoding="utf-8") as f:
            f.write("edited\n")
        code, lines = self.check()
        self.assertEqual(code, 4)
        self.assertEqual(sorted(lines[:-1]), ["CHANGED 2026-11-01-101700--c", "EXTRA 2026-11-01-101500--a",
                                              "MISSING 2026-11-01-101600--b"])

    def test_a_bad_entry_fails_the_check_and_is_counted_apart(self):
        entry(self.root)
        self.imp()
        with open(os.path.join(self.root, "2026", "11", "notes.md"), "w", encoding="utf-8") as f:
            f.write("---\ntitle: x\n---\nbody\n")
        code, lines = self.check()
        self.assertEqual((code, lines[-1]), (4, "CHECK disk 1 store 1 legacy 0 bad_entry 1"))

    def test_the_day_of_written_at_wins_over_the_name_when_they_differ(self):
        entry(self.root, stamp="2026-11-11-233000", slug="late", written_at="2026-11-12T00:30:00+02:00")
        self.imp()
        self.assertEqual(self.records()[0]["valid_time"]["from"], "2026-11-12")

    def test_a_broken_body_not_in_the_store_is_named_once(self):
        path = entry(self.root)
        text = open(path, encoding="utf-8").read().replace("stood still", "flew away")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        code, lines = self.check()
        self.assertEqual((code, lines), (4, ["BODY_MISMATCH 2026-11-01-101500--heron-day",
                                             "CHECK disk 0 store 0 legacy 0 body_mismatch 1"]))

    def test_check_never_writes_the_store(self):
        entry(self.root)
        self.imp()
        before = snapshot(self.store_root)
        self.check()
        self.assertEqual({p: h for p, (h, _) in snapshot(self.store_root).items() if not p.endswith(("-wal", "-shm"))},
                         {p: h for p, (h, _) in before.items() if not p.endswith(("-wal", "-shm"))})


class ControlRound1(Base):
    """wf_5a6010fe: findings confirmed by two skeptics each."""

    def erase_only_record(self):
        s = Store(self.store_root)
        try:
            [mid] = [r[0] for r in s.db.execute("SELECT memory_id FROM records WHERE tombstoned=0")]
            s.erase(mid, "asked", OP, **MAIN)
        finally:
            s.close()

    def evidence_rows(self):
        s = Store(self.store_root)
        try:
            return s.db.execute("SELECT count(*) FROM evidence").fetchone()[0], sorted(os.listdir(s.archive))
        finally:
            s.close()

    def test_c1_a_companion_file_is_not_an_entry(self):
        entry(self.root)
        with open(os.path.join(self.root, "COVERS.md"), "w", encoding="utf-8") as f:
            f.write("# covers\n")
        self.assertEqual(self.imp()[0], 0)
        self.assertEqual(self.check(), (0, ["CHECK disk 1 store 1 legacy 0"]))

    def test_c1_an_undated_name_with_frontmatter_is_still_a_bad_entry(self):
        with open(os.path.join(self.root, "notes.md"), "w", encoding="utf-8") as f:
            f.write("---\ntitle: x\n---\nbody\n")
        code, lines = self.imp()
        self.assertEqual((code, lines[0].endswith("\tbad_entry")), (4, True))

    def test_c2_an_entry_saved_with_crlf_still_matches_its_hash(self):
        path = entry(self.root)
        with open(path, "rb") as f:
            data = f.read().replace(b"\n", b"\r\n")
        with open(path, "wb") as f:
            f.write(data)
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (0, "2026-11-01-101500--heron-day\timported"))

    def test_c3_an_erased_entry_puts_no_byte_back_into_the_store(self):
        entry(self.root)
        self.imp()
        self.erase_only_record()
        before = self.evidence_rows()
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (0, "2026-11-01-101500--heron-day\terased"))
        self.assertEqual(self.evidence_rows(), before)

    def test_c4_check_names_an_erased_entry_and_stays_green(self):
        entry(self.root)
        self.imp()
        self.erase_only_record()
        self.assertEqual(self.check(), (0, ["ERASED 2026-11-01-101500--heron-day", "CHECK disk 0 store 0 legacy 0"]))

    def test_c5_an_erased_entry_stays_erased_after_its_title_and_tags_change(self):
        path = entry(self.root)
        self.imp()
        self.erase_only_record()
        os.remove(path)
        entry(self.root, title="a new title", tags=["other"], body="a rewritten body")
        code, lines = self.imp()
        self.assertEqual((code, lines[0]), (0, "2026-11-01-101500--heron-day\terased"))
        self.assertEqual(self.records(), [])

    def test_c6_an_unreadable_file_is_a_bad_entry_without_a_traceback(self):
        entry(self.root)
        bad = entry(self.root, stamp="2026-11-01-111500", slug="locked-secret")
        real_open = open

        def guarded(path, *a, **k):
            if os.path.abspath(str(path)) == os.path.abspath(bad):
                raise PermissionError(13, "denied", bad)
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", guarded):
            code, lines = self.imp()
        self.assertEqual(code, 4)
        self.assertEqual(sum(1 for line in lines if line.endswith("\tbad_entry")), 1)
        self.assertNotIn("locked-secret", "\n".join(lines))

    def test_c7_a_folder_that_cannot_be_listed_is_said(self):
        entry(self.root)
        real_walk = os.walk

        def walk_with_error(top, onerror=None, **k):
            if onerror:
                onerror(PermissionError(13, "denied", os.path.join(top, "2026", "12")))
            yield from real_walk(top, onerror=onerror, **k)
        with mock.patch("os.walk", walk_with_error):
            code, lines = self.imp()
            check_code, check_lines = self.check()
        self.assertEqual((code, check_code), (4, 4))
        self.assertTrue(any(line.endswith("\tbad_entry") for line in lines))
        self.assertTrue(any(line.startswith("BAD_ENTRY ") for line in check_lines))
        self.assertTrue(check_lines[-1].endswith(" bad_entry 1"), check_lines[-1])

    def test_the_memory_id_is_derived_from_the_entry_id(self):
        entry(self.root)
        self.imp()
        self.assertEqual(self.records()[0]["memory_id"], diary.memory_id_of("2026-11-01-101500--heron-day"))

    def test_erased_id_is_main_only(self):
        from herald.memory import PolicyRefused
        s = Store(self.store_root)
        self.addCleanup(s.close)
        with self.assertRaises(PolicyRefused):
            s.erased_id("MEM-0000000000000000", reader_scope="worker")


class LastAndRefusals(Base):
    def test_last_is_newest_first_with_id_day_and_title(self):
        entry(self.root, stamp="2026-11-01-101500", slug="older", title="older one")
        entry(self.root, stamp="2026-11-03-101500", slug="newer", title="newer one", written_at="2026-11-03T10:15:00+02:00")
        self.imp()
        code, lines = self.run_cli("last", "1", "--store", self.store_root)
        self.assertEqual((code, lines), (0, ["2026-11-03-101500--newer\t2026-11-03\tnewer one"]))

    def test_nothing_to_work_on_is_exit_7_without_ids_or_traceback(self):
        for args in (("import", "--root", self.root, "--store", os.path.join(self.tmp.name, "none")),
                     ("check", "--root", os.path.join(self.tmp.name, "none"), "--store", self.store_root),
                     ("import", "--store", self.store_root),
                     ("bogus", "heron"),
                     ()):
            code, lines = self.run_cli(*args)
            self.assertEqual(code, 7, args)
            self.assertTrue(lines[0].startswith("DIARY UNAVAILABLE"), args)
            self.assertNotIn("heron", "\n".join(lines))

    def test_import_makes_its_own_store_and_check_never_does(self):
        entry(self.root)
        fresh = os.path.join(self.tmp.name, "fresh_store")
        code, lines = self.run_cli("check", "--root", self.root, "--store", fresh)
        self.assertEqual((code, os.path.exists(fresh)), (7, False))
        code, lines = self.run_cli("import", "--root", self.root, "--store", fresh, "--subject", SUBJECT)
        self.assertEqual((code, lines[-1]), (0, "IMPORTED 1 SKIPPED 0 REFUSED 0 LEGACY 0"))

    def test_without_a_subject_import_is_exit_7_and_no_store_is_made_or_touched(self):
        entry(self.root)
        fresh = os.path.join(self.tmp.name, "never")
        for extra in ((), ("--subject", ""), ("--subject", "  ")):
            code, lines = self.run_cli("import", "--root", self.root, "--store", fresh, *extra)
            self.assertEqual((code, lines), (7, ["DIARY UNAVAILABLE: no --subject"]), extra)
        self.assertFalse(os.path.exists(fresh))
        before = snapshot(self.store_root)
        self.run_cli("import", "--root", self.root, "--store", self.store_root)
        self.assertEqual(snapshot(self.store_root), before)

    def test_a_root_without_any_entry_is_exit_7_never_a_green_zero(self):
        for setup in ("empty", "companion only"):
            if setup == "companion only":
                with open(os.path.join(self.root, "COVERS.md"), "w", encoding="utf-8") as f:
                    f.write("# covers\n")
            for cmd in (self.imp, self.check):
                code, lines = cmd()
                self.assertEqual((code, lines), (7, ["DIARY UNAVAILABLE: no diary entry under the root"]), (setup, cmd))

    def test_the_subject_is_given_by_the_caller(self):
        entry(self.root)
        self.run_cli("import", "--root", self.root, "--store", self.store_root, "--subject", "ENT-someone")
        self.assertEqual(self.records()[0]["subject_id"], "ENT-someone")

    def test_the_cli_route(self):
        import subprocess
        entry(self.root)
        r = subprocess.run([sys.executable, "-X", "utf8", "-m", "herald", "mem", "diary", "import", "--root", self.root,
                            "--store", self.store_root, "--subject", SUBJECT], capture_output=True, text=True, encoding="utf-8",
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1]), (0, "IMPORTED 1 SKIPPED 0 REFUSED 0 LEGACY 0"), r.stderr)


if __name__ == "__main__":
    unittest.main()
