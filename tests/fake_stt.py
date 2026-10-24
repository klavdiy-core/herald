"""A stand-in for a speech-to-text program: `fake_stt.py <mode> <audio>`; prints the transcript."""
import sys
import time

mode, audio = sys.argv[1], sys.argv[-1]
if mode == "ok":
    print("  Привет, это голосовое.  ")
elif mode == "fail":
    print("half a transcr")                    # a crash may still leave text on stdout; the exit code decides
    print("model not found", file=sys.stderr)
    sys.exit(3)
elif mode == "sleep":
    time.sleep(30)
elif mode == "empty":
    print("   ")
elif mode == "echo":
    print(open(audio, "rb").read().decode("utf-8", "replace"))
