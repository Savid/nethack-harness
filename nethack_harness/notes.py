"""Level notes: what this game has learned about each level, as a file another copy of the same game can use.

    {"v": 1, "anchor": "...", "levels": {"main:5": {"dlvl": 5, "branch": "main", "down": [[12, 45]],
     "branch_down": [], "up": [5, 10], "holes": [[8, 30, "trap door"]], "shop": false, "mines": false,
     "turns": 312, "hazards": ["floating eye"]}}}

Positions are 1-based ROW,COL (as goal:travel takes them). The anchor fingerprints the first screen of the game,
so notes from a different game are ignored.
"""
import hashlib
import json
import os


def anchor_of(v):
    """A fingerprint of the game's first screen (Dlvl 1 at turn 1): copies of one game share it."""
    return hashlib.sha1("\n".join(v.rows[1:22]).encode()).hexdigest()[:16]


def key_of(k):
    return "%s:%d" % (k[0], k[1]) if isinstance(k, tuple) else "main:%d" % k


def to1(q):
    return [q[0] + 1, q[1] + 1]


def export(p):
    levels = {}
    for k, lv in p.lv.items():
        branch, dl = (k[0], k[1]) if isinstance(k, tuple) else ("main", k)
        levels[key_of(k)] = {
            "dlvl": dl, "branch": branch, "mines": lv.mines, "shop": lv.has_shop,
            "down": [to1(q) for q, kind in sorted(lv.downs.items()) if kind == "main"],
            "branch_down": [to1(q) for q, kind in sorted(lv.downs.items()) if kind == "branch"],
            "up": to1(lv.up) if lv.up else None,
            "holes": [to1(q) + [t] for q, t in sorted(lv.traps.items()) if t in ("trap door", "hole")],
            "turns": lv.turns, "hazards": sorted(lv.hazards)}
    return {"v": 1, "anchor": p.anchor, "levels": levels}


def write(path, p):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(export(p), f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def merge(p, data):
    """Fold another copy's notes into this game's levels. Returns the merged level keys, or an error string."""
    if not isinstance(data, dict) or data.get("v") != 1:
        return "notes file is not version 1"
    if not p.anchor or data.get("anchor") != p.anchor:
        return "notes are from another game (anchor differs)"
    merged = []
    for key, note in sorted((data.get("levels") or {}).items()):
        branch, dl = note.get("branch", "main"), int(note.get("dlvl", 0))
        lv = p.level(dl, branch)
        to0 = lambda q: (int(q[0]) - 1, int(q[1]) - 1)  # noqa: E731
        lv.imported = {"down": [to0(q) for q in note.get("down") or []],
                       "branch_down": [to0(q) for q in note.get("branch_down") or []],
                       "up": to0(note["up"]) if note.get("up") else None,
                       "holes": [to0(q[:2]) + (q[2],) for q in note.get("holes") or []]}
        for q in lv.imported["branch_down"]:
            lv.downs[q] = "branch"          # a staircase the other copy found leads into the Mines
        for q, t in [(tuple(h[:2]), h[2]) for h in lv.imported["holes"]]:
            lv.traps.setdefault(q, t)
        if lv.imported["up"] and not lv.up:
            lv.up = lv.imported["up"]
        lv.hazards |= set(note.get("hazards") or [])
        merged.append("%s Dlvl %d" % (branch, dl))
    return merged


def lines(p):
    """One line per level: what is known, and where it came from."""
    out = []
    for k in sorted(p.lv, key=lambda k: (isinstance(k, tuple), k if not isinstance(k, tuple) else k[1])):
        lv = p.lv[k]
        branch, dl = (k[0], k[1]) if isinstance(k, tuple) else ("main", k)
        parts = ["%s %d,%d [seen]" % ("down" if kind == "main" else "Mines down", q[0] + 1, q[1] + 1)
                 for q, kind in sorted(lv.downs.items())]
        parts += ["down %d,%d [imported]" % (q[0] + 1, q[1] + 1) for q in lv.imported.get("down", [])
                  if q not in lv.downs]
        if lv.up:
            parts.append("up %d,%d" % (lv.up[0] + 1, lv.up[1] + 1))
        parts += ["%s %d,%d" % (t, q[0] + 1, q[1] + 1) for q, t in sorted(lv.traps.items()) if t in ("trap door", "hole")]
        if lv.hazards:
            parts.append("hazards: " + ", ".join(sorted(lv.hazards)))
        out.append("%s Dlvl %d: %s" % ("Mines" if branch == "mines" else "", dl, "; ".join(parts) or "-"))
    return "\n".join(x.strip() for x in out) + "\n"
