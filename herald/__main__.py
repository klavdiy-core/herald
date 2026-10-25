"""python -m herald doctor|run|say --config control/config.json
   python -m herald learn <transcript.jsonl> [--private] [--ignore REGEX] [--out proposals.md]
   python -m herald team <projects folder> --out <shared folder> [--since-hours N] [--ignore REGEX]
   python -m herald room --config control/config.json   (the agents' room in a browser)
   python -m herald recall <sessions folder> --query WORDS [--db FILE]"""
import argparse
import os
import sys

from . import doctor, learning, recall, run, team, voice


def main(argv=None):
    ap = argparse.ArgumentParser(prog="herald")
    ap.add_argument("cmd", choices=["doctor", "run", "say", "learn", "team", "room", "recall"])
    ap.add_argument("--query", default="", help="recall: words to find in past sessions")
    ap.add_argument("--db", default="", help="recall: index file (default ~/.herald_recall.db)")
    ap.add_argument("transcript", nargs="?", default="")
    ap.add_argument("--config", default="control/config.json")
    ap.add_argument("--text", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--private", action="store_true", help="learn: keep mechanics, drop every quoted message")
    ap.add_argument("--min-sessions", type=int, default=2, help="learn: a lesson must happen in this many sessions")
    ap.add_argument("--ignore", default="", help="learn: regex for outputs that are signals, not failures")
    ap.add_argument("--since-hours", type=float, default=None, help="team: only sessions touched in this window")
    a = ap.parse_args(argv)
    if a.cmd == "recall":                        # transcript = a folder of Claude Code sessions
        if not a.transcript or not a.query:
            ap.error("recall needs a sessions folder and --query")
        db = a.db or os.path.join(os.path.expanduser("~"), ".herald_recall.db")
        n = recall.index(db, a.transcript)
        sys.stdout.reconfigure(encoding="utf-8")
        print(f"(indexed {n} new message(s))\n" + recall.render(recall.search(db, a.query), a.query))
        return 0
    if a.cmd == "team":                          # transcript = a Claude Code projects/<project> folder
        if not a.transcript or not a.out:
            ap.error("team needs a projects folder and --out")
        res = team.run(a.transcript, a.out, a.since_hours, ignore=a.ignore or None)
        print(f"{len(res['roles'])} role(s), {len(res['shared'])} shared error(s) -> {a.out}")
        return 0
    if a.cmd == "learn":                         # works on any Claude Code transcript, no config needed
        if not a.transcript:
            ap.error("learn needs a transcript path")
        # a transcript or a folder of them; a lesson must happen in --min-sessions different sessions (default 2)
        paths = ([os.path.join(a.transcript, n) for n in sorted(os.listdir(a.transcript)) if n.endswith(".jsonl")]
                 if os.path.isdir(a.transcript) else [a.transcript])
        if not paths:
            raise FileNotFoundError(f"no .jsonl transcripts in {a.transcript}")
        rep = learning.analyze_many(paths, a.ignore or None)
        md = learning.render_md(learning.propose(rep, a.private, a.min_sessions),
                                source=f"{os.path.basename(os.path.normpath(a.transcript))}, {len(paths)} session(s)",
                                not_lessons=learning.shown_not_lessons(rep, a.private))
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(md)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print(md)
        return 0
    token = os.environ.get("HERALD_TELEGRAM_TOKEN", "")
    cfg = run.load_config(a.config)
    if a.cmd == "room":                          # the agents' room in a browser, on this machine only
        from . import jev, room_web
        ag = run.room_agents(cfg)
        if not ag:
            ap.error('add an "agents" list to the config first (see README, "The room")')
        rc = cfg.get("room") or {}
        judge = jev.from_env()
        vs = run.room_voices(cfg)
        os.makedirs(cfg["control"], exist_ok=True)
        should_stop, on_spend = run.room_guard(cfg)
        srv = room_web.serve(ag, record_dir=os.path.join(cfg["control"], "rooms"), judge=judge,
                             port=rc.get("port", 8765), pace_s=rc.get("pace_s", 0.3), background=False,
                             synth=voice.synth if vs else None, voices=vs, should_stop=should_stop,
                             on_spend=on_spend, default_budget=rc.get("budget_usd", 0.5))
        print(f"room: {srv.url}\nmoderator: {'Jev' if judge else 'off (set TYPESAFE_API_KEY)'} · Ctrl+C to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    if a.cmd == "doctor":
        ecfg = run.engine_config(cfg)            # the configured model and tools, token or not
        checks = doctor.run_all(ecfg, token, cfg["owners"], (cfg.get("voice") or {}).get("model"))
        print(doctor.render(checks))
        return 0 if doctor.may_start(checks) else 1
    if a.cmd == "say":
        print(voice.synth(a.text, a.out or "say.ogg", voice.VoiceConfig(**(cfg.get("voice") or {}))))
        return 0
    return run.main_run(a.config, token) or 0


def cli(argv=None):
    """A missing file, a folder instead of a file or a broken config is one plain line, not a traceback."""
    try:
        return main(argv)
    except (OSError, ValueError) as e:
        print(f"herald: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
