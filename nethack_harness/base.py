"""Command encoding and shared execution errors."""
import re


class Paused(Exception):
    """An execution or decision boundary requires the caller's attention."""


def unescape(keys):
    table = {"r": "\r", "n": "\n", "t": "\t", "e": "\x1b", "\\": "\\"}
    return re.sub(r"\\(x[0-9a-fA-F]{2}|[rnte\\])",
                  lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "x" else table[m.group(1)], keys)
