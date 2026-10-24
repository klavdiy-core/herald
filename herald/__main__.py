"""python -m herald doctor|run|say --config control/config.json"""
import argparse
import os
import sys

from . import doctor, run, voice


def main(argv=None):
    ap = argparse.ArgumentParser(prog="herald")
    ap.add_argument("cmd", choices=["doctor", "run", "say"])
    ap.add_argument("--config", default="control/config.json")
    ap.add_argument("--text", default="")
    ap.add_argument("--out", default="say.ogg")
    a = ap.parse_args(argv)
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
        print(voice.synth(a.text, a.out, voice.VoiceConfig(**(cfg.get("voice") or {}))))
        return 0
    return run.main_run(a.config, token) or 0


if __name__ == "__main__":
    sys.exit(main())
