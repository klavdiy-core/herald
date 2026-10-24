"""A stand-in for piper: reads text on stdin, writes a short real WAV to -f. Mode from env-free argv marker file."""
import math
import os
import struct
import sys
import time
import wave

args = sys.argv[1:]
out = args[args.index("-f") + 1]
model = args[args.index("-m") + 1]
text = sys.stdin.read()
with open(out + ".seen", "w", encoding="utf-8") as f:
    f.write(text + "\n" + " ".join(args))
if os.path.basename(model) == "sleep.onnx":
    time.sleep(30)
if os.path.basename(model) == "fail.onnx":
    sys.exit(3)
with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(22050)
    w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(22050 // 2)))
