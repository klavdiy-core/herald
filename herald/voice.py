"""The mouth: text -> local speech (piper, a subprocess) -> OGG/Opus, the format of a Telegram voice message.

Voice is extra, never in the way: every failure is a VoiceError and the caller still sends the text.
Only short plain replies are spoken; code, tables and long answers stay text.
piper is GPL and an optional extra; herald only calls it as a separate program.
"""
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

MAX_CHARS = 600


class VoiceError(Exception):
    pass


@dataclass
class VoiceConfig:
    piper_cmd: list = field(default_factory=lambda: ["piper"])
    model: str = ""
    length_scale: float = 1.0
    ffmpeg: str = "ffmpeg"
    timeout_s: float = 60
    link_word: str = "link"            # what a URL is read as; "ссылка" for a Russian voice
    code_words: str = "(code skipped)"
    pass_env: list = field(default_factory=list)      # variables piper/ffmpeg need beyond the allow-list


def clean_for_speech(text, link_word="link", code_words="(code skipped)"):
    t = re.sub(r"```.*?```", f" {code_words} ", text, flags=re.S)
    t = re.sub(r"https?://\S+", link_word, t)
    t = t.replace("`", "")
    t = re.sub(r"(\*\*|__|\*|_)(\S.*?\S|\S)\1", r"\2", t)
    t = re.sub(r"^\s*(#{1,6}|>|[-*+]|\d+[.)])\s+", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()


def speakable(text):
    if "```" in text or re.search(r"^\s*\|.*\|\s*$\n^\s*\|[\s:|-]+\|\s*$", text, flags=re.M):
        return False
    clean = clean_for_speech(text)
    return 0 < len(clean) < MAX_CHARS


def _run(argv, timeout, what, pass_env=(), **kw):
    from .engine import media_env                       # piper reads the model's text: no bot token, no Jev key (F4)
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, env=media_env(pass_env), **kw)
    except subprocess.TimeoutExpired:
        raise VoiceError(f"{what}: timeout after {timeout:g}s") from None
    except OSError as e:
        raise VoiceError(f"{what}: cannot start ({e.strerror})") from None
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else r.stderr
        raise VoiceError(f"{what}: exit {r.returncode}: {err.strip()[-200:]}")


def synth(text, out_ogg, cfg):
    """Speak `text` into `out_ogg`. Raises VoiceError; never leaves a half-written file or a temp WAV."""
    if not speakable(text):
        raise VoiceError("not speakable: too long, empty, or code/table")
    if not os.path.isfile(cfg.model):
        raise VoiceError(f"voice model not found: {cfg.model}")
    fd, wav = tempfile.mkstemp(suffix=".wav", dir=os.path.dirname(os.path.abspath(out_ogg)))
    os.close(fd)
    tmp_ogg = out_ogg + ".part"
    try:
        _run(list(cfg.piper_cmd) + ["-m", cfg.model, "-f", wav, "--length-scale", str(cfg.length_scale)],
             cfg.timeout_s, "piper", pass_env=cfg.pass_env,
             input=clean_for_speech(text, cfg.link_word, cfg.code_words).encode("utf-8"))
        _run([cfg.ffmpeg, "-v", "error", "-y", "-i", wav, "-ac", "1", "-ar", "48000", "-c:a", "libopus",
              "-b:a", "32k", "-f", "ogg", tmp_ogg], cfg.timeout_s, "ffmpeg", pass_env=cfg.pass_env)
        os.replace(tmp_ogg, out_ogg)
        return out_ogg
    finally:
        for p in (wav, tmp_ogg):
            if os.path.exists(p):
                os.remove(p)
