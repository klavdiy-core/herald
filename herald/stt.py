"""Default speech-to-text for herald: `python stt.py [--model small] [--language ru] <audio>` -> transcript on stdout.

Needs `pip install faster-whisper` (local, no network after the model is downloaded once).
"""
import argparse
import sys


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="small")
    p.add_argument("--language", default=None)
    p.add_argument("--device", default="auto", help="auto (GPU if it works, else CPU), cuda or cpu")
    p.add_argument("audio")
    a = p.parse_args(argv)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper is not installed: pip install faster-whisper", file=sys.stderr)
        return 3

    def run(device):
        model = WhisperModel(a.model, device=device, compute_type="int8")
        segments, _info = model.transcribe(a.audio, language=a.language, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()   # segments are lazy: errors surface here

    try:
        text = run(a.device)
    except RuntimeError as e:
        # "auto" may pick a GPU whose CUDA libraries are missing (cublas64_12.dll not found): CPU still works
        if a.device == "cpu":
            raise
        print(f"stt: {a.device} failed ({str(e)[:120]}), falling back to cpu", file=sys.stderr)
        text = run("cpu")
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
