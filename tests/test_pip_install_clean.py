"""The package as pip installs it, away from any checkout: every subpackage must be in the wheel.
A git checkout hides a missing subpackage (the folder is simply there), so this builds a real wheel, installs it into a
fresh venv with no index and no checkout on the path, and runs the CLI from an empty folder. Offline: the wheel is built
with the setuptools already here (--no-build-isolation); without setuptools >= 61 the test is skipped, said out loud."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _setuptools_ok():
    try:
        import setuptools
        return int(setuptools.__version__.split(".")[0]) >= 61
    except Exception:
        return False


@unittest.skipUnless(_setuptools_ok(), "setuptools >= 61 is not installed here: the wheel cannot be built offline")
class CleanInstall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="herald_install_")
        src = os.path.join(cls.tmp, "src")
        shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.egg-info", "build", "dist"))
        wheels = os.path.join(cls.tmp, "wheels")
        r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", wheels, src],
                           capture_output=True, text=True, cwd=cls.tmp)
        if r.returncode:
            raise AssertionError("the wheel did not build: " + r.stdout + r.stderr)
        venv = os.path.join(cls.tmp, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True, capture_output=True)
        cls.py = os.path.join(venv, "Scripts" if os.name == "nt" else "bin", "python")
        wheel = os.path.join(wheels, [n for n in os.listdir(wheels) if n.endswith(".whl")][0])
        r = subprocess.run([cls.py, "-m", "pip", "install", "--no-index", "--no-deps", "-q", wheel], capture_output=True, text=True)
        if r.returncode:
            raise AssertionError("the wheel did not install: " + r.stdout + r.stderr)
        cls.empty = os.path.join(cls.tmp, "empty")
        os.mkdir(cls.empty)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_installed(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}      # nothing of the checkout on the path
        return subprocess.run([self.py] + list(args), capture_output=True, text=True, cwd=self.empty, env=env)

    def test_the_installed_package_is_not_the_checkout(self):
        r = self.run_installed("-c", "import herald, os; print(os.path.abspath(herald.__file__))")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(os.path.normcase(ROOT), os.path.normcase(r.stdout))

    def test_every_subpackage_imports(self):
        r = self.run_installed("-c", "import herald.memory, herald.memory.store, herald.memory.sources; print('ok')")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr)

    def test_the_example_persona_ships_in_the_wheel(self):
        r = self.run_installed("-c", "import herald.__main__ as m, os; p = m.persona_example(); assert p and os.path.isfile(p); print('ok')")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr)

    def test_the_cli_starts_outside_any_checkout(self):
        r = self.run_installed("-m", "herald", "--help")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
