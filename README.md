# herald

**Claude Code in your Telegram, with a voice, locked to one folder.**

> RU: Claude Code в твоём Telegram — отвечает текстом и голосом, может написать первым в заданных пределах и не выходит за свою папку. Защиту держит сам Claude Code (`--restricted`), а herald перед стартом проверяет её живой канарейкой.

You write to a Telegram bot; herald runs one Claude Code turn in a workspace folder and answers in text, and in a
voice message if you want (local speech, no cloud). It can also write first, on a schedule, inside limits written in
code, and it learns from its own sessions, with your approval. Small: about 1300 lines of Python, standard library plus optional `piper-tts` for the voice.

## Try it in one minute (after the bot exists)

You need: Claude Code with the `--restricted` flag, logged in (built and checked on 2.1.310), Python 3.10+, a bot token from [@BotFather](https://t.me/BotFather), your
Telegram user id (ask [@userinfobot](https://t.me/userinfobot)).

```sh
git clone https://github.com/klavdiy-core/herald && cd herald
mkdir control && cp config.example.json control/config.json && cp persona.example.md control/persona.md
# put your user id into "owners" in control/config.json, and remove the "voice" block for now
export HERALD_TELEGRAM_TOKEN=123456:your-token        # Windows: set HERALD_TELEGRAM_TOKEN=...
python -m herald doctor --config control/config.json  # PASS/FAIL per check: claude flags, token (getMe), canary
python -m herald run --config control/config.json
```

Voice (optional): `pip install piper-tts`, `python -m piper.download_voices en_US-lessac-medium --download-dir voices`,
`ffmpeg` with libopus on PATH, put the `voice` block back. Then `/voice both` in the chat.

## What keeps it in its box

Claude Code does the confining, not herald's own code. Every turn runs with fixed flags that no config can remove:

```
claude -p --restricted --permission-prompts none --permission-mode acceptEdits
       --strict-mcp-config --setting-sources "" --settings '{"permissions":{"deny":[...]}}'
```

- `--restricted`: no shell or code-running tools, no WebFetch, file tools only inside the workspace, your own
  settings and hooks are not loaded.
- `--permission-prompts none`: anything that would ask a human is denied (nobody is at the keyboard).
- `acceptEdits` lets it write inside the workspace; the deny rules keep `CLAUDE.md`, `.claude/`, `.git/` and
  `.mcp.json` out of reach even there.
- The prompt goes on stdin, the child process gets an allow-listed environment (no bot token, no API keys unless you
  opt in), every turn has a turn, money and wall-clock limit.

**`herald doctor` checks it on your machine before every start.** A live turn is told to read and write outside the
workspace, give itself Bash in `.claude/settings.json`, write a git hook and a `CLAUDE.md`, and to write one allowed
file. The disk is checked afterwards, not the model's words. If even the allowed write is missing, the canary proves
nothing and doctor fails. That canary found two real holes while herald was being built: without `acceptEdits` the
model could not write at all, and with it alone it could write `CLAUDE.md`.

## Telegram

- Only your private chat is heard: the chat id must equal the sender id and be in `owners`. Strangers, groups, edits
  and bots are counted and never answered.
- Forwarded messages are handed to the model as data, not instructions; a forwarded `/stop` is not a command.
- Commands: `/stop` (no turns, no speaking first, until `/resume`) · `/resume` · `/new` (fresh conversation) ·
  `/voice off|voice|both` · `/status`.
- The token never appears in errors or logs.
- `/stop` is handled before anything else and needs no state: a broken state file cannot block it. A state file that
  cannot be read is kept as `*.bad`; speaking first then stays quiet until you write. An update that crashes its
  handler is logged and passed, never polled again in a loop.

## Hearing: voice and text in one conversation

Send a voice message: herald transcribes it locally and puts it into the **same** Claude Code session as your typed
messages. Speak, then go on typing; it remembers what you said.

![a real exchange: a voice message "Запомни число семь" is heard and answered; the next, typed question "which number did I say by voice?" gets "Семь."](examples/voice_then_text.png)

Add a `hearing` block to the config (`pip install faster-whisper` for the default program):

```json
"hearing": {"timeout_s": 120, "max_mb": 10}
```

The reply starts with `heard: …` so you see what it understood; only the answer is spoken back. Any program can do
the hearing (`"stt_cmd": [...]`, audio path appended, transcript on stdout). The default tries the GPU and falls back
to the CPU when CUDA libraries are missing. A long message (over `max_mb`) is not downloaded; anything that fails
gets "I couldn't hear that one. Please type it." Spoken words are never commands: saying "slash stop" stops nothing.

## Speaking first

Off until `speak_first.enabled` is true. Then a job (`"at": "09:30"` or `"every_min": 60`) may produce a message only if:
no `STOP` file, not in quiet hours, under the daily cap, past the minimum gap, and you answered the last messages. The
model can always choose silence by answering `[SILENT]`. A job is not spent while the gate is closed, and a long sleep
gives one message, not a burst.

## Learning: it gets better at your work, and you approve every lesson

After a session, send `/learn`. herald reads that session's Claude Code transcript and looks for three things:
an error that happened again and again, the step that finally made a failing step work, and a place where you said
"no, that didn't work". It answers with numbered proposals:

```
1. [skill] bash-pitfalls: Bash pitfalls (1) — seen 3 times (Bash), ...
2. [memory] A claim the human corrected — 2026-10-24T10:41:00Z
/approve N · /drop N
```

`/approve 1` keeps it. From then on every turn gets your approved memory and skills in its system prompt. `/memory`
shows what it keeps, `/forget N` removes a line. The store lives in `control/learned/`, where the model cannot write:
it learns nothing you did not approve. After a long turn it reminds you once: "/learn to keep what worked".

The analysis is plain code, no model. On real sessions it ignores what is not a mistake: red test runs done on
purpose, a failing test fixed by editing code (that is the job), text injected by skills, long briefs that happen to
say "again". A skill is one per tool ("Bash pitfalls"), never one per error message.

It works on any Claude Code transcript too, without Telegram:

```sh
python -m herald learn ~/.claude/projects/<project>/<session>.jsonl --private --out proposals.md
```

`--private` keeps only the mechanics (tools, error lines, what fixed them) and drops every quoted message.
`--ignore REGEX` marks outputs that are signals, not failures.

For a team of sessions (each started with `claude --name <role>`), one call reads a whole project folder:

```sh
python -m herald team ~/.claude/projects/<project> --out shared/ --since-hours 24
```

It writes one proposals file per role and an `INDEX.md` with the errors more than one role keeps hitting: those are
the rules worth writing for everyone. It is private by default: an output line that does not read as an error is
withheld (it may be a message someone typed), and commands are cut to the program and its first argument.

The idea (memory and skills in the prompt, nudges after hard work, umbrella skills, "do not save one-off failures")
comes from [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research, MIT; no code is copied.
The difference: Hermes writes its skills itself, herald only proposes.

## Limits, honestly

- Hearing is as good as the speech model: the default `small` model is a fair compromise, `--model medium` hears
  better and slower.
- The voice speaks short plain answers only (no code, no tables, under 600 characters), and a Russian voice reads
  Latin words badly ("herald" comes out as "Эволд").
- The canary is checked with Claude Code 2.1.310 on Windows. Run `doctor` after every Claude Code update.
- One worker: messages are handled one at a time.

## Test

```sh
python -m unittest discover -s tests
```

147 tests against a fake `claude`, a fake Bot API, a fake `piper` and a fake speech-to-text; each module was also checked with planted
breaks (the tests must go red). Live: the canary, a two-turn `--resume` smoke, an end-to-end run with a real Claude turn
and a real voice message out through a fake Bot API, and a voice message in (real piper voice, real faster-whisper)
followed by a typed question in the same session; the learning loop with a real turn (an approved memory line
changed the next answer), and `learn` on three real Claude Code sessions.

## License

MIT
