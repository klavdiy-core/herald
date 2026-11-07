"""When tasks.json can be neither read nor moved aside, herald refuses to write the task store (so it cannot overwrite
the file). These tests pin what the owner sees in that state:
  S1  a message in a task topic gets one plain line about the locked store, and no paid turn is started;
  S2  /task does not create a Telegram topic it could not record;
  S3  once the file is readable again, the lock is lifted and the next turn's answer reaches the topic.
And the doctor command passes the configured forum group to the forum check (D1).
"""
import builtins, json, os, sys, tempfile, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_forum_tasks as T  # noqa: E402

STORE = {"tasks": {"42": {"name": "precious", "session_id": None, "state": "open", "created": 0, "closed_at": None}},
         "done_topic": None}


class StoreLocked(T.Base):
    def setUp(self):
        super().setUp()
        self.make()
        self.path = os.path.join(self.control, "tasks.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(STORE, f)

    def _denied(self):
        real_open, real_replace = builtins.open, os.replace
        base = os.path.basename(self.path)

        def deny_open(p, *a, **k):
            if isinstance(p, str) and os.path.basename(p) == base:
                raise PermissionError(13, "denied", p)
            return real_open(p, *a, **k)

        def deny_replace(s, d, *a, **k):
            if isinstance(s, str) and os.path.basename(s).startswith(base) and not s.endswith(".tmp"):
                raise PermissionError(13, "denied", s)
            return real_replace(s, d, *a, **k)

        return mock.patch.object(builtins, "open", deny_open), mock.patch.object(os, "replace", deny_replace)

    def _texts(self, thread):
        return [t for (_c, t, th) in self.bot.texts if th == thread]

    def test_S1_topic_message_gets_a_line_and_no_paid_turn(self):
        a, b = self._denied()
        with a, b:
            self.owner(42, "hello")
        self.assertEqual(self.engine.n, 0, "a paid turn ran while the task store was locked")
        said = self._texts(42)
        self.assertTrue(any("locked" in t.lower() for t in said), f"no line about the locked store in the topic: {said}")

    def test_S2_task_creates_no_topic_it_cannot_record(self):
        a, b = self._denied()
        with a, b:
            try:
                self.owner(0, "/task fresh")
            except OSError:
                pass
        self.assertEqual(self.bot.of("create"), [], "a Telegram topic was created while the store could not record it")

    def test_S3_lock_lifts_when_the_file_is_readable_again(self):
        a, b = self._denied()
        with a, b:
            self.owner(42, "hello")
        self.owner(42, "and now?")                         # the file is readable again
        self.assertEqual(self.engine.n, 1)
        self.assertIn("answer 1", " ".join(self._texts(42)), "the paid turn's answer did not reach the topic")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["tasks"]["42"]["session_id"], "s1", "the turn's session was not saved")


    def test_S4_stop_works_while_the_store_is_locked(self):
        a, b = self._denied()
        with a, b:
            self.owner(42, "/stop")
        self.assertTrue(os.path.exists(self.h.stop_path), "/stop did not stop herald while the task store was locked")

    def test_S5_stop_works_after_a_broken_store_was_moved_aside(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"tasks": {"42"')                      # broken: it goes to .bad, the topic becomes unknown
        self.owner(42, "/stop")
        self.assertTrue(os.path.exists(self.h.stop_path), "/stop in a topic was ignored after tasks.json went to .bad")

    def test_S6_close_saved_or_said_when_the_write_fails_after_telegram(self):
        self.owner(42, "/done")
        real_replace = os.replace

        def fail_tasks_replace(s, d, *a, **k):
            if isinstance(d, str) and os.path.basename(d) == "tasks.json":
                raise PermissionError(13, "denied", d)
            return real_replace(s, d, *a, **k)

        with mock.patch.object(os, "replace", fail_tasks_replace):
            self.tap(42, "task:done:42")                   # Telegram closes the topic, then the write fails
        said = " ".join(self._texts(42)).lower()
        self.assertIn("not saved", said, f"a close that Telegram did but the store did not keep was not said: {said}")


class DoctorForum(unittest.TestCase):
    def test_D1_doctor_passes_forum_chat_id(self):
        from herald import __main__ as cli
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "c"))
            cfg = os.path.join(d, "c", "config.json")
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({"owners": [1001], "workspace": os.path.join(d, "w"), "control": os.path.join(d, "c"),
                           "forum_chat_id": -1001111111111}, f)
            with mock.patch.object(cli.doctor, "run_all", return_value=[]) as ra, \
                    mock.patch.object(cli.doctor, "render", return_value=""), \
                    mock.patch.object(cli.doctor, "may_start", return_value=True), \
                    mock.patch.dict(os.environ, {"HERALD_TELEGRAM_TOKEN": ""}):
                try:
                    cli.main(["doctor", "--config", cfg])
                except SystemExit:
                    pass
            self.assertTrue(ra.called, "doctor.run_all was not called")
            self.assertEqual(ra.call_args.kwargs.get("forum_chat_id"), -1001111111111,
                             f"the forum group did not reach doctor.run_all: {ra.call_args}")


if __name__ == "__main__":
    unittest.main()
