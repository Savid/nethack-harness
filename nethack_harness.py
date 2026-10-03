#!/usr/bin/env python3
"""nethack-harness: a fast NetHack inner loop for an outer-loop agent.

The harness plays NetHack through a terminal socket. Code does the geometry
(screen parsing, paths, legal actions); a SystemOne-compatible decision
endpoint (typed `choice`/`noul` questions, e.g. TypeSafe Jev or Cloudflare
clef-flash) judges danger and checks the rules' choice on contested steps.
When judgment is needed it pauses and hands a compact situation report to the
outer loop (an agent such as an LLM with a shell), which acts and resumes it.

Commands (run `nethack_harness.py COMMAND --help` for options):
  start   launch the background inner loop, block until it needs you
  wait    block until the next escalation      (exit 0 paused, 2 still running, 3 over)
  resume  continue after an escalation, optionally with new orders; blocks like wait
  pause | stop | status | log
  screen  the screen as the inner loop sees it
  send    send keys yourself while it is paused (prints the new screen)
  probe   ask the decision endpoint about the current screen; sends nothing
  serve-local  run nethack in a pty behind a local terminal socket (testing)

Standard library only (Python 3.9+).
"""
import argparse
import base64
import codecs
import collections
import heapq
import http.client
import importlib.util
import json
import os
import pickle
import random
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"

# Tunables: `--set k=v` on start/resume changes them.
CFG = {
    "mode": "descend",     # descend | explore | careful
    "danger_max": 0.8,     # escalate when danger exceeds this and model and rules disagree (or HP < 50%)
    "p_min": 0.25,         # escalate when risky and the model's top probability is below this
    "hp_escalate": 0.34,   # escalate below this HP fraction under attack when prayer is not safe
    "elbereth_hp": 0.34,   # engrave Elbereth below this HP fraction when threatened
    "rest_hp": 0.75,       # rest below this HP fraction when nothing is in view
    "descend_hp": 0.75,    # take stairs eagerly only above this HP fraction
    "xl_lead": 99,         # do not descend deeper than experience level + this
    "stall": 60,           # escalate after this many decisions without new squares or depth
    "mines": "escalate",   # escalate | allow | avoid: the Gnomish Mines branch
    "avoid": "",           # regex of monster names never to melee
    "calm": 8,             # decisions after a resume without model-based escalations
    "quiet": 0.06,         # seconds of terminal silence that end a key send
    "last_prayer": -1,     # turn of a prayer made by hand (-1: none to record)
}
CAREFUL = {"danger_max": 0.65, "p_min": 0.35, "hp_escalate": 0.5, "elbereth_hp": 0.5, "rest_hp": 0.85, "xl_lead": 2}

DIRS = {"h": (0, -1), "j": (1, 0), "k": (-1, 0), "l": (0, 1), "y": (-1, -1), "u": (-1, 1), "b": (1, -1), "n": (1, 1)}
DN = {"h": "west", "j": "south", "k": "north", "l": "east", "y": "northwest", "u": "northeast", "b": "southwest",
      "n": "southeast"}
ITEMS = set(")[%?/=!(*\"$")
FLOOR = set(".#<>{_^\\") | ITEMS
MON = set("abcdefghijklmnopqrstuvwxyzABCDEFGHJKLMNOPQRSTUVWXYZ@&';:~")  # 'I' marks a remembered unseen monster
CORPSES = ("newt", "jackal", "coyote", "fox", " rat", "iguana", "lichen", "gnome", "orc", "hobbit", "gecko", "dwarf",
           "rothe", "goblin", "pony", "wolf")
ALARM = re.compile(r"slowing down|limbs are stiffening|deathly sick|can't breathe|You turn into|feverish|slimed|"
                   r"swallows you|engulfs you|closed for inventory|Welcome to [A-Z][\w ]*'s|You stole|strangled|"
                   r"You can't move")
YES_NO = [(re.compile(r"Are you sure you want to pray|Unlock it"), "y"),
          (re.compile(r"Really |Still climb|eat it\?|add to the|Continue eating|Shall I pay|Do you want to keep|"
                      r"[Pp]ick (it )?up|no return"), "n")]
CONDITIONS = ("Hungry", "Weak", "Fainting", "Fainted", "Satiated", "Burdened", "Stressed", "Strained", "Blind",
              "Conf", "Stun", "Hallu", "Ill", "FoodPois", "Slime", "Stone", "Strngl", "Lev", "Held", "Trapped")
HIT = re.compile(r"\b(hits|bites|stings|kicks|butts|touches|claws|misses)\b")


# ------------------------------------------------------------- terminal ---

COLORS = ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")
DEC_GRAPHICS = dict(zip("`afgjklmnopqrstuvwxyz{|}~",
                        "◆▒°±┘┐┌└┼⎺⎻─⎼⎽"
                        "├┤┴┬│≤≥π≠£·"))


class VT:
    """A small VT100/xterm screen: enough of the protocol for NetHack's tty port."""

    def __init__(self, rows=24, cols=80):
        self.rows, self.cols = rows, cols
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.reset()

    def reset(self):
        self.chars = [[" "] * self.cols for _ in range(self.rows)]
        self.fg = [["default"] * self.cols for _ in range(self.rows)]
        self.bold = [[False] * self.cols for _ in range(self.rows)]
        self.rev = [[False] * self.cols for _ in range(self.rows)]
        self.y = self.x = 0
        self.attr = ("default", False, False)
        self.top, self.bottom = 0, self.rows - 1
        self.wrap = False
        self.saved = (0, 0, self.attr)
        self.dec = False
        self.esc = ""
        self.complete = False

    def feed(self, data):
        for ch in self.decoder.decode(data):
            if self.esc:
                self.esc += ch
                self._escape()
            elif ch == "\x1b":
                self.esc = ch
            elif ord(ch) < 32 or ch == "\x7f":
                self._control(ch)
            else:
                self._put(ch)

    def lines(self):
        return ["".join(row) for row in self.chars]

    # -- internals
    def _put(self, ch):
        if self.dec:
            ch = DEC_GRAPHICS.get(ch, ch)
        if self.wrap:
            self.x, self.wrap = 0, False
            self._linefeed()
        self.chars[self.y][self.x] = ch
        self.fg[self.y][self.x], self.bold[self.y][self.x], self.rev[self.y][self.x] = self.attr
        if self.x == self.cols - 1:
            self.wrap = True
        else:
            self.x += 1

    def _control(self, ch):
        if ch == "\r":
            self.x, self.wrap = 0, False
        elif ch in "\n\x0b\x0c":
            self.wrap = False
            self._linefeed()
        elif ch == "\b":
            self.x, self.wrap = max(0, self.x - 1), False
        elif ch == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
        elif ch == "\x0e":
            self.dec = True
        elif ch == "\x0f":
            self.dec = False

    def _linefeed(self):
        if self.y == self.bottom:
            self._scroll(self.top, self.bottom, 1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _blank(self, y, x0, x1):
        for x in range(max(0, x0), min(self.cols, x1)):
            self.chars[y][x], self.fg[y][x], self.bold[y][x], self.rev[y][x] = " ", "default", False, False

    def _scroll(self, top, bottom, n):
        """Scroll rows top..bottom up by n (down if n < 0)."""
        for grid in (self.chars, self.fg, self.bold, self.rev):
            blank = {id(self.chars): " ", id(self.fg): "default"}.get(id(grid), False)
            block = grid[top:bottom + 1]
            k = min(abs(n), len(block))
            if n > 0:
                block = block[k:] + [[blank] * self.cols for _ in range(k)]
            else:
                block = [[blank] * self.cols for _ in range(k)] + block[:len(block) - k]
            grid[top:bottom + 1] = block

    def _escape(self):
        s = self.esc
        if len(s) == 2:
            c = s[1]
            if c in "[()#%":
                return
            if c == "7":
                self.saved = (self.y, self.x, self.attr)
            elif c == "8":
                self.y, self.x, self.attr = self.saved
            elif c == "M":
                if self.y == self.top:
                    self._scroll(self.top, self.bottom, -1)
                else:
                    self.y = max(0, self.y - 1)
            elif c == "D":
                self._linefeed()
            elif c == "E":
                self.x = 0
                self._linefeed()
            elif c == "c":
                self.reset()
                self.complete = True
            self.esc = ""
            return
        if s[1] in "()#%":
            if s[1] == "(":
                self.dec = s[2] == "0"
            self.esc = ""
            return
        final = s[-1]
        if "@" <= final <= "~":
            self.esc = ""
            self._csi(s[2:-1], final)
        elif len(s) > 64:
            self.esc = ""

    def _csi(self, params, final):
        private = params[:1] in ("?", ">", "=", "!")
        nums = []
        for p in (params.lstrip("?>=!").split(";") if params else []):
            nums.append(int(p) if p.isdigit() else None)

        def num(i, default=1):
            v = nums[i] if i < len(nums) and nums[i] is not None else default
            return default if v == 0 and default == 1 else v

        self.wrap = False
        if final in "Hf":
            self.y, self.x = min(self.rows - 1, num(0) - 1), min(self.cols - 1, num(1) - 1)
        elif final == "A":
            self.y = max(0, self.y - num(0))
        elif final == "B":
            self.y = min(self.rows - 1, self.y + num(0))
        elif final == "C":
            self.x = min(self.cols - 1, self.x + num(0))
        elif final == "D":
            self.x = max(0, self.x - num(0))
        elif final == "E":
            self.y, self.x = min(self.rows - 1, self.y + num(0)), 0
        elif final == "F":
            self.y, self.x = max(0, self.y - num(0)), 0
        elif final == "G" or final == "`":
            self.x = min(self.cols - 1, num(0) - 1)
        elif final == "d":
            self.y = min(self.rows - 1, num(0) - 1)
        elif final == "J":
            how = num(0, 0)
            if how == 0:
                self._blank(self.y, self.x, self.cols)
                for y in range(self.y + 1, self.rows):
                    self._blank(y, 0, self.cols)
                if self.y == 0 and self.x == 0:
                    self.complete = True
            elif how == 1:
                for y in range(self.y):
                    self._blank(y, 0, self.cols)
                self._blank(self.y, 0, self.x + 1)
            else:
                for y in range(self.rows):
                    self._blank(y, 0, self.cols)
                self.complete = True
        elif final == "K":
            how = num(0, 0)
            if how == 0:
                self._blank(self.y, self.x, self.cols)
            elif how == 1:
                self._blank(self.y, 0, self.x + 1)
            else:
                self._blank(self.y, 0, self.cols)
        elif final == "m" and not private:
            self._sgr(nums or [0])
        elif final == "r" and not private:
            top, bottom = num(0) - 1, (nums[1] if len(nums) > 1 and nums[1] else self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
            self.y = self.x = 0
        elif final in "LM" and self.top <= self.y <= self.bottom:
            self._scroll(self.y, self.bottom, -num(0) if final == "L" else num(0))
        elif final == "S":
            self._scroll(self.top, self.bottom, num(0))
        elif final == "T":
            self._scroll(self.top, self.bottom, -num(0))
        elif final == "P":
            n, row = num(0), self.y
            for grid, blank in ((self.chars, " "), (self.fg, "default"), (self.bold, False), (self.rev, False)):
                line = grid[row]
                grid[row] = line[:self.x] + line[self.x + n:] + [blank] * min(n, self.cols - self.x)
                grid[row] = grid[row][:self.cols]
        elif final == "@":
            n, row = num(0), self.y
            for grid, blank in ((self.chars, " "), (self.fg, "default"), (self.bold, False), (self.rev, False)):
                line = grid[row]
                grid[row] = (line[:self.x] + [blank] * n + line[self.x:])[:self.cols]
        elif final == "X":
            self._blank(self.y, self.x, self.x + num(0))
        elif final in "hl" and private and 1049 in nums and final == "h":
            for y in range(self.rows):
                self._blank(y, 0, self.cols)

    def _sgr(self, nums):
        fg, bold, rev = self.attr
        i = 0
        while i < len(nums):
            n = nums[i] or 0
            if n == 0:
                fg, bold, rev = "default", False, False
            elif n == 1:
                bold = True
            elif n == 22:
                bold = False
            elif n == 7:
                rev = True
            elif n == 27:
                rev = False
            elif 30 <= n <= 37:
                fg = COLORS[n - 30]
            elif n == 39:
                fg = "default"
            elif 90 <= n <= 97:
                fg = "bright" + COLORS[n - 90]
            elif n in (38, 48):
                i += 2 if i + 1 < len(nums) and nums[i + 1] == 5 else 4
            i += 1
        self.attr = (fg, bold, rev)


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
        self.cursor = 0
        self.sends = 0
        self.send_time = 0.0

    def poll(self, data=b""):
        for _ in range(600):
            status, raw = self.link.post(data, self.cursor)
            if status == 200:
                d = json.loads(raw)
                out = base64.b64decode(d.get("output") or "")
                if d.get("truncated") or d["cursor"] < self.cursor:
                    self.vt.reset()
                self.vt.feed(out)
                self.cursor = d["cursor"]
                return bool(out)
            text = raw.decode(errors="replace").strip()
            if status == 410 and "over" in text:
                raise Closed(text)
            if status == 410 and "waiting" in text or status == 503:
                time.sleep(0.5 if status == 410 else 0.05)  # refused before any byte reached the game
                continue
            raise RuntimeError("terminal socket answered HTTP %d: %s" % (status, text[:200]))
        raise RuntimeError("terminal input held for too long")

    def ready(self):
        y, x = self.vt.y, self.vt.x
        lines = self.vt.lines()
        if 1 <= y <= 21 and lines[y][x:x + 1] == "@":
            return True
        return y == 0 or any("--More--" in r or re.search(r"\((end|\d+ of \d+)\)", r) for r in lines)

    def settle(self):
        start = last = time.monotonic()
        seen = False
        while True:
            time.sleep(0.012)
            now = time.monotonic()
            if self.poll():
                last, seen = now, True
            elif (seen and now - last >= CFG["quiet"] and (self.ready() or now - last > 0.4)) or \
                    (not seen and now - start > 0.4) or now - start > 3:
                return

    def sync(self):
        self.poll()
        if not self.vt.complete:   # joined mid-stream: ask the game for a full redraw (Ctrl-R)
            self.vt.reset()
            self.poll(b"\x12")
            self.settle()

    def send(self, keys):
        started = time.monotonic()
        self.sends += 1
        self.poll()
        self.poll(keys.encode() if isinstance(keys, str) else keys)
        self.settle()
        self.send_time += time.monotonic() - started

    def view(self):
        vt = self.vt
        return View(vt.lines(), vt.fg, [[("R" if vt.rev[r][c] else "") + ("B" if vt.bold[r][c] else "")
                                         for c in range(vt.cols)] for r in range(vt.rows)], (vt.y, vt.x))


# ------------------------------------------------------------- parsing ---

class View:
    """One parsed screen: message line, prompts, status, hero position."""

    def __init__(self, rows, fg, at, cursor):
        self.rows, self.fgs, self.at = [r.ljust(80)[:80] for r in rows], fg, at
        msg = self.msg = rows[0].strip()
        self.more = any("--More--" in r for r in rows)
        self.menu = any(re.search(r"\((end|\d+ of \d+)\)\s*$", r.rstrip()) for r in rows[:23])
        m = re.search(r"\[([a-zA-Z\-]+)\](?: \(.\))?\s*$", msg)
        self.yn = m.group(1) if m and not self.more else None
        m = re.search(r"\[([^\]]*?)(?: or \?\*)?\]\s*$", msg)
        self.obj = m.group(1) if m and "What do you want" in msg else None
        if self.obj is not None:
            self.yn = None
        self.text = self.obj is None and bool(re.search(
            r"What do you want to (write|name|call|add)|Call a |Name it|What monster|write in the", msg))
        self.other = bool(re.search(r"direction\?|Where do you want to travel|type a \?|Pick an", msg))
        self.dead = bool(re.search(r"You die\.\.\.|possessions identified|You (starve|drown)|killed by|Goodbye ",
                                   "\n".join(rows)))
        status = rows[22] + " " + rows[23]
        self.st = {}
        for key, rx in (("dlvl", r"Dlvl:(\d+)"), ("gold", r"\$:(\d+)"), ("hp", r"HP:(-?\d+)\((\d+)\)"),
                        ("pw", r"Pw:(\d+)\((\d+)\)"), ("ac", r"AC:(-?\d+)"), ("xl", r"(?:Xp|XL|Exp):(\d+)"),
                        ("turn", r"T:(\d+)")):
            m = re.search(rx, status)
            if m:
                self.st[key] = int(m.group(1))
                if key in ("hp", "pw"):
                    self.st[key + "max"] = int(m.group(2))
        self.cond = [c for c in CONDITIONS if re.search(r"\b%s\b" % c, rows[23])]
        self.title = rows[22].split("St:")[0].strip()
        prompt = self.more or self.menu or self.yn or self.obj is not None or self.text or self.other
        y, x = cursor
        self.hero = (y, x) if 1 <= y <= 21 and self.rows[y][x:x + 1] == "@" else None
        if not self.hero and not prompt:
            ats = [(r, c) for r in range(1, 22) for c in range(80) if self.rows[r][c] == "@"]
            self.hero = min(ats, key=lambda p: abs(p[0] - y) + abs(p[1] - x)) if ats else None
        self.normal = bool(self.hero) and not prompt and not self.dead

    def ch(self, r, c):
        return self.rows[r][c] if 1 <= r <= 21 and 0 <= c < 80 else " "

    def fg(self, r, c):
        return self.fgs[r][c] if 1 <= r <= 21 and 0 <= c < 80 else "default"

    def pet(self, r, c):
        return "R" in self.at[r][c]

    def text_screen(self):
        return "\n".join(r.rstrip() for r in self.rows)


def door(ch, fg):
    return ch in "+|-" and fg in ("brown", "yellow")


def passable(ch, fg, doors=False):
    if ch in FLOOR:
        return not (ch == "#" and fg in ("green", "cyan"))  # trees, iron bars
    return (door(ch, fg) and (ch != "+" or doors)) or ch in MON or ch == "I"


def cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def compass(a, b):
    dr, dc = b[0] - a[0], b[1] - a[1]
    return ("north" if dr < 0 else "south" if dr > 0 else "") + ("west" if dc < 0 else "east" if dc > 0 else "")


def nbrs(p):
    for k, (dr, dc) in DIRS.items():
        yield k, (p[0] + dr, p[1] + dc)


def travel(a, b, prefix="_"):
    """Keys that put the getpos cursor on b (starting from the hero, '@') and confirm it."""
    dr, dc = b[0] - a[0], b[1] - a[1]
    keys = prefix + "@"
    for n, big, small in ((dc, "L", "l"), (-dc, "H", "h"), (dr, "J", "j"), (-dr, "K", "k")):
        if n > 0:
            keys += big * (n // 8) + small * (n % 8)
    return keys + "."


class Level:
    """What the hero has seen and learnt on one dungeon level."""

    def __init__(self):
        self.terr, self.tfg, self.near, self.downs = {}, {}, set(), {}
        self.searched, self.failed, self.kicks = collections.Counter(), collections.Counter(), collections.Counter()
        self.locked, self.blocked, self.statues = set(), set(), set()
        self.up, self.up_branch, self.door_frontier = None, False, set()

    def observe(self, v):
        for r in range(1, 22):
            for c in range(80):
                ch = v.rows[r][c]
                if ch == " " or ch in MON or ch == "I" or (r, c) == v.hero:
                    continue
                self.terr[(r, c)], self.tfg[(r, c)] = ch, v.fgs[r][c]
                yellow = v.fgs[r][c] in ("brown", "yellow") and "B" in v.at[r][c]
                if ch == ">":
                    self.downs[(r, c)] = "branch" if yellow else "main"  # yellow: a branch staircase used before
                elif ch == "<":
                    self.up, self.up_branch = (r, c), yellow
        if v.hero:
            self.see(v.hero)

    def see(self, p):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                self.near.add((p[0] + dr, p[1] + dc))

    def cell(self, v, p):
        ch = v.ch(*p)
        if ch == " " or ch in MON or ch == "I" or p == v.hero:
            if p in self.terr:
                return self.terr[p], self.tfg[p]
            if ch != " ":
                return ".", "default"
        return ch, v.fg(*p)

    def paths(self, v, start):
        """Shortest known-square distances; doors block diagonal steps, traps cost extra."""
        dist, heap = {start: 0}, [(0, start)]
        while heap:
            d, p = heapq.heappop(heap)
            if d > dist[p]:
                continue
            c0 = self.cell(v, p)
            for k, q in nbrs(p):
                if not (1 <= q[0] <= 21 and 0 <= q[1] < 80) or q in self.blocked:
                    continue
                ch, fg = self.cell(v, q)
                if not passable(ch, fg, q not in self.locked) or (k in "yubn" and (door(ch, fg) or door(*c0))):
                    continue
                nd = d + 1 + 8 * (ch == "^") + 3 * (ch == "+")
                if nd < dist.get(q, 1e9):
                    dist[q] = nd
                    heapq.heappush(heap, (nd, q))
        return dist

    def frontier(self, v, dist):
        """Reachable squares beside blank squares the hero has never been next to."""
        out, self.door_frontier = [], set()
        for p, d in dist.items():
            if not d or self.failed[p] >= 3:
                continue
            if any(1 <= q[0] <= 21 and 0 <= q[1] < 80 and v.ch(*q) == " " and q not in self.terr
                   and q not in self.near for _, q in nbrs(p)):
                if self.cell(v, p)[0] == "+":  # aim beside a closed door; opening it is its own action
                    self.door_frontier.add(p)
                    p = min((q for k, q in nbrs(p) if k in "hjkl" and q in dist), key=dist.get, default=p)
                    if not dist.get(p) or self.failed[p] >= 3:
                        continue
                out.append((d + 4 * self.failed[p], p))
        return sorted(out)

    def walked(self, v, a, b):
        """A run or travel went from a to b: mark the likely path as seen up close."""
        dist = self.paths(v, a)
        p = b
        for _ in range(200):
            if p not in dist:
                return
            self.see(p)
            if p == a:
                return
            p = min((q for _, q in nbrs(p) if q in dist), key=lambda q: dist[q], default=a)

    def spots(self, v, dist):
        """Where to look for hidden passages, best first."""
        out = []
        for p, d in dist.items():
            ch, fg = self.cell(v, p)
            blank = [q for k, q in nbrs(p) if k in "hjkl" and v.ch(*q) == " "]
            if any(v.ch(*q) == "`" and v.ch(2 * q[0] - p[0], 2 * q[1] - p[1]) == " " for _, q in nbrs(p)):
                out.append((d - 4, p))  # a boulder with unknown space behind it
            elif door(ch, fg) and ch != "+" and blank:
                out.append((d - 2, p))  # a doorway that leads nowhere yet
            elif ch == "#" and sum(passable(*self.cell(v, q)) and self.cell(v, q)[0] != " " for _, q in nbrs(p)) <= 1:
                out.append((d, p))      # a dead-end corridor
            elif ch == "." and blank and any(self.cell(v, q)[0] in "|-" for _, q in nbrs(p)):
                out.append((d + 6, p))  # a room square beside a wall with nothing known beyond
        return sorted((d + 30 * self.searched[p], p) for d, p in out if self.searched[p] < 3)


class Act:
    def __init__(self, key, desc, keys, kind, prior=0.0, target=None):
        self.key, self.desc, self.keys, self.kind, self.prior, self.target = key, desc, keys, kind, prior, target


def passive(name, ch, fg):
    """Dangerous only to melee and nearly immobile: route around them."""
    if any(w in name for w in ("floating eye", "gas spore", " mold")):
        return True
    return name.startswith("unseen") and ((ch == "e" and fg in ("blue", "default", "white")) or
                                          (ch == "F" and fg != "green"))


# --------------------------------------------------------------- hooks ---

HOOK_API = 1
RULES = ("noul_gte", "noul_lte", "score_gte", "score_lte", "choice_in", "min_confidence")


class HookError(Exception):
    pass


def check_question(key, q):
    if not isinstance(q, dict) or q.get("type") not in ("choice", "noul", "score"):
        raise HookError("question %r needs type choice, noul or score" % key)
    if q["type"] == "choice" and not (isinstance(q.get("criteria"), dict) and q["criteria"]):
        raise HookError("choice question %r needs a criteria map" % key)
    if q["type"] == "score" and not (isinstance(q.get("criteria"), list) and len(q["criteria"]) >= 2):
        raise HookError("score question %r needs an ordered criteria list" % key)
    return {k: v for k, v in q.items() if k in ("type", "instructions", "criteria")}


def rule_fires(rule, answer):
    """Does an escalate_when rule match one answer? All given conditions must hold."""
    if not answer:
        return False
    checks = []
    if "noul_gte" in rule:
        checks.append(answer.get("noul", -1) >= rule["noul_gte"])
    if "noul_lte" in rule:
        checks.append(answer.get("noul", 2) <= rule["noul_lte"])
    if "score_gte" in rule:
        checks.append(answer.get("score", -1e9) >= rule["score_gte"])
    if "score_lte" in rule:
        checks.append(answer.get("score", 1e9) <= rule["score_lte"])
    if "choice_in" in rule:
        checks.append(answer.get("choice") in rule["choice_in"])
    if "min_confidence" in rule:
        checks.append(answer.get("confidence", 0) >= rule["min_confidence"])
    return bool(checks) and all(checks)


class Hooks:
    """Outside strategies, without the inner loop knowing what they are for.

    Declarative questions (JSON): each entry is merged into the step's batched decision call when due and
    pauses the loop (reason "hook:KEY") when its escalate_when rule matches:
      {"key": "shop", "question": {"type": "noul", "instructions": "..."},
       "escalate_when": {"noul_gte": 0.8}, "when": {"every": 10} | {"new_level": true}, "cooldown": 50}
    Plugins (Python files) may define API = 1 and any of:
      extra_questions(facts) -> {key: question}
      on_answers(facts, answers) -> None | {"escalate": reason} | {"action": keys}
      on_resume(facts, orders) -> None
    Plugin errors pause the loop with reason "hook error" instead of crashing it.
    """

    def __init__(self):
        self.questions, self.plugins, self.disabled = {}, {}, set()
        self.fired, self.asked = {}, {}

    def load_questions(self, path):
        with open(path) as f:
            data = json.load(f)
        entries = data.get("questions", []) if isinstance(data, dict) else data
        for e in entries:
            key = str(e.get("key") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", key) or key in ("act", "danger"):
                raise HookError("bad hook key %r" % key)
            rule = e.get("escalate_when") or {}
            if not rule or set(rule) - set(RULES):
                raise HookError("hook %s: escalate_when needs one of %s" % (key, ", ".join(RULES)))
            self.questions[key] = {"question": check_question(key, e.get("question")), "rule": rule,
                                   "when": e.get("when") or {}, "cooldown": int(e.get("cooldown", 0))}
        return list(self.questions)

    def load_plugin(self, path):
        name = re.sub(r"\W", "_", os.path.splitext(os.path.basename(path))[0])
        spec = importlib.util.spec_from_file_location("nethack_harness_plugin_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if getattr(module, "API", HOOK_API) != HOOK_API:
            raise HookError("plugin %s wants hook API %s; this is %d" % (name, module.API, HOOK_API))
        self.plugins[name] = module
        return name

    def enabled(self, key):
        return key not in self.disabled

    def due(self, facts):
        """Questions to ask this decision: {wire key: question}."""
        out = {}
        n = facts["decisions"]
        for key, h in self.questions.items():
            if not self.enabled(key) or n - self.fired.get(key, -10 ** 9) <= h["cooldown"]:
                continue
            when = h["when"]
            if when.get("new_level") and not facts["new_level"]:
                continue
            if when.get("every") and n - self.asked.get(key, -10 ** 9) < int(when["every"]):
                continue
            out[key] = h["question"]
        for name, mod in self.plugins.items():
            if self.enabled(name) and hasattr(mod, "extra_questions"):
                for k, q in (self.call(name, "extra_questions", facts) or {}).items():
                    out["%s.%s" % (name, k)] = check_question(k, q)
        for key in out:
            self.asked[key] = n
        return out

    def judge(self, facts, answers):
        """Returns (reason, keys): an escalation reason and/or keys a plugin wants sent instead."""
        n = facts["decisions"]
        for key, h in self.questions.items():
            if key in answers and rule_fires(h["rule"], answers[key]):
                self.fired[key] = n
                return "hook:%s %s" % (key, describe(answers[key])), None
        for name, mod in self.plugins.items():
            if not self.enabled(name) or not hasattr(mod, "on_answers"):
                continue
            own = {k.split(".", 1)[1]: v for k, v in answers.items() if k.startswith(name + ".")}
            own.update({k: v for k, v in answers.items() if k in ("act", "danger")})
            out = self.call(name, "on_answers", facts, own) or {}
            if out.get("escalate"):
                return "hook:%s %s" % (name, str(out["escalate"])[:200]), None
            if out.get("action"):
                return None, str(out["action"])
        return None, None

    def resumed(self, facts, orders):
        for name, mod in self.plugins.items():
            if self.enabled(name) and hasattr(mod, "on_resume"):
                self.call(name, "on_resume", facts, orders)

    def call(self, name, fn, *args):
        try:
            return getattr(self.plugins[name], fn)(*args)
        except Exception as e:
            raise HookError("hook error in %s.%s: %s: %s" % (name, fn, type(e).__name__, e))


def normalize(answers):
    """Engines differ in optional fields: make choice answers carry choice, probabilities and confidence."""
    out = {}
    for key, ans in (answers or {}).items():
        if not isinstance(ans, dict):
            continue
        ans = dict(ans)
        probs = ans.get("probabilities")
        if "choice" in ans or (isinstance(probs, dict) and ans.get("type") == "choice"):
            if not isinstance(probs, dict) or not probs:
                probs = {ans.get("choice"): float(ans.get("confidence", 1.0))}
            probs = {str(k): float(v) for k, v in probs.items()}
            ans["probabilities"] = probs
            ans.setdefault("choice", max(probs, key=probs.get))
            ans.setdefault("confidence", max(probs.values()))
        for k in ("noul", "score", "confidence"):
            if k in ans:
                ans[k] = float(ans[k])
        out[key] = ans
    return out


def describe(answer):
    if "noul" in answer:
        return "(yes %.2f)" % answer["noul"]
    if "choice" in answer:
        return "(%s %.2f)" % (answer["choice"], answer.get("confidence", 0))
    if "score" in answer:
        return "(score %.2f)" % answer["score"]
    return ""


# --------------------------------------------------------------- pilot ---

class Pilot:
    """The inner loop. step() plays one decision and returns an escalation reason or None."""

    def __init__(self, term, decide):
        self.term, self.decide = term, decide
        self.lv = collections.defaultdict(Level)
        self.names = {}
        self.no_food, self.options = 0, False
        self.last_prayer = None
        self.hist, self.msgs = collections.deque(maxlen=80), collections.deque(maxlen=12)
        self.keys = self.decisions = self.calls = self.escs = 0
        self.mtime, self.t0, self.mark, self.progress = 0.0, time.time(), None, 0
        self.directive, self.max_dl, self.prev_dl, self.mines_noted = "", 0, None, False
        self.obst, self.hostiles, self.pending, self.last_act, self.last_try = [], [], None, None, None
        self.corpse, self.low_noted, self.turn, self.bad_screens, self.hit_turn = None, None, None, 0, -99
        self.futile, self.calm_until, self.seen_levels, self.hook_answers = collections.Counter(), 0, set(), {}
        self.rng = random.Random(0)
        self.log = None
        self.hooks = Hooks()

    # -- bookkeeping
    def view(self):
        v = self.term.view()
        if v.normal:
            self.lv[v.st.get("dlvl", 0)].observe(v)
        return v

    def note(self, kind, text, **kw):
        kw.update(step=self.keys, kind=kind, text=text, t=round(time.time() - self.t0, 1))
        self.hist.append(kw)
        if self.log:
            self.log.write(json.dumps(kw) + "\n")
            self.log.flush()

    def send(self, keys):
        self.term.send(keys)
        self.keys += 1

    def esc(self, reason, **kw):
        self.note("escalate", reason, turn=self.turn, **kw)
        return None if CFG.get("auto") else reason   # auto: benchmarks log escalations and play on

    def message(self, text, v):
        if HIT.search(text):
            self.hit_turn = v.st.get("turn", 0)
        self.msgs.append(text)
        self.note("msg", text[:200])
        if "You begin praying" in text and v.st.get("turn") is not None:
            if self.last_prayer is None or v.st["turn"] - self.last_prayer > 5:
                self.last_prayer = v.st["turn"]
        m = re.search(r"You (?:kill|destroy) (?:the |an? )?([a-z -]+?)!", text)
        if m and self.last_act and self.last_act.kind == "attack":
            self.corpse = (v.st.get("dlvl"), self.last_act.target, m.group(1), v.st.get("turn", 0))
        if "This door is locked" in text and self.last_act and self.last_act.kind == "door":
            self.lv[v.st.get("dlvl", 0)].locked.add(self.last_act.target)
        if ALARM.search(text):
            return self.esc("alarming message: " + text[:160])

    def interrupts(self, v):
        if v.dead:
            return "game_over"
        if v.more:
            text = " ".join(r.strip() for r in v.rows[:3] if r.strip()).replace("--More--", "").strip()
            reason = self.message(text, v) if text else None
            self.send(" ")
            return reason
        if v.menu or v.other or v.text or v.obj is not None:
            self.note("cancel", v.msg[:100])
            self.send("\x1b")
            return None
        if v.yn:
            for rx, answer in YES_NO:
                if rx.search(v.msg):
                    self.note("prompt", v.msg[:100], answer=answer)
                    self.send(answer)
                    return None
            reason = self.esc("unknown prompt: " + v.msg[:160])
            if not reason:
                self.send("\x1b")
            return reason
        if v.msg and v.normal and (not self.msgs or self.msgs[-1] != v.msg):
            return self.message(v.msg, v)
        return None

    def setup(self):
        """In-game options: hilite_pet (spot the pet), time (prayer timing), autoopen; no sparkle or
        timed_delay (speed). The tty options menu toggles a boolean at once and redraws page 1."""
        want = {"hilite_pet": True, "time": True, "sparkle": False, "timed_delay": False, "autoopen": True}

        def toggle(rows):
            for line in rows:
                m = re.match(r"\s*([a-zA-Z]) - (\S+)\s+\[(.)\]", line)
                if m and m.group(2) in want and (m.group(3) == "X") != want[m.group(2)]:
                    return m.group(1)
            return None

        self.send("O")
        for _ in range(12):
            rows = self.term.view().rows
            k = toggle(rows)
            if not k and re.search(r"\(1 of \d+\)", "\n".join(rows)):
                self.send(">")
                k = toggle(self.term.view().rows)
            if not k:
                break
            self.send(k)
        for _ in range(4):
            if self.term.view().normal:
                break
            self.send("\x1b")
        self.options = True
        self.note("setup", "options: hilite_pet, time, autoopen on; sparkle, timed_delay off")

    def farlook(self, v, p):
        key = (v.st.get("dlvl"), p, v.ch(*p), v.fg(*p))
        if key not in self.names:
            self.term.send(travel(v.hero, p, ";"))
            text = self.term.view().msg
            for _ in range(3):
                w = self.term.view()
                if w.more:
                    self.term.send(" ")
                elif w.menu or w.other:
                    self.term.send("\x1b")
                else:
                    break
            m = re.findall(r"\(([^()]*)\)", text)
            self.names[key] = (m[-1] if m else re.sub(r"^\S\s+", "", text)).strip()[:60] or "unknown"
        return self.names[key]

    # -- one decision
    def context(self, v):
        st, hero = v.st, v.hero
        dl = st.get("dlvl", 0)
        lv = self.lv[dl]
        hp, hpmax, xl, turn = st.get("hp", 1), max(1, st.get("hpmax", 1)), st.get("xl", 1), st.get("turn", 0)
        c = {"dl": dl, "lv": lv, "hero": hero, "hp": hp, "hpmax": hpmax, "hpf": hp / hpmax, "turn": turn, "xl": xl}
        hostile, peace, obst = [], [], []
        for r in range(1, 22):
            for col in range(80):
                p, ch = (r, col), v.rows[r][col]
                if ch not in MON or p == hero or v.pet(r, col) or p in lv.statues:
                    continue
                d, fg, name = cheb(p, hero), v.fgs[r][col], None
                if d <= 5 and ch != "~":
                    name = self.farlook(v, p)
                    if "tame" in name:
                        continue
                    if "statue" in name:
                        lv.statues.add(p)
                        continue
                m = {"pos": p, "ch": ch, "fg": fg, "dist": d, "name": name or "unseen %s %s" % (fg, ch)}
                bad = passive(m["name"], ch, fg) or (CFG["avoid"] and re.search(CFG["avoid"], m["name"]))
                (obst if bad else peace if "peaceful" in m["name"] else hostile).append(m)
        hostile.sort(key=lambda h: h["dist"])
        self.hostiles, self.obst = hostile, obst
        c.update(hostiles=hostile, peace=peace, obst=obst)
        lv.blocked = {h["pos"] for h in obst} | lv.statues
        c["threats"] = [h for h in hostile if h["dist"] <= 2]
        c["dist"] = lv.paths(v, hero)
        c["under"] = lv.terr.get(hero, "?")
        c["frontier"] = lv.frontier(v, c["dist"])
        div = 5 if xl <= 5 else 6 if xl <= 13 else 7   # the game's own "low HP" rule for prayer
        c["trouble"] = hp <= 5 or hp * div <= min(hpmax, 15 * xl) or \
            any(x in v.cond for x in ("Weak", "Fainting", "Fainted", "FoodPois", "Ill", "Stone", "Slime", "Strngl"))
        lp = self.last_prayer
        c["can_pray"] = (lp is None and turn > 120) or (lp is not None and turn - lp > 800)
        c["hungry"] = next((x for x in v.cond if x in ("Hungry", "Weak", "Fainting", "Fainted")), None)
        c["hit"] = turn - self.hit_turn <= 2
        return c

    def actions(self, v, c):
        """Legal macro actions with rule priors (higher is better)."""
        hero, lv, dist, hpf, th, hs = c["hero"], c["lv"], c["dist"], c["hpf"], c["threats"], c["hostiles"]
        acts, hurt, dl, fr = [], hpf < 0.5, c["dl"], c["frontier"]
        for k, q in nbrs(hero):
            ch, fg = v.ch(*q), v.fg(*q)
            m = next((h for h in hs if h["pos"] == q), None)
            if m:
                acts.append(Act("attack_" + k, "Attack the %s adjacent %s" % (m["name"], DN[k]), "F" + k, "attack",
                                2 if hurt else 4, q))
            elif ch == "I" and (c["hit"] or "Blind" in v.cond):
                acts.append(Act("attack_" + k, "Attack the unseen monster to the " + DN[k], "F" + k, "attack", 3.5, q))
            elif ch == "`" and v.ch(2 * q[0] - hero[0], 2 * q[1] - hero[1]) in " .#":
                acts.append(Act("push_" + k, "Push the boulder " + DN[k], k, "move", -1 if fr else 3, q))
            elif ch == "+" and door(ch, fg) and k in "hjkl":
                if q not in lv.locked:
                    blocked = any(re.search(r"door is closed|bump into a door", x) for x in list(self.msgs)[-2:])
                    new = q in lv.door_frontier or v.ch(2 * q[0] - hero[0], 2 * q[1] - hero[1]) == " "
                    acts.append(Act("open_" + k, "Open the closed door to the " + DN[k], k, "door",
                                    6 if blocked else 3 if new else 1 if fr else 2.4, q))
                elif lv.kicks[q] < 6:
                    acts.append(Act("kick_" + k, "Kick open the locked door to the " + DN[k], "\x04" + k, "kick",
                                    0.8 if fr else 2.2, q))
            elif any(h["dist"] <= 3 for h in hs) and dist.get(q) == 1 and passable(ch, fg) and ch not in MON:
                t = (th or hs)[0]
                nd = cheb(q, t["pos"])
                rel = "away from" if nd > t["dist"] else "toward" if nd < t["dist"] else "beside"
                what = {"#": "corridor", "<": "the up stairs", ">": "the down stairs", "^": "a TRAP"}.get(ch, "floor")
                acts.append(Act("move_" + k, "Step %s onto %s (%s the %s)" % (DN[k], what, rel, t["name"]), k, "move",
                                0.5 if rel == "away from" and hurt and t["dist"] == 1 else -1, q))
        ok = dl - c["xl"] < CFG["xl_lead"]
        avoid = CFG["mines"] == "avoid"
        if c["under"] == ">":
            branch = lv.downs.get(hero) == "branch"
            p = 3.5 if th and hurt else 6 if hpf >= CFG["descend_hp"] else 2
            acts.append(Act("descend", "Go down the stairs here to Dlvl %d%s" % (dl + 1, " (Gnomish Mines)" * branch),
                            ">", "descend", (0.2 if branch and avoid else p) if ok else -3))
        downs = sorted((kind == "branch" and CFG["mines"] != "allow", dist[p], p, kind) for p, kind in lv.downs.items()
                       if p in dist and p != hero)
        if downs:
            _, d, p, kind = downs[0]
            acts.append(Act("goto_stairs", "Travel to the down stairs %d squares %s%s" % (
                d, compass(hero, p), " (Gnomish Mines)" * (kind == "branch")), travel(hero, p), "travel",
                (0.4 if kind == "branch" and avoid else 1.5 if th else 5) if ok else -2, p))
        if lv.up_branch and 2 <= dl <= 5 and avoid and not th:
            acts.append(Act("leave_mines", "Leave the Gnomish Mines by the up stairs",
                            "<" if c["under"] == "<" else travel(hero, lv.up or hero), "travel", 7, lv.up))
        if c["under"] == "<" and dl > 1 and th and hurt:
            acts.append(Act("flee_up", "Escape up the stairs you stand on", "<", "flee", 3 if hpf < 0.34 else 1))
        if fr:
            d, p = self.rng.choice([x for x in fr if x[0] <= fr[0][0] + 1])
            keys = travel(hero, p)
            if d == 1 and lv.cell(v, p)[0] == "#":
                keys = "G" + next(k for k, q in nbrs(hero) if q == p)   # follow the corridor in one command
            usable = [x for x in downs if not (x[3] == "branch" and avoid)]
            pr = (2.5 if not usable or CFG["mode"] == "explore" else 0.5) - (2.5 if th else 0)
            acts.append(Act("explore", "Explore toward the nearest unexplored area, %d squares %s" % (
                d, compass(hero, p)), keys, "explore", pr, p))
        if not hs and not c["hit"]:
            if not fr and not downs:
                sp = lv.spots(v, dist)
                if sp and sp[0][1] != hero:
                    acts.append(Act("goto_search", "Go to a likely hidden-passage spot %s to search" %
                                    compass(hero, sp[0][1]), travel(hero, sp[0][1]), "explore", 2, sp[0][1]))
            spot = not fr and not downs and lv.searched[hero] < 3 and hero in [q for _, q in lv.spots(v, dist)]
            acts.append(Act("search", "Search here 15 turns for hidden passages" +
                            " (nothing left to explore)" * (not fr and not downs), "15s", "search",
                            2.5 if spot else (1.6 if not fr and not downs else -2) - lv.searched[hero]))
            if hpf < CFG["rest_hp"]:
                acts.append(Act("rest", "Rest 20 turns to regain HP (HP %d/%d, no monsters in view)" % (
                    c["hp"], c["hpmax"]), "20s", "rest", 3 + 2 * (hpf < 0.35) - (c["hungry"] is not None)))
        if not acts:
            acts.append(Act("search", "Search here 15 turns", "15s", "search", -3))
        if hs and not any(h["dist"] == 1 for h in hs):
            acts.append(Act("wait", "Wait a turn and let monsters come to you", "s", "wait",
                            0.8 if hurt and th else -1.5))
        cs = self.corpse
        if c["hungry"] and cs and cs[0] == dl and c["turn"] - cs[3] <= 40 and cs[2].endswith(CORPSES) \
                and not th and cs[2].split()[-1] not in v.title.lower():
            if hero == cs[1]:
                acts.append(Act("eat_corpse", "Eat the fresh %s corpse here (you are %s)" % (cs[2], c["hungry"]),
                                "e", "eat_corpse", 5))
            elif dist.get(cs[1], 99) <= 4:
                acts.append(Act("goto_corpse", "Step to the fresh %s corpse to eat it" % cs[2],
                                travel(hero, cs[1]), "travel", 4.5, cs[1]))
        if c["hungry"] and not self.no_food:
            acts.append(Act("eat", "Eat food from your pack (you are %s)" % c["hungry"], "e", "eat",
                            (0 if th else 3.5) + 3 * (c["hungry"] != "Hungry")))
        if c["can_pray"] and c["trouble"]:
            acts.append(Act("pray", "Pray for help (in serious trouble; prayer timeout looks safe)", "", "pray", 9))
        if th and hpf < max(0.5, CFG["elbereth_hp"]) and \
                not any(h["ch"] in "@A" or "minotaur" in h["name"] for h in th):
            acts.append(Act("elbereth", "Engrave Elbereth in the dust to scare monsters away", "", "elbereth",
                            2.5 if hpf < CFG["elbereth_hp"] else -0.5))
        for a in acts:   # actions that achieved nothing here before sink
            a.prior -= 3 * self.futile[(dl, hero, a.key)]
        return sorted(acts, key=lambda a: -a.prior)

    def state(self, v, c):
        """The decision state: structured facts plus the raw screen (best measured encoding)."""
        h = c["hero"]
        crop = ["".join("P" if (r, x) != h and 1 <= r <= 21 and v.ch(r, x) in MON and v.pet(r, x) else v.ch(r, x)
                        for x in range(h[1] - 8, h[1] + 9)) for r in range(h[0] - 4, h[0] + 5)]
        s = {"goal": "Get as deep as possible in a timed NetHack race without dying (mode: %s)." % CFG["mode"],
             "hero": {"Dlvl": c["dl"], "HP": "%d of %d" % (c["hp"], c["hpmax"]), "HP_percent": int(100 * c["hpf"]),
                      "AC": v.st.get("ac"), "XL": c["xl"], "turn": c["turn"], "status": v.cond or ["normal"],
                      "who": v.title},
             "hostile_monsters": ["%s, %d squares %s" % (x["name"], x["dist"], compass(h, x["pos"]))
                                  for x in c["hostiles"][:6]] or ["none in view"],
             "standing_on": {">": "down stairs", "<": "up stairs"}.get(c["under"], "floor"),
             "messages": list(self.msgs)[-3:],
             "recent_actions": [x["text"].split(":")[0] for x in self.hist if x["kind"] == "act"][-8:],
             "map_around_hero": crop,
             "map_legend": "@ hero (center), P pet, letters monsters, . floor, # corridor, | - walls, + door, "
                           "> down stairs, < up stairs, blank unknown"}
        if c["peace"]:
            s["peaceful_monsters"] = [x["name"] for x in c["peace"][:4]]
        if c["obst"]:
            s["do_not_melee"] = [x["name"] for x in c["obst"][:4]]
        if self.directive:
            s["orders"] = self.directive
        s["screen"] = v.text_screen()
        return s

    def facts(self, v, c, new_level=False):
        """What hooks see: plain facts about this decision, plus the decision state."""
        return {"api": HOOK_API, "dlvl": c["dl"], "hp": c["hp"], "hpmax": c["hpmax"], "hp_percent": int(100 * c["hpf"]),
                "xl": c["xl"], "turn": c["turn"], "conditions": list(v.cond), "new_level": new_level,
                "hostiles": [h["name"] for h in c["hostiles"]], "standing_on": c["under"],
                "messages": list(self.msgs)[-3:], "decisions": self.decisions, "keys": self.keys,
                "mode": CFG["mode"], "orders": self.directive, "screen": v.text_screen(),
                "state": self.state(v, c)}

    def ask(self, v, c, acts, extra=None):
        questions = {}
        if acts:
            order = list(acts)
            self.rng.shuffle(order)   # cheap insurance against position bias
            questions = {
                "act": {"type": "choice", "criteria": {a.key: a.desc for a in order},
                        "instructions": "Choose the hero's best next action. Survive first: do not melee when HP is "
                                        "low if a safer option exists. Otherwise make progress: descend when on the "
                                        "down stairs, travel to known down stairs, kill weak monsters in the way, "
                                        "explore."},
                "danger": {"type": "noul", "instructions": "Could the hero plausibly die within the next 5 turns "
                                                           "(very low HP, dangerous monster adjacent, or a deadly "
                                                           "condition)?"}}
        questions.update(extra or {})
        started = time.monotonic()
        res = self.decide({"state": self.state(v, c), "questions": questions})
        took = time.monotonic() - started
        self.calls += 1
        self.mtime += took
        return normalize(res.get("answers")), took

    def do(self, v, c, a):
        lv = c["lv"]
        self.last_act = a
        if a.kind == "pray":
            self.last_prayer = c["turn"]
            self.send("#pray\r")
            for _ in range(12):
                w = self.term.view()
                if w.yn and "pray" in w.msg:
                    self.send("y")
                elif w.more:
                    self.message(" ".join(r.strip() for r in w.rows[:2]).replace("--More--", "").strip(), w)
                    self.send(" ")
                else:
                    break
            return
        if a.kind == "elbereth":
            self.send("E")
            for _ in range(8):
                w = self.term.view()
                if w.obj is not None:
                    self.send("-")
                elif w.yn:
                    self.send("n" if "add to" in w.msg else "y")
                elif w.more:
                    self.send(" ")
                elif w.text:
                    self.send("Elbereth\r")
                    break
                else:
                    break
            return
        if a.kind in ("eat", "eat_corpse"):
            self.send("e")
            for _ in range(4):
                w = self.term.view()
                if w.yn and "eat" in w.msg:
                    self.send("y" if a.kind == "eat_corpse" else "n")
                    continue
                if w.obj is not None:   # the prompt lists edible letters first
                    self.send(w.obj[0] if a.kind == "eat" and w.obj[:1].isalpha() else "\x1b")
                elif "don't have anything" in w.msg:
                    self.no_food = c["turn"] or 1
                break
            if a.kind == "eat_corpse":
                self.corpse = None
            return
        if a.kind == "search":
            lv.searched[c["hero"]] += 1
        if a.kind == "kick":
            lv.kicks[a.target] += 1
        self.send(a.keys)
        if a.kind in ("explore", "travel"):
            w = self.term.view()
            if w.other:
                self.send("\x1b")
            if w.hero == c["hero"] and a.target:
                lv.failed[a.target] += 1

    def step(self):
        v = self.view()
        self.turn = v.st.get("turn", self.turn)
        reason = self.interrupts(v)
        if reason:
            return reason
        if not v.normal:
            v = self.view()
            if not v.normal:
                self.bad_screens += 1
                if self.bad_screens > 6:
                    self.bad_screens = 0
                    return self.esc("unrecognised screen; inspect it and resume")
                if self.bad_screens > 3:
                    self.send("\x1b")
                else:
                    time.sleep(0.1)
                return None
        self.bad_screens = 0
        if not self.options:
            self.setup()
            return None
        if self.no_food and v.st.get("turn", 0) - self.no_food > 500:
            self.no_food = 0
        c = self.context(v)
        v = self.view()
        if not v.normal:
            return None
        dl, lv, hpf = c["dl"], c["lv"], c["hpf"]
        if self.last_try and self.last_try[0] == dl:
            if cheb(self.last_try[1], c["hero"]) > 1:
                lv.walked(v, self.last_try[1], c["hero"])
                c["dist"] = lv.paths(v, c["hero"])
                c["frontier"] = lv.frontier(v, c["dist"])
            if self.last_try[1] == c["hero"] and self.last_try[3] == c["turn"]:
                self.futile[(dl, c["hero"], self.last_try[2])] += 1   # no time passed and no move: it failed
        if dl != self.prev_dl:
            if self.prev_dl is not None:
                self.note("level", "arrived on Dlvl %d (turn %s)" % (dl, c["turn"]))
            self.prev_dl = dl
        self.max_dl = max(self.max_dl, dl)
        if lv.up_branch and 2 <= dl <= 5 and not self.mines_noted and CFG["mines"] == "escalate":
            self.mines_noted = True
            reason = self.esc("entered the Gnomish Mines (Dlvl %d): many hostile gnomes and dwarves unless the hero "
                              "is a gnome or dwarf; resume with --set mines=allow or mines=avoid" % dl)
            if reason:
                return reason
        mark = (dl, len(lv.near))
        if mark != self.mark:
            self.mark, self.progress = mark, self.decisions
        acts = self.actions(v, c)
        near = [h for h in c["hostiles"] if h["dist"] <= 3]
        reason = None
        if (near or c["hit"]) and hpf < CFG["hp_escalate"] and not any(a.kind == "pray" for a in acts):
            if self.low_noted != (dl, c["hp"] // 3):
                self.low_noted = (dl, c["hp"] // 3)
                reason = "low HP %d/%d with %s and no safe prayer" % (
                    c["hp"], c["hpmax"], (near[0]["name"] + " near") if near else "an unseen attacker")
        elif c["hungry"] in ("Weak", "Fainting") and not any(a.kind in ("eat", "eat_corpse", "pray") for a in acts):
            reason = "%s from hunger, no food, no safe prayer" % c["hungry"]
        elif self.decisions - self.progress > CFG["stall"]:
            self.progress = self.decisions
            reason = "stalled: no new squares or depth in %d decisions" % CFG["stall"]
        if reason and self.esc(reason, hp=c["hp"], hpmax=c["hpmax"]):
            return reason
        reason = None
        self.decisions += 1
        new_level = dl not in self.seen_levels
        self.seen_levels.add(dl)
        facts = self.facts(v, c, new_level)
        try:
            due = self.hooks.due(facts)
        except HookError as e:
            return self.esc(str(e)) or None
        top = acts[0]
        calm = not near and not c["hit"] and hpf >= 0.5 and \
            not set(v.cond) & {"Weak", "Fainting", "Conf", "Stun", "Blind", "Hallu"}
        info = {"src": "rule"}
        chosen = top
        contested = not (top.prior >= 8 or len(acts) == 1 or (calm and top.prior - acts[1].prior >= 1))
        ans = {}
        if (contested or due) and self.decide:
            try:
                ans, took = self.ask(v, c, acts if contested else None, due)
            except Exception as e:   # endpoint trouble: keep playing on the rules
                self.note("model_error", str(e)[:200])
                ans = {}
            if "act" in ans and any(a.key in ans["act"]["probabilities"] for a in acts):
                ranked = sorted(((k, x) for k, x in ans["act"]["probabilities"].items()
                                 if any(a.key == k for a in acts)), key=lambda kv: -kv[1])
                p1 = ranked[0][1]
                danger = ans.get("danger", {}).get("noul", 0.0)
                pick = next(a for a in acts if a.key == ranked[0][0])
                info = {"src": "model", "p": round(p1, 3), "danger": round(danger, 3), "ms": int(took * 1000),
                        "rule": top.key, "agree": pick is top, "top": [(k, round(x, 3)) for k, x in ranked[:4]]}
                if self.directive and p1 >= 0.4:
                    chosen = pick   # with orders in force the model, which reads them, decides contested steps
                if self.decisions < self.calm_until:
                    pass   # just resumed: let the outer loop's fix work for a few decisions
                elif danger > CFG["danger_max"] and (pick is not top or hpf < 0.5):
                    reason = "danger %.2f (rules want %s, model wants %s %.2f)" % (danger, top.key, pick.key, p1)
                elif (danger > 0.5 or hpf < 0.5) and p1 < CFG["p_min"]:
                    reason = "uncertain in a risky spot: %s" % ", ".join("%s %.2f" % kv for kv in ranked[:3])
        if not reason:
            try:
                reason, keys = self.hooks.judge(facts, ans)
            except HookError as e:
                reason, keys = str(e), None
            if reason:
                self.hook_answers = {k: x for k, x in ans.items() if k not in ("act", "danger")}
            elif keys:
                self.note("hook_action", repr(keys)[:80], turn=c["turn"])
                self.last_try = None
                self.send(keys)
                return None
        if reason and self.esc(reason, hp=c["hp"], hpmax=c["hpmax"], **info):
            self.pending = (acts, info) if "top" in info else None
            return reason
        self.note("act", "%s: %s" % (chosen.key, chosen.desc), dlvl=dl, hp="%d/%d" % (c["hp"], c["hpmax"]),
                  turn=c["turn"], **info)
        self.last_try = (dl, c["hero"], chosen.key, c["turn"])
        self.do(v, c, chosen)
        return None

    def summary(self, reason):
        v = self.term.view()
        st, lp = v.st, self.last_prayer
        out = ["ESCALATION: " + reason,
               "Dlvl %s HP %s/%s AC %s XL %s T %s %s | last prayer %s | mode %s | orders: %s" % (
                   st.get("dlvl"), st.get("hp"), st.get("hpmax"), st.get("ac"), st.get("xl"), st.get("turn"),
                   " ".join(v.cond), "never" if lp is None else "T%d (%d ago)" % (lp, (st.get("turn") or 0) - lp),
                   CFG["mode"], self.directive or "-"),
               "inner loop: %d keys, %d decisions, %d model calls (avg %d ms), %d escalations, %.0fs" % (
                   self.keys, self.decisions, self.calls, 1000 * self.mtime / max(1, self.calls), self.escs,
                   time.time() - self.t0)]
        if self.pending:
            acts, info = self.pending
            out.append("model: %s | danger %.2f" % (", ".join("%s %.2f" % kv for kv in info["top"]), info["danger"]))
            out.append("options (rule order): " + " | ".join("%s: %s" % (a.key, a.desc) for a in acts[:8]))
        if reason.startswith("hook:") and self.hook_answers:
            out.append("hook answers: " + "; ".join("%s %s" % (k, json.dumps(x)) for k, x in self.hook_answers.items()))
        for label, group in (("hostiles", self.hostiles), ("never melee", self.obst)):
            if group:
                out.append(label + ": " + "; ".join("%s (%s) %d %s" % (
                    h["name"], h["ch"], h["dist"], compass(v.hero or h["pos"], h["pos"])) for h in group[:6]))
        recent = [h["text"][:80] for h in list(self.hist)[-12:] if h["kind"] in ("act", "msg", "level", "prompt")]
        out.append("recent: " + " / ".join(recent))
        return "\n".join(out + ["--- screen ---", v.text_screen()])


# -------------------------------------------------------------- decide ---

def decider(url, model=None, key=None, timeout=20):
    """POST {state, questions} to a SystemOne-compatible endpoint; retries busy/rate-limit answers."""
    if not url or url == "none":
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key

    def decide(body):
        if model:
            body = dict(body, model=model)
        data = json.dumps(body).encode()
        for attempt in range(8):
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503, 529):
                    raise RuntimeError("decision endpoint HTTP %d: %s" % (e.code, e.read()[:300]))
            except (urllib.error.URLError, OSError) as e:
                if attempt >= 2:
                    raise RuntimeError("decision endpoint unreachable: %s" % e)
            time.sleep(0.25 * (attempt + 1))
        raise RuntimeError("decision endpoint busy")
    return decide


# --------------------------------------------------------------- daemon ---

class Store:
    """Files in the state directory shared by the inner loop and the commands."""

    def __init__(self, path):
        self.dir = os.path.abspath(path)
        os.makedirs(self.dir, exist_ok=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, obj):
        tmp = self.path(name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, self.path(name))

    def read(self, name):
        try:
            with open(self.path(name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def text(self, name, value=None):
        if value is None:
            try:
                with open(self.path(name)) as f:
                    return f.read()
            except OSError:
                return ""
        tmp = self.path(name + ".tmp")
        with open(tmp, "w") as f:
            f.write(value)
        os.replace(tmp, self.path(name))
        return value


def apply_settings(mode, sets):
    if mode:
        CFG["mode"] = mode
        if mode == "careful":
            CFG.update(CAREFUL)
    for k, v in (sets or {}).items():
        if k not in CFG:
            raise ValueError("unknown setting %s (known: %s)" % (k, ", ".join(sorted(CFG))))
        CFG[k] = type(CFG[k])(v)


def save_pilot(store, p):
    kept = (p.term, p.log, p.decide, p.hooks)
    p.term = p.log = p.decide = p.hooks = None
    try:
        with open(store.path("memory.tmp"), "wb") as f:
            pickle.dump((p, dict(CFG)), f)
        os.replace(store.path("memory.tmp"), store.path("memory.pkl"))
    finally:
        p.term, p.log, p.decide, p.hooks = kept


def load_hooks(hooks, questions=(), plugins=(), enable=(), disable=()):
    """Returns a list of problems (empty when everything loaded)."""
    problems = []
    for path in questions or ():
        try:
            hooks.load_questions(path)
        except (OSError, ValueError, HookError) as e:
            problems.append("questions %s: %s" % (path, e))
    for path in plugins or ():
        try:
            hooks.load_plugin(path)
        except Exception as e:
            problems.append("plugin %s: %s: %s" % (path, type(e).__name__, e))
    hooks.disabled |= set(disable or ())
    hooks.disabled -= set(enable or ())
    return problems


def daemon(args):
    store = Store(args.dir)
    cfg = store.read("config.json")
    apply_settings(cfg.get("mode"), cfg.get("set"))
    term = Term(cfg["socket"])
    term.sync()
    decide = decider(cfg.get("decide"), cfg.get("model"), os.environ.get(cfg.get("key_env") or "", None))
    p = None
    if not cfg.get("fresh"):
        try:
            with open(store.path("memory.pkl"), "rb") as f:
                p, saved = pickle.load(f)
            CFG.update(saved)
            apply_settings(cfg.get("mode"), cfg.get("set"))
        except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError):
            p = None
    if p is None:
        p = Pilot(term, decide)
    p.term, p.decide, p.log, p.hooks = term, decide, open(store.path("log.jsonl"), "a"), Hooks()
    p.pending, p.progress, p.calm_until = None, p.decisions, p.decisions + CFG["calm"]
    if cfg.get("directive") is not None:
        p.directive = cfg["directive"]
    p.rng = random.Random(cfg.get("seed") or os.getpid())
    problems = load_hooks(p.hooks, cfg.get("questions"), cfg.get("plugins"), cfg.get("enable"), cfg.get("disable"))
    status = {"state": "running", "pid": os.getpid(), "escalation": store.read("status.json").get("escalation", 0),
              "version": VERSION}
    seq = store.read("control.json").get("seq", 0)
    paused = None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def pause(reason, ended=False):
        nonlocal paused
        paused = reason
        p.escs += 1
        try:
            text = p.summary(reason)
        except Exception as e:   # never lose the escalation itself
            text = "ESCALATION: %s (summary failed: %s)" % (reason, e)
        store.text("escalation.txt", text)
        status.update(state="ended" if ended else "paused", reason=reason, escalation=status["escalation"] + 1)
        p.note("pause", reason)
        save_pilot(store, p)

    store.write("status.json", status)
    if problems:
        pause("hook setup failed: " + "; ".join(problems))
        store.write("status.json", status)
    try:
        while True:
            ctl = store.read("control.json")
            if ctl.get("seq", 0) != seq:
                seq, cmd = ctl["seq"], ctl.get("cmd")
                if cmd == "stop":
                    status.update(state="stopped")
                    store.write("status.json", status)
                    save_pilot(store, p)
                    return 0
                if cmd == "pause" and not paused:
                    pause("paused on request")
                elif cmd == "resume" and status["state"] == "paused":
                    if ctl.get("directive") is not None:
                        p.directive = ctl["directive"]
                    try:
                        apply_settings(ctl.get("mode"), ctl.get("set"))
                    except (ValueError, TypeError) as e:
                        p.note("bad_settings", str(e))
                    if CFG["last_prayer"] >= 0:
                        p.last_prayer, CFG["last_prayer"] = CFG["last_prayer"], -1
                    problems = load_hooks(p.hooks, ctl.get("questions"), ctl.get("plugins"), ctl.get("enable"),
                                          ctl.get("disable"))
                    if problems:
                        pause("hook setup failed: " + "; ".join(problems))
                        store.write("status.json", status)
                        continue
                    try:
                        p.hooks.resumed({"orders": p.directive, "mode": CFG["mode"], "decisions": p.decisions},
                                        {k: ctl.get(k) for k in ("directive", "mode", "set", "enable", "disable")})
                    except HookError as e:
                        p.note("hook_error", str(e))
                    paused, p.pending = None, None
                    p.progress, p.calm_until = p.decisions, p.decisions + CFG["calm"]
                    term.sync()
                    p.note("resume", "orders=%r mode=%s set=%s" % (p.directive, CFG["mode"], ctl.get("set")))
                    status.update(state="running", reason=None)
                elif cmd in ("send", "screen"):
                    reply = ""
                    if cmd == "send" and not paused:
                        reply = "refused: the inner loop is running; pause it first\n"
                    else:
                        try:
                            if cmd == "send":
                                term.send(base64.b64decode(ctl.get("keys", "")))
                                p.note("manual", repr(base64.b64decode(ctl.get("keys", "")))[:80])
                            else:
                                term.poll()
                            reply = term.view().text_screen() + "\n"
                        except Closed as e:
                            reply = "game closed: %s\n" % e
                    store.text("reply.txt", reply)
                    status["reply"] = seq
                store.write("status.json", status)
            if status["state"] == "ended":
                time.sleep(0.25)
                continue
            if paused:
                try:
                    term.poll()   # stay in sync while the outer loop plays by hand
                except Exception:
                    pass
                time.sleep(0.2)
                continue
            try:
                reason = p.step()
            except Closed:
                reason = "game_over"
            except Exception as e:
                reason = "inner loop error: %s: %s" % (type(e).__name__, str(e)[:200])
            last = next((h["text"] for h in reversed(p.hist) if h["kind"] == "act"), "")
            status.update(keys=p.keys, decisions=p.decisions, model_calls=p.calls, max_dlvl=p.max_dl,
                          model_ms=int(1000 * p.mtime / max(1, p.calls)), last=last[:120])
            if reason:
                pause(reason, ended=reason == "game_over")
            store.write("status.json", status)
            if p.decisions % 50 == 0:
                save_pilot(store, p)
    finally:
        save_pilot(store, p)


def alive(status):
    try:
        os.kill(int(status["pid"]), 0)
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


def wait(store, timeout, since):
    end = time.time() + timeout
    while time.time() < end:
        st = store.read("status.json")
        if st.get("escalation", 0) > since and st.get("state") in ("paused", "ended"):
            print(store.text("escalation.txt"))
            if st["state"] == "paused":
                print("\n[paused: the keyboard is yours (send/screen, or your own terminal tool). Continue with: "
                      "resume [--directive TEXT] [--mode descend|explore|careful] [--set k=v]]")
                return 0
            print("\n[game over: %s]" % st.get("reason"))
            return 3
        if st.get("state") == "stopped" or (st and not alive(st)):
            print("inner loop not running (state: %s); see %s" % (st.get("state"), store.path("daemon.log")))
            return 1
        time.sleep(0.2)
    st = store.read("status.json")
    print("still running fine, call wait again: %s keys, %s decisions, %s model calls (%s ms), max Dlvl %s, last: %s"
          % (st.get("keys"), st.get("decisions"), st.get("model_calls"), st.get("model_ms"), st.get("max_dlvl"),
             st.get("last")))
    return 2


def control(store, cmd, **kw):
    kw.update(cmd=cmd, seq=store.read("control.json").get("seq", 0) + 1)
    store.write("control.json", kw)
    return kw["seq"]


# ---------------------------------------------------------- local game ---

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
                        return self.reply(410, b"game over\n")
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


# ------------------------------------------------------------------ cli ---

def parse_sets(items):
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit("--set wants k=v, got %r" % item)
        k, v = item.split("=", 1)
        if k not in CFG:
            raise SystemExit("unknown setting %s (known: %s)" % (k, ", ".join(sorted(CFG))))
        out[k] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nethack_harness.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("NETHACK_HARNESS_DIR", ".nethack-harness"),
                    help="state directory shared by the inner loop and these commands")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="launch the inner loop in the background; block until it needs you")
    s.add_argument("--socket", required=True, help="terminal socket: Unix socket path, unix:///path or http://host:port")
    s.add_argument("--decide", required=True, help="decision endpoint URL (SystemOne-compatible), or 'none' for rules only")
    s.add_argument("--model", help="model name to send (some endpoints require one)")
    s.add_argument("--key-env", help="environment variable holding a bearer key for the decision endpoint")
    s.add_argument("--fresh", action="store_true", help="ignore memory saved by an earlier inner loop")
    s.add_argument("--seed", help="seed for tie-breaking choices (default: process id)")
    for name in ("start", "resume"):
        p = s if name == "start" else sub.add_parser("resume", help="continue after an escalation; blocks like wait")
        p.add_argument("--directive", help="orders for the decision model; with orders set it decides contested steps "
                                           "('' clears)")
        p.add_argument("--mode", choices=["descend", "explore", "careful"])
        p.add_argument("--set", action="append", default=[], metavar="K=V", help="tunable, e.g. danger_max=0.7")
        p.add_argument("--timeout", type=float, default=100, help="seconds to block (default 100)")
        p.add_argument("--questions", action="append", default=[], metavar="FILE",
                       help="JSON hook questions to add (see README)")
        p.add_argument("--plugin", action="append", default=[], metavar="FILE", help="Python hook module to load")
        p.add_argument("--enable", action="append", default=[], metavar="KEY", help="re-enable a hook or plugin")
        p.add_argument("--disable", action="append", default=[], metavar="KEY", help="disable a hook or plugin")
    w = sub.add_parser("wait", help="block until the next escalation")
    w.add_argument("--timeout", type=float, default=100)
    sub.add_parser("pause", help="pause at the next step (prints the situation)")
    sub.add_parser("stop", help="stop the inner loop (memory is kept)")
    sub.add_parser("status", help="state as JSON")
    lg = sub.add_parser("log", help="recent inner-loop events")
    lg.add_argument("n", nargs="?", type=int, default=30)
    sub.add_parser("screen", help="the screen as the inner loop sees it")
    sd = sub.add_parser("send", help="send keys while paused; prints the resulting screen")
    sd.add_argument("keys", nargs="?")
    sd.add_argument("--hex", help="bytes as hex, e.g. 1b for Escape, 0d for Enter")
    pr = sub.add_parser("probe", help="one decision on the current screen; sends no game keys except look-ups")
    pr.add_argument("--socket", required=True)
    pr.add_argument("--decide", required=True)
    pr.add_argument("--model")
    lo = sub.add_parser("serve-local", help="run nethack in a pty behind a terminal socket, for testing")
    lo.add_argument("--socket", required=True)
    lo.add_argument("--nethack", default="nethack")
    lo.add_argument("--playground", help="passed to nethack as -d DIR")
    lo.add_argument("--options", help="NETHACKOPTIONS value, e.g. 'seed:42,color,!autopickup'")
    lo.add_argument("args", nargs="*", help="extra nethack arguments (after --)")
    sub.add_parser("_daemon")
    a = ap.parse_args(argv)
    store = Store(a.dir)

    if a.cmd == "_daemon":
        with open(store.path("daemon.log"), "a") as log:
            os.dup2(log.fileno(), 2)
        return daemon(a)
    if a.cmd == "serve-local":
        return serve_local(a)
    if a.cmd == "start":
        st = store.read("status.json")
        if st.get("state") in ("running", "paused") and alive(st):
            print("already running (%s); use wait, resume or stop" % st["state"])
            return 1
        cfg = {"socket": a.socket, "decide": a.decide, "model": a.model, "key_env": a.key_env, "fresh": a.fresh,
               "seed": a.seed, "directive": a.directive, "mode": a.mode, "set": parse_sets(a.set),
               "questions": [os.path.abspath(x) for x in a.questions], "plugins": [os.path.abspath(x) for x in a.plugin],
               "enable": a.enable, "disable": a.disable}
        store.write("config.json", cfg)
        for name in ("status.json", "control.json"):
            if os.path.exists(store.path(name)):
                os.unlink(store.path(name))
        cmd = [sys.executable, os.path.abspath(__file__), "--dir", store.dir, "_daemon"]
        with open(os.devnull, "rb") as null, open(store.path("daemon.log"), "a") as log:
            subprocess.Popen(cmd, stdin=null, stdout=log, stderr=log, start_new_session=True, close_fds=True)
        for _ in range(150):
            if store.read("status.json"):
                break
            time.sleep(0.1)
        else:
            print("the inner loop did not start; last lines of %s:" % store.path("daemon.log"))
            print("".join(open(store.path("daemon.log")).readlines()[-8:]))
            return 1
        return wait(store, a.timeout, 0)
    st = store.read("status.json")
    if a.cmd == "wait":
        return wait(store, a.timeout, st.get("escalation", 0) - (1 if st.get("state") in ("paused", "ended") else 0))
    if a.cmd == "status":
        print(json.dumps(dict(st, alive=alive(st))))
        return 0
    if a.cmd == "log":
        try:
            with open(store.path("log.jsonl")) as f:
                lines = f.readlines()[-a.n:]
        except OSError:
            return 1
        for line in lines:
            d = json.loads(line)
            print(d.get("step"), d.get("kind"), d.get("text", "")[:120], d.get("p", ""))
        return 0
    if a.cmd == "probe":
        term = Term(a.socket)
        term.sync()
        pilot = Pilot(term, decider(a.decide, a.model, os.environ.get("DECIDE_API_KEY")))
        pilot.options = True
        v = pilot.view()
        if not v.normal:
            print("not at a normal command prompt: %s" % v.msg)
            return 1
        c = pilot.context(v)
        acts = pilot.actions(term.view(), c)
        ans, took = pilot.ask(term.view(), c, acts)
        print(json.dumps({"options": {x.key: x.desc for x in acts}, "rule": acts[0].key, "answers": ans,
                          "ms": int(took * 1000)}, indent=1))
        return 0
    if not alive(st):
        print("inner loop not running; use start")
        return 1
    if a.cmd in ("pause", "stop"):
        control(store, a.cmd)
        if a.cmd == "pause":
            return wait(store, 15, st.get("escalation", 0))
        print("stop requested")
        return 0
    if a.cmd == "resume":
        if st.get("state") != "paused":
            print("not paused (state: %s); use wait" % st.get("state"))
            return 1
        control(store, "resume", directive=a.directive, mode=a.mode, set=parse_sets(a.set),
                questions=[os.path.abspath(x) for x in a.questions], plugins=[os.path.abspath(x) for x in a.plugin],
                enable=a.enable, disable=a.disable)
        return wait(store, a.timeout, st.get("escalation", 0))
    if a.cmd in ("send", "screen"):
        keys = b""
        if a.cmd == "send":
            if (a.keys is None) == (a.hex is None):
                print("send wants keys or --hex")
                return 2
            keys = a.keys.encode() if a.keys is not None else bytes.fromhex(a.hex)
        seq = control(store, a.cmd, keys=base64.b64encode(keys).decode())
        for _ in range(150):
            if store.read("status.json").get("reply") == seq:
                print(store.text("reply.txt"), end="")
                return 0
            time.sleep(0.1)
        print("no reply from the inner loop")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
