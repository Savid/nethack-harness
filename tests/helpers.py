"""Scripted terminals and local protocol endpoints."""
import base64
import json
import os
import socketserver
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nethack_harness import screen as screen_module  # noqa: E402

def screen(top="", rows=None, status1="Hero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful",
           status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400"):
    lines = [top] + (rows or []) + [""] * 24
    return [line.ljust(80)[:80] for line in lines[:22] + [status1, status2]]


def view(lines=None, cursor=(2, 3), fg=None, bold=None, rev=None):
    return screen_module.View(lines or screen("", [" ---- ", " |.@.| ", " ---- "]),
                              fg or [["default"] * 80 for _ in range(24)],
                              bold or [[False] * 80 for _ in range(24)],
                              rev or [[False] * 80 for _ in range(24)], cursor)


class FakeTerm:
    def __init__(self, views=None):
        self.views = list(views or [view()])
        self.sent = []
        self.on_poll = None

    def poll(self):
        if self.on_poll:
            self.on_poll()
        return False

    def send(self, keys, before_send=None):
        self.poll()
        if before_send:
            before_send(self.view())
        self.sent.append(keys)
        if len(self.views) > 1:
            self.views.pop(0)

    def view(self):
        return self.views[0]


class FakeGame:
    def __init__(self, outputs, refusals=()):
        self.inputs, self.outputs, self.refusals = [], list(outputs), list(refusals)
        self.buf, self.cursor = b"", 0
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "t.sock")
        game = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = base64.b64decode(req["input"])
                if data and game.refusals:
                    code, body = game.refusals.pop(0)
                    return self.reply(code, body)
                if data:
                    game.inputs.append(data)
                    if game.outputs:
                        out = game.outputs.pop(0)
                        game.buf += out
                        game.cursor += len(out)
                body = json.dumps({"output": base64.b64encode(game.buf[req["after"]:]).decode(),
                                   "cursor": game.cursor, "truncated": False}).encode()
                self.reply(200, body)

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(self.path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()


class Endpoint:
    def __init__(self, answer):
        self.requests = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                endpoint.requests.append(request)
                result = answer(request)
                code, result = result if isinstance(result, tuple) else (200, result)
                body = json.dumps(result).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/choose" % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
