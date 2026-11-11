"""herald init must HAND the example persona to first_run.init (pip-install review: the mutant 'persona_src=None' lived, because test_pip_layout only checks persona_example()).
first_run.init is replaced by a recorder; the package is a copy WITHOUT the repo root around it, so only the packaged persona can be found.
Usage: python -X utf8 test_pip_init_persona.py <herald repo root>   (public-clean: no names, no paths of ours)"""
import os, shutil, subprocess, sys, tempfile, unittest

ROOT = sys.argv.pop(1) if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CODE = """
import os, sys
import herald.first_run as fr
import herald.__main__ as m
seen = {}
def fake_init(control, token, **kw):
    seen.update(kw)
    return 0
fr.init = fake_init
rc = m.main(["init", "--config", os.path.join("ctl", "config.json")])
p = seen.get("persona_src")
print("PERSONA_SRC=%s" % p)
assert rc == 0, rc
assert p and os.path.isfile(p), "init was not given an existing persona file: %r" % (p,)
"""


class InitGetsPersona(unittest.TestCase):
    def test_init_passes_the_packaged_persona(self):
        d = tempfile.mkdtemp(prefix="herald_init_")
        shutil.copytree(os.path.join(ROOT, "herald"), os.path.join(d, "herald"), ignore=shutil.ignore_patterns("__pycache__"))
        r = subprocess.run([sys.executable, "-c", CODE], cwd=d, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
