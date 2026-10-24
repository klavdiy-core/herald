import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from herald import speak_first as sf  # noqa: E402


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.stop = os.path.join(self.tmp.name, "STOP")
        self.clock = Clock(datetime(2026, 10, 24, 12, 0))
        self.limits = sf.Limits(enabled=True)

    def tearDown(self):
        self.tmp.cleanup()

    def gate(self, limits=None):
        return sf.Gate(limits or self.limits, self.state, self.stop, now=self.clock)

    def test_off_by_default(self):
        ok, why = self.gate(sf.Limits()).may_speak()
        self.assertFalse(ok)
        self.assertIn("disabled", why)

    def test_stop_file_silences_everything(self):
        open(self.stop, "w").close()
        ok, why = self.gate().may_speak()
        self.assertFalse(ok)
        self.assertIn("STOP", why)

    def test_quiet_hours_cross_midnight(self):
        g = self.gate()
        for hh, mm, quiet in ((21, 59, False), (22, 0, True), (23, 30, True), (0, 0, True), (8, 59, True), (9, 0, False)):
            self.clock.t = datetime(2026, 10, 24, hh, mm)
            self.assertEqual(not g.may_speak()[0], quiet, f"{hh}:{mm:02}")

    def test_quiet_hours_inside_one_day(self):
        g = self.gate(sf.Limits(enabled=True, quiet=("13:00", "14:00")))
        self.clock.t = datetime(2026, 10, 24, 13, 30)
        self.assertFalse(g.may_speak()[0])
        self.clock.t = datetime(2026, 10, 24, 14, 0)
        self.assertTrue(g.may_speak()[0])

    def test_min_gap_between_messages(self):
        g = self.gate()
        g.record_sent()
        g.record_owner_reply()
        self.clock.t += timedelta(hours=2, minutes=59)
        self.assertFalse(g.may_speak()[0])
        self.clock.t += timedelta(minutes=1)
        self.assertTrue(g.may_speak()[0])

    def test_per_day_cap_resets_at_midnight(self):
        g = self.gate(sf.Limits(enabled=True, min_gap_min=0, quiet=None))
        self.clock.t = datetime(2026, 10, 24, 9, 0)
        for _ in range(3):
            self.assertTrue(g.may_speak()[0])
            g.record_sent()
            g.record_owner_reply()
        ok, why = g.may_speak()
        self.assertFalse(ok)
        self.assertIn("per day", why)
        self.clock.t = datetime(2026, 10, 25, 0, 1)
        self.assertTrue(g.may_speak()[0])

    def test_silence_after_unanswered(self):
        g = self.gate(sf.Limits(enabled=True, min_gap_min=0, quiet=None, per_day=10))
        g.record_sent()
        g.record_sent()
        ok, why = g.may_speak()
        self.assertFalse(ok)
        self.assertIn("unanswered", why)
        g.record_owner_reply()
        self.assertTrue(g.may_speak()[0])

    def test_state_survives_a_restart(self):
        self.gate().record_sent()
        self.clock.t += timedelta(minutes=10)
        self.assertFalse(self.gate().may_speak()[0])

    def test_broken_state_fails_closed(self):
        with open(self.state, "w") as f:
            f.write("{not json")
        ok, why = self.gate().may_speak()
        self.assertFalse(ok)
        self.assertIn("state", why)

    def test_bad_limits_are_refused(self):
        for bad in (dict(per_day=0), dict(min_gap_min=-1), dict(quiet=("25:00", "09:00")), dict(max_unanswered=0)):
            with self.subTest(**bad), self.assertRaises(ValueError):
                sf.Limits(enabled=True, **bad)


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 10, 24, 8, 0))
        self.gate = sf.Gate(sf.Limits(enabled=True), os.path.join(self.tmp.name, "s.json"),
                            os.path.join(self.tmp.name, "STOP"), now=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_job_fires_once_after_its_time(self):
        job = sf.Job("morning", "say good morning", at="09:30")
        self.assertEqual(self.gate.take_due([job]), [])
        self.clock.t = datetime(2026, 10, 24, 9, 30)
        self.assertEqual([j.name for j in self.gate.take_due([job])], ["morning"])
        self.clock.t = datetime(2026, 10, 24, 18, 0)
        self.assertEqual(self.gate.take_due([job]), [])
        self.clock.t = datetime(2026, 10, 25, 9, 31)
        self.assertEqual(len(self.gate.take_due([job])), 1)

    def test_interval_job_and_downtime_fire_once_not_a_burst(self):
        job = sf.Job("check", "look at the inbox", every_min=60)
        self.assertEqual(len(self.gate.take_due([job])), 1)      # never ran: due now
        self.clock.t += timedelta(minutes=59)
        self.assertEqual(self.gate.take_due([job]), [])
        self.clock.t += timedelta(hours=10)                      # the machine was asleep
        self.assertEqual(len(self.gate.take_due([job])), 1)
        self.assertEqual(self.gate.take_due([job]), [])

    def test_job_needs_exactly_one_schedule(self):
        with self.assertRaises(ValueError):
            sf.Job("x", "p")
        with self.assertRaises(ValueError):
            sf.Job("x", "p", at="09:00", every_min=5)


class SilentTest(unittest.TestCase):
    def test_silent_answers(self):
        for t in ("[SILENT]", "  [SILENT]\n", "[silent]", ""):
            self.assertIsNone(sf.what_to_send(t), repr(t))
        self.assertEqual(sf.what_to_send("Good morning! [SILENT] is a word I know."), "Good morning! [SILENT] is a word I know.")


if __name__ == "__main__":
    unittest.main()
