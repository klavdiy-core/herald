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
    kind: str              # "text" | "voice" | "callback" (a button tap; text = its data)
    text: str = ""
    file_id: str = ""
    forwarded: bool = False
    file_size: int = 0
    callback_id: str = ""


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
        cq = update.get("callback_query")
        if cq:                                  # a button tap: only the owner's, only in the private chat
            sender, chat = cq.get("from") or {}, ((cq.get("message") or {}).get("chat") or {})
            if (sender.get("id") in self.owners and not sender.get("is_bot") and chat.get("type") == "private"
                    and chat.get("id") == sender.get("id") and isinstance(cq.get("data"), str)):
                return Message(update["update_id"], sender["id"], "callback", text=cq["data"],
                               callback_id=str(cq.get("id", "")))
            return None
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
            return Message(update["update_id"], chat["id"], "voice", file_id=m["voice"]["file_id"], forwarded=fwd,
                           file_size=int(m["voice"].get("file_size") or 0))
        if "text" in m:
            return Message(update["update_id"], chat["id"], "text", text=m["text"], forwarded=fwd)
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
        if int(chat_id) not in self.owners:
            raise PermissionError("herald only writes to its owners")

    def send_text(self, chat_id, text):
        self._check(chat_id)
        for part in split_text(text):
            if part:
                self._call("sendMessage", {"chat_id": chat_id, "text": part})

    def send_buttons(self, chat_id, text, buttons):
        """One message with a button per row: buttons = [(label, data), ...], data at most 64 bytes."""
        self._check(chat_id)
        rows = [[{"text": label[:60], "callback_data": data[:64]}] for label, data in buttons]
        parts = [p for p in split_text(text) if p] or ["…"]
        for part in parts[:-1]:                     # a long answer is sent whole; the buttons ride on its last part
            self._call("sendMessage", {"chat_id": chat_id, "text": part})
        self._call("sendMessage", {"chat_id": chat_id, "text": parts[-1], "reply_markup": {"inline_keyboard": rows}})

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
            raise TelegramError(f"download: {self._redact(e)}") from None
        if len(data) > max_bytes:
            raise TelegramError("download: file larger than the limit")
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path

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
