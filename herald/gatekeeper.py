"""The gatekeeper: a separate bot WITHOUT a model that keeps each group's membership equal to groups.json.

Telegram has no per-topic membership, so a task with its own people is its own supergroup; a person or a bot that is
not listed for that group is banned the moment they join, and join requests are approved or declined from the list.
The gatekeeper never posts into a group and never reads messages: it asks Telegram only for membership updates.
Every decision is one line in its audit log. Owners are never banned.

    HERALD_GATEKEEPER_TOKEN=... python -m herald.gatekeeper --groups control/groups.json --log control/gatekeeper.log

groups.json: {"owners": [ids], "groups": [{"chat_id": -100..., "name": "...", "members": [ids], "bots": [ids]}]}
Make this bot an admin of every listed group with the right to ban users and to invite users (for join requests).
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

from . import telegram

ALLOWED_UPDATES = ["chat_member", "chat_join_request"]
JOINED = {"member", "restricted", "administrator", "creator"}
GROUP_KEYS = {"chat_id", "name", "members", "bots"}
TOP_KEYS = {"owners", "groups"}
BAN_TRIES = 3                                            # a failed ban is retried on the next polls, then logged GAVE UP


def _ids(v, where):
    if not isinstance(v, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in v):
        raise ValueError(f"{where} must be a list of numeric ids (not usernames), got {v!r}")
    return frozenset(v)


@dataclass(frozen=True)
class Group:
    chat_id: int
    name: str
    allowed: frozenset


@dataclass
class Groups:
    owners: frozenset = frozenset()
    groups: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) - TOP_KEYS:
            raise ValueError(f"groups.json: unknown keys {sorted(set(data) - TOP_KEYS) if isinstance(data, dict) else data!r}")
        owners = _ids(data.get("owners", []), "owners")
        if not owners:
            raise ValueError("groups.json: owners is empty - an owner would be banned from their own group on the next join")
        out = {}
        for g in data.get("groups", []):
            if not isinstance(g, dict) or set(g) - GROUP_KEYS:
                raise ValueError(f"groups.json: unknown keys in a group: {sorted(set(g) - GROUP_KEYS) if isinstance(g, dict) else g!r}")
            cid = g.get("chat_id")
            if isinstance(cid, bool) or not isinstance(cid, int):
                raise ValueError(f"groups.json: chat_id must be a number, got {cid!r}")
            if cid in out:
                raise ValueError(f"groups.json: chat_id {cid} is listed twice - a typo would silently widen or narrow a group")
            out[cid] = Group(cid, str(g.get("name", "")), _ids(g.get("members", []), "members") | _ids(g.get("bots", []), "bots"))
        return cls(owners, out)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def allowed(self, chat_id, user_id):
        return user_id in self.owners or user_id in self.groups[chat_id].allowed


@dataclass(frozen=True)
class Action:
    kind: str              # ban | approve | decline | log
    chat_id: int
    user_id: int
    why: str = ""


def decide(update, groups):
    """One update -> the actions to take. Pure: no network, no clock."""
    if "chat_member" in update:
        cm = update["chat_member"]
        chat = (cm.get("chat") or {}).get("id")
        new = cm.get("new_chat_member") or {}
        user = (new.get("user") or {}).get("id")
        if chat not in groups.groups:
            return [Action("log", chat, user, "membership update from a chat not in groups.json (a group migrated to a supergroup gets a new chat_id: update groups.json)")]
        if new.get("status") not in JOINED or user is None:
            return []                                    # left, kicked: nothing to guard
        if new.get("status") == "restricted" and new.get("is_member") is False:
            return []                                    # restricted but not in the chat: they left
        if groups.allowed(chat, user):
            return []
        return [Action("ban", chat, user, "not listed for this group")]
    if "chat_join_request" in update:
        jr = update["chat_join_request"]
        chat, user = (jr.get("chat") or {}).get("id"), (jr.get("from") or {}).get("id")
        if chat not in groups.groups:
            return [Action("log", chat, user, "join request to a chat not in groups.json")]
        if user is None:
            return [Action("log", chat, user, "join request without a user: nothing to approve or decline")]
        return [Action("approve" if groups.allowed(chat, user) else "decline", chat, user, "join request")]
    return []


def apply(action, bot, log):
    log(f"{action.kind} chat={action.chat_id} user={action.user_id} ({action.why})")
    if action.kind == "ban":
        bot._call("banChatMember", {"chat_id": action.chat_id, "user_id": action.user_id, "revoke_messages": False})
    elif action.kind == "approve":
        bot._call("approveChatJoinRequest", {"chat_id": action.chat_id, "user_id": action.user_id})
    elif action.kind == "decline":
        bot._call("declineChatJoinRequest", {"chat_id": action.chat_id, "user_id": action.user_id})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m herald.gatekeeper")
    ap.add_argument("--groups", required=True)
    ap.add_argument("--log", required=True)
    a = ap.parse_args(argv)
    token = os.environ.get("HERALD_GATEKEEPER_TOKEN", "")
    if not token:
        print("Set HERALD_GATEKEEPER_TOKEN (the gatekeeper bot's token; never an argument).")
        return 2
    groups = Groups.load(a.groups)
    bot = telegram.Bot(token, owners=set())
    del token

    def log(msg):
        with open(a.log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")

    log(f"gatekeeper up: {len(groups.groups)} groups")
    self_id = None
    try:
        me = bot._call("getMe", {})
        self_id = me.get("id") if isinstance(me, dict) else None
    except telegram.TelegramError as e:
        log(f"getMe failed, own id unknown: {e}")
    if self_id is not None:
        for g in groups.groups.values():
            if self_id not in g.allowed and self_id not in groups.owners:
                log(f"WARNING own bot {self_id} is not in bots: of group {g.chat_id} ({g.name}) - add it, and every peer bot")
    def stamp():
        st = os.stat(a.groups)
        return (st.st_mtime_ns, st.st_size)       # a rewrite inside the same second still changes one of the two

    mtime = stamp()
    pending = []                                         # [action, tries]: failed bans, retried on the next polls
    offset = None

    def attempt(act, tries):
        if act.kind == "ban" and act.user_id == self_id:
            log(f"not banning myself in chat={act.chat_id}: add my id to bots:")
            return
        try:
            apply(act, bot, log)
        except telegram.TelegramError as e:
            if act.kind != "ban":
                log(f"FAILED {act.kind} chat={act.chat_id} user={act.user_id}: {e}")
            elif tries + 1 >= BAN_TRIES:
                log(f"GAVE UP ban chat={act.chat_id} user={act.user_id} after {tries + 1} tries: {e} - check the bot's admin rights")
            else:
                log(f"FAILED ban chat={act.chat_id} user={act.user_id} (try {tries + 1}/{BAN_TRIES}, will retry): {e}")
                pending.append([act, tries + 1])

    while True:
        try:
            m = stamp()
            if m != mtime:
                try:
                    groups = Groups.load(a.groups)
                    log(f"groups.json reloaded: {len(groups.groups)} groups")
                except (OSError, ValueError) as e:
                    log(f"groups.json NOT reloaded, keeping the old list: {e}")
                mtime = m
        except OSError as e:
            log(f"groups.json unreadable, keeping the old list: {e}")
        retry, pending[:] = list(pending), []
        for act, tries in retry:
            if act.chat_id not in groups.groups:
                log(f"retry dropped: chat={act.chat_id} is no longer in groups.json")
            elif groups.allowed(act.chat_id, act.user_id):
                log(f"retry dropped: user={act.user_id} is now listed for chat={act.chat_id}")
            else:
                attempt(act, tries)
        try:
            params = {"timeout": 50, "allowed_updates": ALLOWED_UPDATES}
            if offset is not None:
                params["offset"] = offset
            for u in bot._call("getUpdates", params, timeout=60) or []:
                offset = u["update_id"] + 1
                for act in decide(u, groups):
                    attempt(act, 0)
        except telegram.TelegramError as e:
            log(f"poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
