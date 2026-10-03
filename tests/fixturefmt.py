"""Plain-text screen fixtures: the screen, its colours, pets, cursor, farlook answers and expected actions.

    cursor R C
    lookup R,C name            (0-based; one line per farlook answer the loop needs)
    expect key key key         (the rule ranking's top actions for a fresh pilot on this screen)
    ==== chars                 24 lines
    ==== colour                one char per cell: . default, k black, r red, g green, y brown, b blue,
                               m magenta, c cyan, w white; UPPERCASE = bold or bright; * = bold default
    ==== reverse               # = reverse video (pets); omitted when empty
"""
import random

CODE = {"default": ".", "black": "k", "red": "r", "green": "g", "brown": "y", "blue": "b", "magenta": "m",
        "cyan": "c", "white": "w", "gray": "."}
NAME = {v: k for k, v in CODE.items() if k != "gray"}


def dump(view, lookups=(), expect=()):
    out = ["cursor %d %d" % view.cursor]
    out += ["lookup %d,%d %s" % (p[0], p[1], name) for p, name in sorted(lookups)]
    if expect:
        out.append("expect " + " ".join(expect))
    out += ["==== chars"] + [r.rstrip() for r in view.rows]
    col = []
    for y in range(24):
        line = ""
        for x in range(80):
            f = view.fgs[y][x] or "default"
            bright = f.startswith("bright")
            ch = CODE.get(f[6:] if bright else f, ".")
            bold = view.bolds[y][x] or bright
            line += ch.upper() if bold and ch != "." else ("*" if bold else ch)
        col.append(line.rstrip("."))
    out += ["==== colour"] + col
    if any(any(r) for r in view.revs):
        out += ["==== reverse"] + ["".join("#" if c else " " for c in r).rstrip() for r in view.revs]
    return "\n".join(out) + "\n"


def load(text):
    """Returns (view arguments, lookups dict, expected keys)."""
    lines = text.split("\n")
    head, sec, data = {"lookup": {}, "expect": []}, None, {"chars": [], "colour": [], "reverse": []}
    cursor = (0, 0)
    for line in lines:
        if line.startswith("==== "):
            sec = line[5:]
            continue
        if sec:
            data[sec].append(line)
        elif line.startswith("cursor "):
            cursor = tuple(int(x) for x in line.split()[1:3])
        elif line.startswith("lookup "):
            pos, name = line[7:].split(" ", 1)
            head["lookup"][tuple(int(x) for x in pos.split(","))] = name
        elif line.startswith("expect "):
            head["expect"] = line.split()[1:]
    rows = [(data["chars"] + [""] * 24)[i].ljust(80)[:80] for i in range(24)]
    fg = [["default"] * 80 for _ in range(24)]
    bold = [[False] * 80 for _ in range(24)]
    for i in range(24):
        for j, ch in enumerate((data["colour"] + [""] * 24)[i].ljust(80, ".")[:80]):
            if ch == "*":
                bold[i][j] = True
            elif ch != ".":
                fg[i][j], bold[i][j] = NAME[ch.lower()], ch.isupper()
    rev = [[c == "#" for c in (data["reverse"] + [""] * 24)[i].ljust(80)[:80]] for i in range(24)]
    return (rows, fg, bold, rev, cursor), head["lookup"], head["expect"]


class ScreenTerm:
    """A terminal that always shows one screen and records what is sent."""

    def __init__(self, view_cls, args):
        self.args, self.view_cls, self.sent, self.seen = args, view_cls, [], ""

    def view(self):
        return self.view_cls(*self.args)

    def send(self, keys):
        self.sent.append(keys)

    def settle(self, *a):
        pass

    def poll(self, *a):
        return False


def fresh_ranking(nh, args, lookups, top=3):
    """The rule ranking a fresh pilot gives this screen, without touching a game."""
    term = ScreenTerm(nh.View, args)
    p = nh.Pilot(term, None)
    p.options = p.briefed = True
    p.inv_turn = 10 ** 9
    p.rng = random.Random(0)
    p.lookup = lambda pos: lookups.get(pos)
    v = term.view()
    if not v.normal:
        return ["not-normal"], term.sent
    p.branch_dl = v.st.get("dlvl")
    p.view()
    c = p.context(v)
    acts = p.actions(v, c)
    return [a.key for a in acts[:top]], term.sent
