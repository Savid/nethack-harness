"""Parsing one 80x24 tty screen: messages, prompts, menus, status lines, hero, map cells."""
import re

from .knowledge import CONDITION_RE, colour


class PromptContext:
    """Keep a targeting cursor distinct from the hero after automatic descriptions."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.position = False
        self.status = None

    def observe(self, view):
        status = (view.st.get("location"), view.st.get("turn"))
        changed = self.position and any(before is not None and after is not None and before != after
                                        for before, after in zip(self.status, status))
        if view.ended or changed:
            self.reset()
        if view.getpos and not view.more and not view.menu:
            self.position = True
        if self.position and not view.ended:
            # Full-screen help and target menus can cover the status lines.
            self.status = tuple(after if after is not None else before
                                for before, after in zip(self.status or status, status))
            view.getpos = view.prompt = True
            view.hero = None
            view.normal = view.engulfed = False
        return view

    def before_input(self, view, keys):
        # Help pages and target menus return to getpos; their Escape belongs to
        # the nested window, not the targeting operation.
        if self.position and not view.more and not view.menu:
            if any(key in ".,;:\x1b" for key in keys):
                self.reset()


class View:
    def __init__(self, rows, fg, bold, rev, cursor):
        self.rows = [r.ljust(80)[:80] for r in rows]
        self.fgs, self.bolds, self.revs = fg, bold, rev
        top = rows[0].rstrip()
        wrapped = len(top) >= 79 and rows[1].strip() and not re.search(r"[|\-#.]{3}", rows[1])
        if wrapped:
            top += " " + rows[1].strip()   # a message or prompt that wrapped onto row 1
        msg = self.msg = top.strip()
        self.more = any("--More--" in r for r in rows)
        self.menu = any(re.search(r"\((end|\d+ of \d+)\)\s*$", r.rstrip()) for r in rows)
        # A question is live only while the cursor waits on the message line; once answered, its text can stay
        # on screen but must not be answered again.
        asking = self.asking = (cursor[0] == 0 or wrapped and cursor[0] == 1) and not self.more
        m = re.search(r"\[([a-zA-Z#\-]+)\](?: \(.\))?\s*$", msg)
        self.yn = m.group(1) if m and not self.more and asking else None
        m = re.search(r"\[([^\]]*?)(?: or \?\*)?\]\s*$", msg)
        self.obj = m.group(1) if m and "What do you want" in msg and asking else None
        self.spell = m.group(1) if m and "Cast which spell?" in msg and asking else None
        if self.obj is not None:
            self.yn = None
        self.text = self.obj is None and asking and bool(re.search(
            r"What do you want to (write|name|call|add)|Call an? |Name it|What monster|write in the|"
            r"What do you want to engrave|who are you\?|What do you want to (?:wish|genocide)", msg))
        self.direction = asking and bool(re.search(r"(?:in what|which) direction|Open where\?", msg, re.I))
        self.getpos = bool(re.search(r"Where do you want to (?:travel|cast the spell)|"
                                    r"For instructions type a ['\"]?\?|Pick an? |Move cursor to ", msg))
        joined = "\n".join(rows)
        # A death message can precede life-saving; disclosure and farewells are irreversible.
        farewell = r"(?:^|\n)\s*(?:Goodbye|Fare thee well|Sayonara|Aloha|Farvel) "
        self.result = "ascended" if (re.search(r"^You ascend to the status of Demigod(?:dess)?\.\.\.", msg) or
                                     re.search(farewell + r".+ the Demigod(?:dess)?\.\.\.", joined)) else None
        self.ended = bool(self.result or re.search(r"possessions identified\?|Do you want to see what you had",
                                   msg) or re.search(r"(?:^|\n)\s*(?:Goodbye|Fare thee well|Sayonara|Aloha|Farvel) "
                                                    r".+ the .+\.\.\.|REST\s+IN\s+PEACE", joined))
        status = rows[22] + " " + rows[23]
        location = re.search(r"\b(Dlvl:\d+|Home \d+|Tutorial:\d+|Fort Ludios|Astral Plane|"
                             r"Earth|Air|Fire|Water|Plane of \w+|End Game)\b", rows[23])
        self.st = {"location": location.group(1) if location else None}
        for key, rx in (("dlvl", r"Dlvl:(\d+)"), ("gold", r"\$:(\d+)"), ("hp", r"HP:(-?\d+)\((\d+)\)"),
                        ("pw", r"Pw:(\d+)\((\d+)\)"), ("ac", r"AC:(-?\d+)"), ("xl", r"(?:Xp|XL|Exp):(\d+)"),
                        ("turn", r"T:(\d+)"), ("dex", r"Dx:(\d+)"), ("con", r"Co:(\d+)"),
                        ("int", r"In:(\d+)"), ("wis", r"Wi:(\d+)"), ("cha", r"Ch:(\d+)")):
            m = re.search(rx, status)
            if m:
                self.st[key] = int(m.group(1))
                if key in ("hp", "pw"):
                    self.st[key + "max"] = int(m.group(2))
        strength = re.search(r"St:(\d+(?:/(?:\d+|\*\*))?)", status)
        if strength:
            self.st["strength"] = strength.group(1)
        alignment = re.search(r"\b(Lawful|Neutral|Chaotic|Unaligned)\b", rows[22])
        if alignment:
            self.st["alignment"] = alignment.group(1)
        self.cond = [name for name, rx in CONDITION_RE.items() if rx.search(rows[22][40:] + " " + rows[23])]
        self.title = rows[22].split("St:")[0].strip()
        self.prompt = bool(self.more or self.menu or self.yn or self.obj is not None or self.spell is not None or self.text or
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
        self.normal = bool(self.hero) and not self.prompt and not self.ended

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
