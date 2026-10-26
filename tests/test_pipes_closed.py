"""A killed turn closes its pipes. A Linux run with ResourceWarnings on (a second review asked for it) showed the stdout
and stderr of a turn killed by timeout or /stop left open: two handles per killed turn in a bot that runs for weeks.
The test catches the Popen object and checks its pipes after run_turn returns. It is red on Linux (POSIX) only: on
Windows CPython's communicate() reads the pipes in threads that close them at EOF, so the old code did not leak there
and the test passes on it - the CI's ubuntu job is the one that guards this."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import engine  # noqa: E402
from herald.engine import EngineConfig  # noqa: E402


class PipesClosed(unittest.TestCase):
    def setUp(self):
        self.procs = []
        real = subprocess.Popen

        def spy(*a, **k):
            p = real(*a, **k)
            self.procs.append(p)
            return p

        engine.subprocess.Popen = spy
        self.addCleanup(setattr, engine.subprocess, "Popen", real)

    def cfg(self, cmd, timeout):
        return EngineConfig(claude_cmd=[sys.executable, "-c", cmd], timeout_s=timeout, max_budget_usd=0.5)

    def assert_closed(self):
        p = self.procs[0]                  # the turn itself (on Windows the kill adds a taskkill Popen after it)
        for name in ("stdin", "stdout", "stderr"):
            pipe = getattr(p, name)
            self.assertTrue(pipe is None or pipe.closed, f"{name} left open")

    def test_a_turn_killed_by_timeout_closes_its_pipes(self):
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg("import time; time.sleep(30)", 1))
        self.assertIn("timeout", r.error)
        self.assert_closed()

    def test_a_turn_stopped_by_the_owner_closes_its_pipes(self):
        t0 = time.time()
        with tempfile.TemporaryDirectory() as d:
            r = engine.run_turn("x", d, self.cfg("import time; time.sleep(30)", 60),
                                should_abort=lambda: time.time() - t0 > 1, check_s=0.2)
        self.assertIn("stopped by /stop", r.error)
        self.assert_closed()

    def test_a_turn_that_ends_normally_closes_its_pipes(self):
        with tempfile.TemporaryDirectory() as d:
            engine.run_turn("x", d, self.cfg("print('not json')", 30))
        self.assert_closed()


if __name__ == "__main__":
    unittest.main()
