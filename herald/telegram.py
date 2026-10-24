"""Telegram Bot API over urllib: hear only the owners, answer only the owners, never leak the token.

Heard: a `message` in a private chat whose chat id equals the sender id and the sender is an owner (not a bot).
Everything else (strangers, groups, edits, callbacks) is acknowledged and counted, never answered.
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

TOKEN_SHAPE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
LIMIT = 4096


class TelegramError(Exception):
    pass


@dataclass
class Message:
    update_id: int
    chat_id: int
    kind: str              # "text" | "voice"
    text: str = ""
    file_id: str = ""
    forwarded: bool = False


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
    def __init__(self, token, owners, api_base="https://api.telegram.org", sleep=time.sleep, timeout=60):
        if not TOKEN_SHAPE.match(token or ""):
            raise ValueError("telegram token does not look like <digits>:<secret>")
        if not owners:
            raise ValueError("owners must not be empty: herald answers no one by default")
        self._token = token
        self._base = f"{api_base}/bot{token}/"
        self.owners = {int(o) for o in owners}
        self._sleep = sleep
        self._timeout = timeout
        self.ignored = 0

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
    def _accept(self, update):
        m = update.get("message")
        if not m:
            return None
        chat, sender = m.get("chat") or {}, m.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot") or chat.get("id") != sender.get("id"):
            return None
        if sender.get("id") not in self.owners:
            return None
        fwd = any(k in m for k in ("forward_origin", "forward_from", "forward_from_chat", "forward_date"))
        if "voice" in m:
            return Message(update["update_id"], chat["id"], "voice", file_id=m["voice"]["file_id"], forwarded=fwd)
        if "text" in m:
            return Message(update["update_id"], chat["id"], "text", text=m["text"], forwarded=fwd)
        return None

    def poll(self, offset, timeout=50):
        """One long poll. Returns (messages heard, next offset)."""
        ups = self._call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
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
        if int(chat_id) not in self.owners:
            raise PermissionError("herald only writes to its owners")

    def send_text(self, chat_id, text):
        self._check(chat_id)
        for part in split_text(text):
            if part:
                self._call("sendMessage", {"chat_id": chat_id, "text": part})

    def send_voice(self, chat_id, ogg_path):
        self._check(chat_id)
        with open(ogg_path, "rb") as f:
            self._call("sendVoice", {"chat_id": str(chat_id)}, files={"voice": (os.path.basename(ogg_path), f.read())})

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
