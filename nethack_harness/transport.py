"""Terminal sockets: the client (Link, Term) and a local pty server for testing."""
import base64
import http.client
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.parse

from .settings import CFG
from .screen import View
from .term import VT


class Held(Exception):
    """Game input is held for now; nothing was sent. Try again shortly."""


class Closed(Exception):
    """The game takes no more input (it ended, or the session is over)."""


class Link:
    """Client for a terminal socket: POST /terminal {"input": base64, "after": n} returns
    {"output": base64, "cursor": n, "truncated": bool}. 410 refuses input ("...over": closed for good,
    "waiting...": held, retry later); 503 means busy. `where` is a Unix socket path, unix:///path,
    or http://host:port."""

    def __init__(self, where):
        self.where = where
        u = urllib.parse.urlparse(where)
        if u.scheme in ("http", "https"):
            self.unix, self.host, self.port, self.prefix = None, u.hostname, u.port, u.path.rstrip("/")
            self.https = u.scheme == "https"
        else:
            self.unix = u.path if u.scheme == "unix" else where
            self.host, self.port, self.prefix, self.https = "localhost", None, "", False

    def post(self, data, after):
        if self.unix:
            conn = http.client.HTTPConnection("localhost", timeout=5)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(self.unix)
            conn.sock = sock
        else:
            cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
            conn = cls(self.host, self.port, timeout=5)
        try:
            body = json.dumps({"input": base64.b64encode(data).decode(), "after": after})
            conn.request("POST", self.prefix + "/terminal", body, {"Content-Type": "application/json"})
            r = conn.getresponse()
            raw = r.read(256 * 1024)
            return r.status, raw
        finally:
            conn.close()


class Term:
    """VT screen kept in sync with the game through a Link."""

    def __init__(self, where):
        self.link = Link(where)
        self.vt = VT()
        self.seen = ""                 # recent decoded output (prayers typed by hand are spotted in it)
        self.cursor = 0
        self.sends = 0
        self.send_time = 0.0

    def poll(self, data=b"", hold_wait=2.0):
        """One exchange with the socket. Input refused as held is retried for hold_wait seconds, then Held is
        raised (nothing was sent). Any other 410 means the game takes no more input (Closed)."""
        end = time.monotonic() + hold_wait
        busy = 0
        while True:
            try:
                status, raw = self.link.post(data, self.cursor)
            except (FileNotFoundError, ConnectionRefusedError) as e:
                if self.cursor:                 # it answered before: the game's terminal is gone
                    raise Closed("terminal socket gone (%s)" % e.__class__.__name__)
                raise
            if status == 200:
                d = json.loads(raw)
                out = base64.b64decode(d.get("output") or "")
                if d.get("truncated") or d["cursor"] < self.cursor:
                    self.vt.reset()
                self.vt.feed(out)
                self.cursor = d["cursor"]
                if out:
                    self.seen = (self.seen + out.decode("utf-8", "replace"))[-65536:]
                return bool(out)
            text = raw.decode(errors="replace").strip()
            if status == 410 and "waiting" in text:
                if time.monotonic() > end:
                    raise Held(text)
                time.sleep(0.25)
                continue
            if status == 410:
                raise Closed(text)
            if status == 503 and busy < 100:
                busy += 1
                time.sleep(0.05)
                continue
            raise RuntimeError("terminal socket answered HTTP %d: %s" % (status, text[:200]))

    def ready(self):
        y, x = self.vt.y, self.vt.x
        lines = self.vt.lines()
        if 1 <= y <= 21 and lines[y][x:x + 1] == "@":
            return True
        return y == 0 or any("--More--" in r or re.search(r"\((end|\d+ of \d+)\)", r) for r in lines)

    def settle(self, status_before=None, multi=False):
        """Wait until the game is quiet. A changed status line with the cursor back on the map means the turn
        finished, so a short quiet suffices; otherwise wait CFG['quiet']. A multi-turn command (a count, travel
        or a run) redraws the status line on the way, so it gets no shortcut and a longer quiet."""
        start = last = time.monotonic()
        need = max(CFG["quiet"], CFG.get("multi_quiet", 0.12)) if multi else CFG["quiet"]
        seen = False
        while True:
            time.sleep(0.02)
            now = time.monotonic()
            if self.poll():
                last, seen = now, True
                continue
            quiet = now - last
            if not multi and seen and quiet >= 0.025 and status_before is not None and \
                    self.vt.lines()[23] != status_before and 1 <= self.vt.y <= 21 and self.ready():
                return
            if (seen and quiet >= need and (self.ready() or quiet > 0.4)) or \
                    (not seen and now - start > 0.4) or now - start > 3:
                return

    def sync(self):
        self.poll()
        for keys in (b"\x12", b"\x1b\x12"):     # joined mid-stream: ask for a full redraw (Ctrl-R)
            if self.vt.complete:
                return
            self.vt.reset()
            self.poll(keys)
            self.settle()

    def send(self, keys):
        started = time.monotonic()
        self.sends += 1
        self.poll()
        before = self.vt.lines()[23]
        data = keys.encode() if isinstance(keys, str) else keys
        self.poll(data)
        self.settle(before, multi=bool(MULTI_TURN.match(data)))
        if COUNTED.match(data) and self.ready() and 1 <= self.vt.y <= 21 and \
                not any("--More--" in r for r in self.vt.lines()):
            # The game can leave T: stale after a counted command (notably one that follows travel or a run),
            # even though the turn counter itself moved on. A redraw (^R) takes no game time and fixes the screen.
            self.poll(b"\x12")
            self.settle()
        self.send_time += time.monotonic() - started

    def view(self):
        vt = self.vt
        return View(vt.lines(), vt.fg, vt.bold, vt.rev, (vt.y, vt.x))


# Commands that take many turns: a count prefix (20s), travel (_), runs (G, shift-moves) and the m/n prefixes.
# With the default runmode the game draws them in steps about 50 ms apart, each redrawing the status line with
# the cursor on the hero, which is why settle gives them no shortcut. (runmode:teleport would avoid the steps,
# but then the game leaves T: stale after a counted command, so the harness keeps the default.)
MULTI_TURN = re.compile(rb"^(?:n?\d+|_|G|[HJKLYUBN]|m[0-9])")
COUNTED = re.compile(rb"^(?:n|m)?\d+")


def serve_local(args):
    """Run nethack in a pty behind a terminal socket (the protocol Link speaks)."""
    import pty
    import struct
    import termios
    import threading
    import fcntl
    from http.server import BaseHTTPRequestHandler
    import socketserver

    state = {"out": bytearray(), "cursor": 0, "exited": False}
    lock, input_lock = threading.Lock(), threading.Lock()
    env = dict(os.environ, TERM="xterm")
    if args.options:
        env["NETHACKOPTIONS"] = args.options
    argv = [args.nethack] + (["-d", args.playground] if args.playground else []) + args.args
    pid, fd = pty.fork()
    if pid == 0:
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~(termios.ISIG | termios.ECHO | termios.ECHONL)
        termios.tcsetattr(0, termios.TCSANOW, attrs)
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.execvpe(argv[0], argv, env)

    def reader():
        while True:
            try:
                data = os.read(fd, 8192)
            except OSError:
                data = b""
            if not data:
                state["exited"] = True
                return
            with lock:
                state["cursor"] += len(data)
                state["out"] += data
                del state["out"][:max(0, len(state["out"]) - 65536)]

    threading.Thread(target=reader, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def address_string(self):
            return "local"

        def log_message(self, *a):
            pass

        def do_POST(self):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                data = base64.b64decode(req.get("input") or "")
                after = int(req.get("after", 0))
            except (ValueError, TypeError):
                return self.reply(400, b"invalid terminal request\n")
            if self.path != "/terminal":
                return self.reply(404, b"not found\n")
            if data:
                with input_lock:
                    if state["exited"]:
                        return self.reply(410, b"game is stopping\n")
                    os.write(fd, data)
            with lock:
                start = state["cursor"] - len(state["out"])
                pos = min(max(after, start), state["cursor"])
                body = {"output": base64.b64encode(bytes(state["out"][pos - start:])).decode(),
                        "cursor": state["cursor"], "truncated": after < start}
            self.reply(200, (json.dumps(body) + "\n").encode())

        def reply(self, code, body):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    if os.path.exists(args.socket):
        os.unlink(args.socket)
    server = Server(args.socket, Handler)
    os.chmod(args.socket, 0o600)
    print(json.dumps({"socket": args.socket, "pid": pid}), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (os.kill(pid, signal.SIGHUP), sys.exit(0)))
    try:
        server.serve_forever()
    finally:
        os.unlink(args.socket)
