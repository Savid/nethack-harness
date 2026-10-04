"""Parsing one 80x24 tty screen: messages, prompts, menus, status lines, hero, map cells."""
import re

from .knowledge import CONDITION_RE, colour


class View:
    def __init__(self, rows, fg, bold, rev, cursor):
        self.rows = [r.ljust(80)[:80] for r in rows]
        self.fgs, self.bolds, self.revs = fg, bold, rev
        top = rows[0].rstrip()
        if len(top) >= 79 and rows[1].strip() and not re.search(r"[|\-#.]{3}", rows[1]):
            top += " " + rows[1].strip()   # a message or prompt that wrapped onto row 1
        msg = self.msg = top.strip()
        self.more = any("--More--" in r for r in rows)
        self.menu = any(re.search(r"\((end|\d+ of \d+)\)\s*$", r.rstrip()) for r in rows[:23])
        # A question is live only while the cursor waits on the message line; once answered, its text can stay
        # on screen but must not be answered again.
        asking = self.asking = cursor[0] == 0 and not self.more
        m = re.search(r"\[([a-zA-Z\-]+)\](?: \(.\))?\s*$", msg)
        self.yn = m.group(1) if m and not self.more and asking else None
        m = re.search(r"\[([^\]]*?)(?: or \?\*)?\]\s*$", msg)
        self.obj = m.group(1) if m and "What do you want" in msg and asking else None
        if self.obj is not None:
            self.yn = None
        self.text = self.obj is None and asking and bool(re.search(
            r"What do you want to (write|name|call|add)|Call an? |Name it|What monster|write in the|"
            r"What do you want to engrave|who are you\?|What do you want to (?:wish|genocide)", msg))
        self.direction = asking and bool(re.search(r"(In what|Which) direction\?", msg))
        self.getpos = bool(re.search(r"Where do you want to travel|type a \?|Pick an|Pick a ", msg))
        joined = "\n".join(rows)
        # Only the death flow counts: engravings and epitaphs quote "killed by" and "You die" too.
        self.dead = bool(re.search(r"^You die\.\.\.|possessions identified\?|Do you want to see what you had",
                                   msg) or re.search(r"Goodbye \S+ the |REST\s+IN\s+PEACE", joined))
        status = rows[22] + " " + rows[23]
        self.st = {}
        for key, rx in (("dlvl", r"Dlvl:(\d+)"), ("gold", r"\$:(\d+)"), ("hp", r"HP:(-?\d+)\((\d+)\)"),
                        ("pw", r"Pw:(\d+)\((\d+)\)"), ("ac", r"AC:(-?\d+)"), ("xl", r"(?:Xp|XL|Exp):(\d+)"),
                        ("turn", r"T:(\d+)"), ("con", r"Co:(\d+)"), ("int", r"In:(\d+)")):
            m = re.search(rx, status)
            if m:
                self.st[key] = int(m.group(1))
                if key in ("hp", "pw"):
                    self.st[key + "max"] = int(m.group(2))
        self.cond = [name for name, rx in CONDITION_RE.items() if rx.search(rows[22][40:] + " " + rows[23])]
        self.title = rows[22].split("St:")[0].strip()
        self.prompt = bool(self.more or self.menu or self.yn or self.obj is not None or self.text or
                           self.direction or self.getpos)
        y, x = cursor
        self.cursor = cursor
        self.hero = None
        if 1 <= y <= 21 and not self.prompt and self.rows[y][x:x + 1].strip():
            self.hero = (y, x)       # tty leaves the cursor on the hero, whatever its symbol
        elif not self.prompt and not (asking and msg):   # a cursor on the message line means a question is open
            ats = [(r, c) for r in range(1, 22) for c in range(80) if self.rows[r][c] == "@"]
            self.hero = min(ats, key=lambda p: abs(p[0] - y) + abs(p[1] - x)) if ats else None
        self.engulfed = bool(self.hero) and self._engulfed()
        self.normal = bool(self.hero) and not self.prompt and not self.dead

    def _engulfed(self):
        r, c = self.hero
        ring = [self.ch(r - 1, c - 1), self.ch(r - 1, c + 1), self.ch(r + 1, c - 1), self.ch(r + 1, c + 1)]
        return ring == ["/", "\\", "\\", "/"]

    def ch(self, r, c):
        return self.rows[r][c] if 1 <= r <= 21 and 0 <= c < 80 else " "

    def fg(self, r, c):
        return self.fgs[r][c] if 1 <= r <= 21 and 0 <= c < 80 else "default"

    def col(self, r, c):
        """(base colour, bright) of a map cell."""
        if not (1 <= r <= 21 and 0 <= c < 80):
            return "gray", False
        return colour(self.fgs[r][c], self.bolds[r][c])

    def pet(self, r, c):
        return 1 <= r <= 21 and 0 <= c < 80 and self.revs[r][c]

    def text_screen(self):
        return "\n".join(r.rstrip() for r in self.rows)
