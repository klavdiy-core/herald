# herald

**Claude Code in your Telegram, with a voice, locked to one folder.**

> RU: Claude Code в твоём Telegram — отвечает текстом и голосом, может написать первым в заданных пределах и не выходит за свою папку. Защиту держит сам Claude Code (`--restricted`), а herald перед стартом проверяет её живой канарейкой.

You write to a Telegram bot; herald runs one Claude Code turn in a workspace folder and answers in text, and in a
voice message if you want (local speech, no cloud). It can also write first, on a schedule, inside limits written in
code. Small: about 700 lines of Python, standard library plus optional `piper-tts` for the voice.

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

## Speaking first

Off until `speak_first.enabled` is true. Then a job (`"at": "09:30"` or `"every_min": 60`) may produce a message only if:
no `STOP` file, not in quiet hours, under the daily cap, past the minimum gap, and you answered the last messages. The
model can always choose silence by answering `[SILENT]`. A job is not spent while the gate is closed, and a long sleep
gives one message, not a burst.

## Limits, honestly

- No hearing yet: a voice message from you gets "please type it".
- The voice speaks short plain answers only (no code, no tables, under 600 characters), and a Russian voice reads
  Latin words badly ("herald" comes out as "Эволд").
- The canary is checked with Claude Code 2.1.310 on Windows. Run `doctor` after every Claude Code update.
- One worker: messages are handled one at a time.

## Test

```sh
python -m unittest discover -s tests
```

100 tests against a fake `claude`, a fake Bot API and a fake `piper`; each module was also checked with planted
breaks (the tests must go red). Live: the canary, a two-turn `--resume` smoke, and an end-to-end run with a real
Claude turn and a real voice message through a fake Bot API.

## License

MIT
