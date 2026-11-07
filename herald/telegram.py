"""Telegram Bot API over urllib: hear only the owners, answer only the owners, never leak the token.

Heard: a `message` in a private chat whose chat id equals the sender id and the sender is an owner (not a bot);
and, only when `forum_chat_id` is set, a message or button tap in THAT forum supergroup from an owner, or a message
from a listed peer bot (numeric ids only; peers never press buttons).
Everything else (strangers, other groups, edits) is acknowledged and counted, never answered.
Forwarded messages are marked so the caller treats their text as untrusted data.
Text goes out plain (no parse_mode): nothing to escape, nothing to fall back from.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

TOPIC_COLOURS = frozenset({0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F})
TOPIC_NAME_LIMIT = 128


def _need_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("topic name must not be empty (Telegram wants 1-128 characters)")
TOKEN_SHAPE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
LIMIT = 4096


class TelegramError(Exception):
    pass


@dataclass
class Message:
    update_id: int
    chat_id: int
    kind: str              # "text" | "voice" | "callback" (a button tap; text = its data)
    text: str = ""
    file_id: str = ""
    forwarded: bool = False
    file_size: int = 0
    callback_id: str = ""
    thread_id: int = 0      # forum topic (message_thread_id); 0 = private chat or the General topic
    from_id: int = 0
    from_bot: bool = False  # True only for a listed peer bot


def split_text(text, limit=LIMIT):
    """Split at line breaks where possible; '\\n'.join(parts) gives the text back."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                    # a single line longer than the limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    parts.append(cur)
    return parts


class Bot:
    def __init__(self, token, owners, api_base="https://api.telegram.org", sleep=time.sleep, timeout=60,
                 forum_chat_id=None, peer_bots=(), log=None, groups=(), her_id=None):
        if not TOKEN_SHAPE.match(token or ""):
            raise ValueError("telegram token does not look like <digits>:<secret>")
        if not owners:
            raise ValueError("owners must not be empty: herald answers no one by default")
        self._token = token
        self._base = f"{api_base}/bot{token}/"
        self.owners = {int(o) for o in owners}
        self.forum_chat_id = int(forum_chat_id) if forum_chat_id is not None else None
        peers = set()
        for p in peer_bots or ():
            if isinstance(p, bool) or not isinstance(p, int):
                raise ValueError("peer_bots must be numeric bot ids, not usernames")
            peers.add(p)
        self.peer_bots = peers
        # groups: [{"chat_id": negative id, "owners": [...], "peer_bots": [...]}] - each listens to its own people only
        self.groups = {}
        for g in groups or ():
            self.groups[int(g["chat_id"])] = ({int(o) for o in g.get("owners", ())}, {int(p) for p in g.get("peer_bots", ())})
        self.her_id = int(her_id) if her_id is not None else None
        self._sleep = sleep
        self._timeout = timeout
        self.ignored = 0
        self.log = log                         # callable(str) or None; set by run.py to the audit log

    def _redact(self, s):
        return str(s).replace(self._token, "<token>").replace(self._token.split(":", 1)[1], "<token>")

    def _call(self, method, params=None, files=None, timeout=None):
        for _ in range(3):
            if files:
                body, ctype = _multipart(params or {}, files)
            else:
                body, ctype = json.dumps(params or {}).encode(), "application/json"
            req = urllib.request.Request(self._base + method, data=body, headers={"Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=timeout or self._timeout) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read())
                except ValueError:
                    data = {"ok": False, "error_code": e.code, "description": e.reason}
                finally:
                    e.close()                  # an HTTPError holds its response: read, then closed, not left to GC
            except (urllib.error.URLError, OSError, ValueError) as e:
                # the message is redacted; `from None` keeps the chained traceback out of logs
                raise TelegramError(f"{method}: {self._redact(e)}") from None
            if data.get("ok"):
                return data.get("result")
            retry = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry:
                self._sleep(retry)
                continue
            raise TelegramError(f"{method}: {data.get('error_code')} {self._redact(data.get('description', ''))}")
        raise TelegramError(f"{method}: still rate limited after retries")

    # ---------- in ----------
    def _where(self, chat, sender):
        """'private' | 'forum' | None. A forum counts only if it is THE forum: id, supergroup and is_forum all match."""
        if chat.get("type") == "private" and chat.get("id") == sender.get("id"):
            return "private"
        if (self.forum_chat_id is not None and chat.get("id") == self.forum_chat_id
                and chat.get("type") == "supergroup" and chat.get("is_forum") is True):
            return "forum"
        if chat.get("id") in self.groups and chat.get("type") == "supergroup":
            return "forum"
        return None

    def _may_speak(self, chat_id, sender_id, is_bot):
        """who is heard in a chat: a group hears its own owners, its own peer bots and her; the legacy forum, as before"""
        if chat_id in self.groups:
            owners, peers = self.groups[chat_id]
            if is_bot:
                return sender_id in peers
            return sender_id in owners or (self.her_id is not None and sender_id == self.her_id)
        if is_bot:
            return sender_id in self.peer_bots
        return sender_id in self.owners

    def _guest_drop(self, update):
        """Guest Mode (a guest_message update, or a message carrying guest_query_id) is never heard: one log line."""
        if self.log:
            try:
                self.log(f"guest message dropped (update {update.get('update_id')})")
            except Exception:                  # noqa: BLE001 — a logger never breaks the poll
                pass

    def _accept(self, update):
        if "guest_message" in update or "guest_query_id" in (update.get("message") or {}):
            self._guest_drop(update)
            return None
        cq = update.get("callback_query")
        if cq:                                  # a button tap: only an owner's (never a bot's), only where we listen
            sender, msg = cq.get("from") or {}, (cq.get("message") or {})
            chat = msg.get("chat") or {}
            if (not sender.get("is_bot") and isinstance(cq.get("data"), str) and self._where(chat, sender)
                    and (self._may_speak(chat.get("id"), sender.get("id"), False) or sender.get("id") in self.owners)):
                return Message(update["update_id"], chat["id"], "callback", text=cq["data"],
                               callback_id=str(cq.get("id", "")), thread_id=int(msg.get("message_thread_id") or 0),
                               from_id=sender["id"])
            return None
        m = update.get("message")
        if not m:
            return None
        chat, sender = m.get("chat") or {}, m.get("from") or {}
        where = self._where(chat, sender)
        if where is None:
            return None
        is_bot = bool(sender.get("is_bot"))
        if is_bot and where != "forum":
            return None
        if where == "private":
            if is_bot or sender.get("id") not in self.owners:
                return None
        elif not self._may_speak(chat.get("id"), sender.get("id"), is_bot):
            return None
        fwd = any(k in m for k in ("forward_origin", "forward_from", "forward_from_chat", "forward_date"))
        extra = dict(thread_id=int(m.get("message_thread_id") or 0), from_id=sender["id"], from_bot=is_bot)
        if "voice" in m:
            return Message(update["update_id"], chat["id"], "voice", file_id=m["voice"]["file_id"], forwarded=fwd,
                           file_size=int(m["voice"].get("file_size") or 0), **extra)
        if "text" in m:
            return Message(update["update_id"], chat["id"], "text", text=m["text"], forwarded=fwd, **extra)
        return None

    def poll(self, offset, timeout=50):
        """One long poll. Returns (messages heard, next offset)."""
        ups = self._call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                         timeout=timeout + 15) or []
        heard = []
        for u in ups:
            offset = max(offset, u["update_id"] + 1)
            m = self._accept(u)
            if m:
                heard.append(m)
            else:
                self.ignored += 1
        return heard, offset

    # ---------- out ----------
    def _check(self, chat_id):
        c = int(chat_id)
        if c not in self.owners and c != self.forum_chat_id and c not in self.groups:
            raise PermissionError("herald only writes to its owners and its forum")

    @staticmethod
    def _with_thread(params, thread_id):
        if thread_id is not None:
            params["message_thread_id"] = int(thread_id)
        return params

    def send_text(self, chat_id, text, thread_id=None):
        self._check(chat_id)
        for part in split_text(text):
            if part:
                self._call("sendMessage", self._with_thread({"chat_id": chat_id, "text": part}, thread_id))

    def send_buttons(self, chat_id, text, buttons, thread_id=None):
        """One message with a button per row: buttons = [(label, data), ...], data at most 64 bytes."""
        self._check(chat_id)
        rows = [[{"text": label[:60], "callback_data": data[:64]}] for label, data in buttons]
        parts = [p for p in split_text(text) if p] or ["…"]
        for part in parts[:-1]:                     # a long answer is sent whole; the buttons ride on its last part
            self._call("sendMessage", self._with_thread({"chat_id": chat_id, "text": part}, thread_id))
        self._call("sendMessage", self._with_thread(
            {"chat_id": chat_id, "text": parts[-1], "reply_markup": {"inline_keyboard": rows}}, thread_id))

    def answer_callback(self, callback_id, text=""):
        """Stops the button's spinner; the text shows as a short toast."""
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def send_voice(self, chat_id, ogg_path):
        self._check(chat_id)
        with open(ogg_path, "rb") as f:
            self._call("sendVoice", {"chat_id": str(chat_id)}, files={"voice": (os.path.basename(ogg_path), f.read())})

    def download(self, file_id, out_path, max_bytes=20 * 1024 * 1024):
        """getFile, check the size BEFORE fetching, then fetch. Errors are redacted like every other call."""
        info = self._call("getFile", {"file_id": file_id}) or {}
        size, path = int(info.get("file_size") or 0), info.get("file_path")
        if not path:
            raise TelegramError("getFile: no file_path")
        if size > max_bytes:
            raise TelegramError(f"getFile: file is {size} bytes, limit {max_bytes}")
        url = self._base.replace("/bot", "/file/bot", 1) + path
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as r:
                data = r.read(max_bytes + 1)
        except (urllib.error.URLError, OSError, ValueError) as e:
            msg = f"download: {self._redact(e)}"         # built first: closing an HTTPError must not change it
            if hasattr(e, "close"):
                e.close()                                 # a 404 HTTPError holds its response
            raise TelegramError(msg) from None
        if len(data) > max_bytes:
            raise TelegramError("download: file larger than the limit")
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path

    # ---------- forum topics (no delete, on purpose) ----------
    def _forum(self, chat_id=None):
        if chat_id is not None:
            if int(chat_id) not in self.groups and int(chat_id) != self.forum_chat_id:
                raise PermissionError("herald makes topics only in its own groups")
            return int(chat_id)
        if self.forum_chat_id is None:
            raise ValueError("forum topics need forum_chat_id")
        return self.forum_chat_id

    def create_topic(self, name, color=None, chat_id=None):
        chat = self._forum(chat_id)
        _need_name(name)
        if color is not None and color not in TOPIC_COLOURS:
            raise ValueError("topic icon colour must be one of Telegram's six")
        params = {"chat_id": chat, "name": name[:TOPIC_NAME_LIMIT]}
        if color is not None:
            params["icon_color"] = color
        return int((self._call("createForumTopic", params) or {})["message_thread_id"])

    def edit_topic(self, thread_id, name, chat_id=None):
        _need_name(name)
        self._call("editForumTopic", {"chat_id": self._forum(chat_id), "message_thread_id": int(thread_id),
                                      "name": name[:TOPIC_NAME_LIMIT]})

    def close_topic(self, thread_id, chat_id=None):
        self._call("closeForumTopic", {"chat_id": self._forum(chat_id), "message_thread_id": int(thread_id)})

    def reopen_topic(self, thread_id, chat_id=None):
        self._call("reopenForumTopic", {"chat_id": self._forum(chat_id), "message_thread_id": int(thread_id)})

    def send_action(self, chat_id, action="typing"):
        self._check(chat_id)
        self._call("sendChatAction", {"chat_id": chat_id, "action": action})


def _multipart(fields, files):
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, data) in files.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                   f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"
