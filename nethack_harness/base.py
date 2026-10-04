"""Shared small types: actions, hard escalations, sentinels, key escaping."""
import re


GAME_OVER = "game_over"
RACE_MONSTER = {"dwarvish": "dwarf", "gnomish": "gnome", "elven": "elf", "orcish": "orc", "human": "human"}


class Act:
    def __init__(self, key, desc, keys, kind, prior=0.0, target=None):
        self.key, self.desc, self.keys, self.kind, self.prior, self.target = key, desc, keys, kind, prior, target
        self.door = None           # goto_door: the locked door this trip is for

    def __repr__(self):
        return "Act(%s %.1f)" % (self.key, self.prior)


class Hard(Exception):
    """An escalation that budgets and calm windows never suppress."""


def unescape(keys):
    """Backslash escapes in typed keys: \\r Enter, \\n, \\t, \\e Escape, \\\\ backslash, \\xHH a byte."""
    table = {"r": "\r", "n": "\n", "t": "\t", "e": "\x1b", "\\": "\\"}
    return re.sub(r"\\(x[0-9a-fA-F]{2}|[rnte\\])",
                  lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "x" else table[m.group(1)], keys)


def escape(keys):
    """The inverse of unescape: printable keys stay, the rest become escapes (one line, safe to replay)."""
    out = []
    for ch in keys:
        if ch == "\\":
            out.append("\\\\")
        elif ch in "\r\n\t\x1b":
            out.append({"\r": "\\r", "\n": "\\n", "\t": "\\t", "\x1b": "\\e"}[ch])
        elif " " <= ch <= "~":
            out.append(ch)
        else:
            out.append("\\x%02x" % ord(ch))
    return "".join(out)
