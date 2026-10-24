"""python -m herald doctor|run|say --config control/config.json
   python -m herald learn <transcript.jsonl> [--private] [--ignore REGEX] [--out proposals.md]
   python -m herald team <projects folder> --out <shared folder> [--since-hours N] [--ignore REGEX]"""
import argparse
import os
import sys

from . import doctor, learning, run, team, voice


def main(argv=None):
    ap = argparse.ArgumentParser(prog="herald")
    ap.add_argument("cmd", choices=["doctor", "run", "say", "learn", "team"])
    ap.add_argument("transcript", nargs="?", default="")
    ap.add_argument("--config", default="control/config.json")
    ap.add_argument("--text", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--private", action="store_true", help="learn: keep mechanics, drop every quoted message")
    ap.add_argument("--ignore", default="", help="learn: regex for outputs that are signals, not failures")
    ap.add_argument("--since-hours", type=float, default=None, help="team: only sessions touched in this window")
    a = ap.parse_args(argv)
    if a.cmd == "team":                          # transcript = a Claude Code projects/<project> folder
        if not a.transcript or not a.out:
            ap.error("team needs a projects folder and --out")
        res = team.run(a.transcript, a.out, a.since_hours, ignore=a.ignore or None)
        print(f"{len(res['roles'])} role(s), {len(res['shared'])} shared error(s) -> {a.out}")
        return 0
    if a.cmd == "learn":                         # works on any Claude Code transcript, no config needed
        if not a.transcript:
            ap.error("learn needs a transcript path")
        md = learning.render_md(learning.propose(learning.analyze(a.transcript, a.ignore or None), a.private),
                                source=os.path.basename(a.transcript))
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(md)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print(md)
        return 0
    token = os.environ.get("HERALD_TELEGRAM_TOKEN", "")
    cfg = run.load_config(a.config)
    if a.cmd == "doctor":
        _, ecfg = run.build(cfg, token) if token else (None, None)
        if ecfg is None:
            from .engine import EngineConfig
            ecfg = EngineConfig()
        checks = doctor.run_all(ecfg, token, cfg["owners"], (cfg.get("voice") or {}).get("model"))
        print(doctor.render(checks))
        return 0 if doctor.may_start(checks) else 1
    if a.cmd == "say":
        print(voice.synth(a.text, a.out or "say.ogg", voice.VoiceConfig(**(cfg.get("voice") or {}))))
        return 0
    return run.main_run(a.config, token) or 0


if __name__ == "__main__":
    sys.exit(main())
