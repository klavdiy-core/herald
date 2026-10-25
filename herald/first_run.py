"""herald init: a working control/ folder without editing JSON by hand.

The token comes from HERALD_TELEGRAM_TOKEN only (never an argument: it would land in the shell history). The bot
is checked with getMe, then the owner is asked to send it any message: the sender of that message becomes the
owner, so nobody has to look up a numeric user id. That message is marked as read, so the first run does not answer
it. Existing files are never overwritten.
"""
import json
import os
import time

from . import telegram

DEFAULT_PERSONA = ("You are a careful assistant who works in this folder for one person. Say what you did and what "
                   "you did not do. When you are not sure, say so.\n")


def _config(owner, control):
    return {
        "owners": [owner],
        "workspace": "workspace",
        "control": control,
        "model": "claude-sonnet-5-5",
        "max_turns": 20,
        "max_budget_usd": 1.0,
        "max_usd_per_day": 5.0,
        "timeout_s": 600,
        "tools": ["Read", "Write", "Edit", "Glob", "Grep"],
        "persona_file": os.path.join(control, "persona.md").replace("\\", "/"),
        "speak_first": {"enabled": False},
    }


def wait_for_owner(bot, wait_s=120, poll_s=10, now=time.monotonic):
    """The first private text message to the bot: (user id, first name). The update is then acknowledged."""
    deadline, offset = now() + wait_s, 0
    while now() < deadline:
        ups = bot._call("getUpdates", {"offset": offset, "timeout": poll_s, "allowed_updates": ["message"]},
                        timeout=poll_s + 10) or []
        for u in ups:
            offset = max(offset, int(u.get("update_id", 0)) + 1)
            m = u.get("message") or {}
            sender, chat = m.get("from") or {}, m.get("chat") or {}
            if chat.get("type") == "private" and not sender.get("is_bot") and isinstance(sender.get("id"), int):
                bot._call("getUpdates", {"offset": offset, "timeout": 0})        # mark it read
                return sender["id"], str(sender.get("first_name") or "")
    return None


def init(control, token, api_base="https://api.telegram.org", wait_s=120, poll_s=10, out=print, persona_src=None):
    """Returns 0 when control/config.json was written, 2 when nothing was written (the reason is printed)."""
    cfg_path = os.path.join(control, "config.json")
    if os.path.exists(cfg_path):
        out(f"{cfg_path} already exists; herald init never overwrites it. Edit it, or init another folder.")
        return 2
    if not token:
        out("Set HERALD_TELEGRAM_TOKEN to the token @BotFather gave you, then run herald init again.")
        return 2
    # Bot insists on owners (it answers no one by default); 0 is a placeholder while the owner is not known yet
    bot = telegram.Bot(token, owners={0}, api_base=api_base)
    me = bot._call("getMe") or {}
    name = me.get("username") or "your bot"
    out(f"Bot @{name} is reachable. Send it any message from your own Telegram account now "
        f"(waiting up to {wait_s} s)...")
    found = wait_for_owner(bot, wait_s, poll_s)
    if not found:
        out("No message arrived; nothing was written. Run herald init again and send the bot a message.")
        return 2
    owner, first = found
    os.makedirs(control, exist_ok=True)
    os.makedirs("workspace", exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(_config(owner, control.replace("\\", "/")), f, indent=2)
    persona = os.path.join(control, "persona.md")
    if not os.path.exists(persona):
        text = DEFAULT_PERSONA
        if persona_src and os.path.isfile(persona_src):
            with open(persona_src, encoding="utf-8") as f:
                text = f.read()
        with open(persona, "w", encoding="utf-8") as f:
            f.write(text)
    out(f"Owner: {first or 'you'} (id {owner}). Wrote {cfg_path} (daily cap $5.00, voice off) and {persona}.\n"
        f"Next: python -m herald doctor --config {cfg_path}   then   python -m herald run --config {cfg_path}")
    return 0
