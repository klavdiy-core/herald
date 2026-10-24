"""Render examples/voice_then_text.png from a real exchange (fake Telegram API, real piper voice in, real
faster-whisper, real Claude turns), recorded 24.10.2026. The lines below are copied from that run's output.
"""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = [
    ("you   [voice message, 2 s]", "you"),
    ("herald  heard: Запомни число 7.", "bot"),
    ("        Запомнил: 7. … Пока мы говорим здесь, число 7 у меня в контексте.", "bot"),
    ("you   Какое число я назвал голосом? Ответь одним словом.", "you"),
    ("herald  Семь.", "bot"),
]


def main():
    try:
        mono, bold = ImageFont.truetype("consola.ttf", 17), ImageFont.truetype("consolab.ttf", 17)
    except OSError:
        mono = bold = ImageFont.load_default()
    img = Image.new("RGB", (900, 60 + 34 * len(ROWS) + 40), (24, 26, 33))
    d = ImageDraw.Draw(img)
    d.text((22, 18), "one conversation: a voice message, then a typed question (real run)", font=mono, fill=(120, 126, 140))
    for i, (t, who) in enumerate(ROWS):
        d.text((22, 60 + i * 34), t, font=bold if who == "you" else mono,
               fill=(110, 190, 230) if who == "you" else (120, 200, 120))
    d.text((22, 60 + 34 * len(ROWS) + 6), "same Claude Code session (--resume): the typed question sees the voice.",
           font=mono, fill=(230, 200, 110))
    img.save(os.path.join(HERE, "voice_then_text.png"))


if __name__ == "__main__":
    main()
