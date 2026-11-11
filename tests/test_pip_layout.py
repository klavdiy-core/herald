"""An installed herald (pip install, no repo around it) must still find the example persona for `herald init`,
and the copy inside the package must not drift from the one at the repo root."""
import importlib, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PipLayout(unittest.TestCase):
    def test_persona_found_without_repo_root(self):
        d = tempfile.mkdtemp(prefix="herald_pkg_")
        shutil.copytree(os.path.join(ROOT, "herald"), os.path.join(d, "herald"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        code = "import herald.__main__ as m, os; p = m.persona_example(); print(p); assert p and os.path.isfile(p), p"
        r = subprocess.run([sys.executable, "-c", code], cwd=d, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_package_copy_matches_root(self):
        a = open(os.path.join(ROOT, "persona.example.md"), "rb").read()
        b = open(os.path.join(ROOT, "herald", "persona.example.md"), "rb").read()
        self.assertEqual(a, b, "herald/persona.example.md drifted from persona.example.md")


if __name__ == "__main__":
    unittest.main()
