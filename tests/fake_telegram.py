"""A local stand-in for the Telegram Bot API: records every call, answers from a script."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeTelegram:
    def __init__(self):
        self.calls = []          # (method, params dict or raw multipart bytes)
        self.replies = {}        # method -> list of (status, body dict); popped in order, last one repeats
        self.files = {}          # file_path -> bytes, served at /file/bot<token>/<file_path>
        self.file_gets = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.file_gets += 1
                path = self.path.split("/", 3)[-1] if self.path.startswith("/file/") else ""
                data = outer.files.get(path)
                self.send_response(200 if data is not None else 404)
                body = data if data is not None else b'{"ok":false,"error_code":404,"description":"Not Found"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                ctype = self.headers.get("Content-Type", "")
                params = json.loads(body or b"{}") if ctype.startswith("application/json") else body
                outer.calls.append((method, params))
                queue = outer.replies.get(method) or [(200, {"ok": True, "result": True})]
                status, reply = queue.pop(0) if len(queue) > 1 else queue[0]
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def of(self, method):
        return [p for m, p in self.calls if m == method]
