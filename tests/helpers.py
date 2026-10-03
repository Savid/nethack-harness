import base64
import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nethack_harness as nh  # noqa: E402,F401


def screen(top="", rows=None, status1="Hero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful",
           status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400"):
    lines = [top] + (rows or []) + [""] * 24
    lines = lines[:22] + [status1, status2]
    return [line.ljust(80)[:80] for line in lines]


def view(lines, cursor, fg=None, bold=None, rev=None):
    fg = fg or [["default"] * 80 for _ in range(24)]
    bold = bold or [[False] * 80 for _ in range(24)]
    rev = rev or [[False] * 80 for _ in range(24)]
    return nh.View(lines, fg, bold, rev, cursor)


def facts(decisions=1, new_level=False, **kw):
    out = {"decisions": decisions, "new_level": new_level, "dlvl": 2, "hp": 10, "hpmax": 10, "hp_percent": 100,
           "hostiles": [], "screen": ""}
    out.update(kw)
    return out


class FakeGame:
    """A terminal socket that records input and replays scripted output."""

    def __init__(self, outputs, refusals=()):
        self.inputs, self.outputs, self.refusals = [], list(outputs), list(refusals)
        self.buf, self.cursor = b"", 0
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.sock")
        game = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def address_string(self):
                return "test"

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
                after = req["after"]
                start = game.cursor - len(game.buf)
                body = json.dumps({"output": base64.b64encode(game.buf[max(after, start) - start:]).decode(),
                                   "cursor": game.cursor, "truncated": after < start}).encode()
                self.reply(200, body)

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(self.path, Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Endpoint:
    """A SystemOne-compatible stub that answers from a function and records requests."""

    def __init__(self, answer):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                out = json.dumps({"answers": answer(body)}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1/systemone" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
