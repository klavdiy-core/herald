"""Child of test_herald_cold_resume.py R7: two tail writes, the second one crashes at a chosen point.
argv: repo-root control thread mode   (mode: torn | rep2 | rep3)
  torn : the write of any file under /tails/ ending .md or .md.tmp is cut in the middle on the 2nd write, then the process dies
  rep2 : the process dies before the 2nd os.replace into tails/ (that is, before the old tail is moved to .prev)
  rep3 : the process dies before the 3rd os.replace into tails/ (the old tail is already .prev, the new one is not yet in place)"""
import builtins, os, sys

root, control, thread, mode = sys.argv[1:5]
sys.path.insert(0, root)
from herald import forum  # noqa: E402

real_open, real_replace = builtins.open, os.replace
state = {"w": 0, "r": 0}


class Torn:
    def __init__(self, f):
        self.f = f

    def write(self, d):
        self.f.write(d[: len(d) // 2])
        self.f.flush()
        os._exit(9)

    def __getattr__(self, n):
        return getattr(self.f, n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()


def fopen(path, m="r", *a, **k):
    f = real_open(path, m, *a, **k)
    if "w" in m and "/tails/" in str(path).replace("\\", "/") and str(path).endswith((".md", ".md.tmp")):
        state["w"] += 1
        if mode == "torn" and state["w"] >= 2:
            return Torn(f)
    return f


def frep(a, b):
    if "tails" in str(b):
        state["r"] += 1
        if (mode == "rep2" and state["r"] == 2) or (mode == "rep3" and state["r"] == 3):
            os._exit(9)
    return real_replace(a, b)


builtins.open, os.replace = fopen, frep
for i in range(2):
    forum.write_tail(control, thread, {"done": "step %d" % i, "next": "keep going", "files": "", "turn_at": "2026-11-07T10:00:00Z", "session": "s%d" % i})
