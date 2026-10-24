"""The ears: a voice message -> text, locally, by a separate program with a timeout.

The transcript goes into the SAME Claude Code session as typed messages, so you can speak, then go on typing.
The default program is herald/stt.py (faster-whisper, an optional extra). Any program works that takes the audio
path as its last argument and prints the transcript. Every failure is a HearingError: herald then asks you to type.
"""
import os
import subprocess
import sys
from dataclasses import dataclass, field

STT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stt.py")


class HearingError(Exception):
    pass


@dataclass
class HearConfig:
    stt_cmd: list = field(default_factory=lambda: [sys.executable, STT])
    timeout_s: float = 120
    max_mb: float = 10


def transcribe(audio_path, cfg):
    try:
        r = subprocess.run(list(cfg.stt_cmd) + [audio_path], capture_output=True, timeout=cfg.timeout_s)
    except subprocess.TimeoutExpired:
        raise HearingError(f"speech-to-text: timeout after {cfg.timeout_s:g}s") from None
    except OSError as e:
        raise HearingError(f"speech-to-text: cannot start ({e.strerror})") from None
    if r.returncode != 0:
        raise HearingError(f"speech-to-text: exit {r.returncode}: "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    text = r.stdout.decode("utf-8", "replace").strip()
    if not text:
        raise HearingError("speech-to-text: heard nothing")
    return text
