"""An account with no open 5-hour window (session_pct = None) is a free TARGET when its week is under the bar,
and the proposal says «окно не открыто» instead of a number (lead's choice (b), 08.11)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_accounts_review import A, Base, NOW, tok, urow  # noqa: E402


class NullWindowTarget(Base):
    def test_null_window_low_week_is_a_target(self):
        r = self.reg({"Ann": {"account": "a"}}, accounts=("a", "b"))
        ps = A.propose(r, [urow("a", 90, 30), urow("b", None, 10)], [tok("Ann", 300)], NOW)
        self.assertEqual([(p.worker, p.target) for p in ps], [("Ann", "b")])
        self.assertIn("b: окно не открыто", ps[0].text, ps[0].text)
        self.assertNotIn("None", ps[0].text, ps[0].text)

    def test_null_window_high_week_is_not_a_target(self):
        r = self.reg({"Ann": {"account": "a"}}, accounts=("a", "b"))
        self.assertEqual(A.propose(r, [urow("a", 90, 30), urow("b", None, 60)], [tok("Ann", 300)], NOW), [])

    def test_unknown_week_is_never_free(self):
        r = self.reg({"Ann": {"account": "a"}}, accounts=("a", "b"))
        self.assertEqual(A.propose(r, [urow("a", 90, 30), urow("b", None, None)], [tok("Ann", 300)], NOW), [])

    def test_a_measured_window_is_preferred_over_an_unopened_one_at_equal_week(self):
        r = self.reg({"Ann": {"account": "a"}})
        ps = A.propose(r, [urow("a", 90, 30), urow("b", None, 20), urow("c", 5, 20)], [tok("Ann", 300)], NOW)
        self.assertEqual(ps[0].target, "c", ps[0].text)


if __name__ == "__main__":
    unittest.main()
