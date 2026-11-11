"""herald mem journal status: a READ-ONLY reader of a kcore journal (organ 1, reviewer M-21920).
Its verdict must equal `python -m kcore --root R status` on the same root, and it must not write a byte there: kcore's own
status takes an exclusive lock that creates state/.events.lock. Stands are built by kcore itself in a temp folder, each at a
new path (kcore keeps an outside anchor keyed by the root's path). Skipped where kcore is not on this machine."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
KCORE = os.environ.get("HERALD_KCORE_TEST", "")      # where kcore lives on this machine; unset = skipped, said out loud
HAVE_KCORE = os.path.isdir(os.path.join(KCORE, "kcore"))
LOCKED = ("MISSION.md", "policy/privacy.yaml", "policy/security.yaml", "policy/canonicalization.yaml",
          "policy/deletion.yaml", "policy/style_guide.md")


LIVE_ANCHORS = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "kcore_anchors")
ANCHORS = {"dir": None}


def beside_live(path):
    live = os.path.normcase(os.path.realpath(LIVE_ANCHORS))
    return os.path.normcase(os.path.realpath(path)).startswith(live)


def kcore(root, *args):
    # reviewer M-22049/M-22054, lead M-22052: a stand never writes its outside anchor beside the live journal's one
    if beside_live(anchor_dir(root)):
        raise RuntimeError("the stand would write its kcore anchor into the live folder: KCORE_ANCHOR_DIR is not set")
    env = dict(os.environ, PYTHONPATH=KCORE)
    env.pop("KCORE_ROOT", None)
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "kcore", "--root", root] + list(args),
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)


def stand(base, tasks=2):
    root = tempfile.mkdtemp(dir=base, prefix="kroot_")
    for rel in LOCKED:
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write("stand %s\n" % rel)
    assert kcore(root, "lock", "--reason", "stand").returncode == 0
    for i in range(1, tasks + 1):
        tid = "TASK-%04d" % i
        assert kcore(root, "task-create", tid, "task %d" % i, "--req", "REQ-%d" % i).returncode == 0
        assert kcore(root, "task-move", tid, "ready").returncode == 0
    return root


def snapshot(root):
    out = {}
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = (hashlib.sha256(f.read()).hexdigest(), os.path.getmtime(p))
    return out


def setUpModule():
    # reviewer M-21933 (2): on the acceptance run these tests must RUN; a skip is said out loud, never silently
    print("\n[test_mem_journal] kcore %s at %s" % ("found" if HAVE_KCORE else "NOT FOUND - organ 1 tests SKIPPED", KCORE),
          file=sys.stderr)
    ANCHORS["before"] = os.environ.get("KCORE_ANCHOR_DIR")
    ANCHORS["dir"] = tempfile.mkdtemp(prefix="kanchors_")
    os.environ["KCORE_ANCHOR_DIR"] = ANCHORS["dir"]     # read by kcore at each call, in this process and in its children


def tearDownModule():
    if ANCHORS["before"] is None:
        os.environ.pop("KCORE_ANCHOR_DIR", None)
    else:
        os.environ["KCORE_ANCHOR_DIR"] = ANCHORS["before"]
    __import__("shutil").rmtree(ANCHORS["dir"], ignore_errors=True)


def anchor_dir(root):
    sys.path.insert(0, KCORE)
    from kcore.eventlog import external_anchor_path
    return os.path.dirname(str(external_anchor_path(__import__("pathlib").Path(root))))


@unittest.skipUnless(HAVE_KCORE, "kcore is not on this machine")
class JournalStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        from herald import journal
        self.journal = journal

    def run_cli(self, root):
        from herald import __main__ as cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["mem", "journal", "status", "--root", root, "--kcore", KCORE])
        return code, buf.getvalue()

    def test_a_stand_writes_its_anchor_beside_the_test_and_never_beside_the_live_one(self):
        """reviewer M-22054: the stands used to leave a kcore anchor per tmp root in the live %LOCALAPPDATA%\\kcore_anchors."""
        live_before = sorted(os.listdir(LIVE_ANCHORS)) if os.path.isdir(LIVE_ANCHORS) else []
        root = stand(self.tmp.name, tasks=1)                 # kcore() refuses before writing if the env is not set
        self.journal.status(root, KCORE)
        self.assertFalse(beside_live(anchor_dir(root)))
        self.assertTrue(os.listdir(os.path.join(ANCHORS["dir"], "kcore_anchors")), "the stand's anchor is in the test's folder")
        self.assertEqual(sorted(os.listdir(LIVE_ANCHORS)) if os.path.isdir(LIVE_ANCHORS) else [], live_before)

    def test_the_verdict_equals_kcore_status_on_the_same_root(self):
        root = stand(self.tmp.name)
        theirs = json.loads(kcore(root, "status").stdout)
        ours = self.journal.status(root, KCORE)
        self.assertEqual(ours["next_safe_action"], theirs["next_safe_action"])
        self.assertEqual(ours["events"], theirs["log"]["events"])
        self.assertEqual(ours["head_hash"], theirs["log"]["head_hash"])
        self.assertTrue(ours["ok"])

    def test_the_cli_prints_the_next_safe_action_and_the_last_record(self):
        root = stand(self.tmp.name)
        theirs = json.loads(kcore(root, "status").stdout)
        code, text = self.run_cli(root)
        self.assertEqual(code, 0, text)
        self.assertIn(theirs["next_safe_action"], text)
        self.assertIn(theirs["log"]["head_hash"], text)

    def test_reading_writes_nothing_in_the_root_not_even_the_lock_file(self):
        root = stand(self.tmp.name)
        lock = os.path.join(root, "state", ".events.lock")
        if os.path.exists(lock):
            os.remove(lock)
        before = snapshot(root)
        self.journal.status(root, KCORE)
        self.assertEqual(snapshot(root), before, "the reader changed or created a file in the journal root")

    def test_reading_writes_nothing_in_the_outside_anchor_folder_either(self):
        """reviewer M-21933 (1): kcore keeps a second anchor in %LOCALAPPDATA%; the reader must not touch it."""
        root = stand(self.tmp.name)
        folder = anchor_dir(root)
        self.assertTrue(os.path.isdir(folder), "stand: kcore wrote an outside anchor folder")
        before = (snapshot(folder), os.path.getmtime(folder))
        self.journal.status(root, KCORE)
        self.assertEqual((snapshot(folder), os.path.getmtime(folder)), before)

    def test_a_torn_last_line_is_read_again_exactly_once(self):
        """reviewer M-21933 (3): a torn last line (a writer mid-write) -> one more pass, which reads the whole line."""
        root = stand(self.tmp.name)
        # a writer appends the line first and moves the anchors after: mid-write, the torn line is NOT anchored yet
        sys.path.insert(0, KCORE)
        from kcore.eventlog import external_anchor_path
        anchors = [os.path.join(root, "state", "anchor.json"), str(external_anchor_path(__import__("pathlib").Path(root)))]
        saved = {p: __import__("pathlib").Path(p).read_bytes() for p in anchors}     # closed, no ResourceWarning
        assert kcore(root, "task-create", "TASK-0099", "the write in flight", "--req", "REQ-99").returncode == 0
        for p, data in saved.items():                  # the anchors as they stood before that write finished
            with open(p, "wb") as f:
                f.write(data)
        events = os.path.join(root, "state", "events.jsonl")
        with open(events, "rb") as f:
            whole = f.read()
        cut = whole[:-1].rfind(b"\n") + 1
        with open(events, "wb") as f:
            f.write(whole[:cut + 20])                  # the new, unanchored event half written
        self.assertEqual({e["code"] for e in json.loads(kcore(root, "status").stdout)["log"]["errors"]}, {"torn_tail"},
                         "stand: a torn unanchored tail and nothing else")
        passes = []

        def finish_the_write():
            passes.append(1)
            with open(events, "wb") as f:
                f.write(whole)                         # the writer finished between the two passes

        ours = self.journal.status(root, KCORE, _between_passes=finish_the_write)
        self.assertEqual(len(passes), 1, "exactly one more pass")
        self.assertEqual(ours["errors"], [], "the second pass read the whole line")
        self.assertEqual(ours["next_safe_action"], json.loads(kcore(root, "status").stdout)["next_safe_action"])

    def test_a_torn_line_that_stays_torn_is_a_loud_failure_after_one_retry(self):
        root = stand(self.tmp.name)
        events = os.path.join(root, "state", "events.jsonl")
        with open(events, "ab") as f:
            f.write(b'{"seq": 99, "half')
        passes = []
        ours = self.journal.status(root, KCORE, _between_passes=lambda: passes.append(1))
        self.assertEqual(len(passes), 1)
        self.assertFalse(ours["ok"])

    def test_garbage_in_the_middle_fails_at_once_without_a_retry(self):
        root = stand(self.tmp.name)
        events = os.path.join(root, "state", "events.jsonl")
        with open(events, "rb") as f:
            lines = f.read().split(b"\n")
        lines.insert(1, b"this is not an event")
        with open(events, "wb") as f:
            f.write(b"\n".join(lines))
        passes = []
        ours = self.journal.status(root, KCORE, _between_passes=lambda: passes.append(1))
        self.assertEqual(passes, [], "no retry for damage in the middle")
        self.assertFalse(ours["ok"])

    def test_the_witness_of_the_read_only_test_is_real(self):
        """Negative control: kcore's own status DOES create the lock file, and the snapshot sees it."""
        root = stand(self.tmp.name)
        lock = os.path.join(root, "state", ".events.lock")
        if os.path.exists(lock):
            os.remove(lock)
        before = snapshot(root)
        kcore(root, "status")
        self.assertNotEqual(snapshot(root), before)

    def test_a_missing_journal_is_a_loud_error(self):
        root = tempfile.mkdtemp(dir=self.tmp.name, prefix="empty_")
        with self.assertRaises(self.journal.JournalUnavailable):
            self.journal.status(root, KCORE)
        code, text = self.run_cli(root)
        self.assertNotEqual(code, 0)
        self.assertNotIn("ok", text.lower().split())

    def test_a_garbage_line_in_the_middle_is_not_ok(self):
        root = stand(self.tmp.name)
        events = os.path.join(root, "state", "events.jsonl")
        with open(events, "rb") as f:
            lines = f.read().split(b"\n")
        lines.insert(1, b"this is not an event")
        with open(events, "wb") as f:
            f.write(b"\n".join(lines))
        theirs = json.loads(kcore(root, "status").stdout)
        ours = self.journal.status(root, KCORE)
        self.assertFalse(ours["ok"])
        self.assertEqual(ours["next_safe_action"], theirs["next_safe_action"])
        code, _ = self.run_cli(root)
        self.assertNotEqual(code, 0, "a broken journal must not exit 0")

    def test_reading_another_root_does_not_pass_for_this_one(self):
        """Mutant guard: two stands differ in their head hash; the reader must answer for the root it was given."""
        a, b = stand(self.tmp.name, tasks=1), stand(self.tmp.name, tasks=2)
        self.assertNotEqual(self.journal.status(a, KCORE)["head_hash"], json.loads(kcore(b, "status").stdout)["log"]["head_hash"])
        self.assertEqual(self.journal.status(b, KCORE)["head_hash"], json.loads(kcore(b, "status").stdout)["log"]["head_hash"])

    def test_no_root_means_refusal_never_the_live_root(self):
        with self.assertRaises(self.journal.JournalUnavailable):
            self.journal.status("", KCORE)
        with self.assertRaises(self.journal.JournalUnavailable):
            self.journal.status(None, KCORE)


if __name__ == "__main__":
    unittest.main()
