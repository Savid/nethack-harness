"""Observed terrain and unweighted paths through known squares."""
from collections import deque

from .knowledge import DIRS, ITEMS, MON, WARNING


FEATURES = {"<": "up stairs", ">": "down stairs", "^": "trap", "_": "altar",
            "{": "fountain", "}": "water or lava", "\\": "throne"}


def on_map(p):
    return 1 <= p[0] <= 21 and 0 <= p[1] < 80


def position(p):
    return [p[0] + 1, p[1] + 1] if p is not None else None


def neighbours(p):
    for key, (dr, dc) in DIRS.items():
        q = p[0] + dr, p[1] + dc
        if on_map(q):
            yield key, q


def cursor_keys(start, target):
    dr, dc = target[0] - start[0], target[1] - start[1]
    return ";@" + ("l" * dc if dc > 0 else "h" * -dc) + ("j" * dr if dr > 0 else "k" * -dr) + "."


class Level:
    def __init__(self, name, label):
        self.id, self.label = name, label
        self.terrain = {}
        self.colours = {}
        self.inferred_floor = set()
        self.open_doors = set()
        self.visits = {}
        self.searches = {}
        self.feature_turns = {}
        self.inspections = {}
        self.landmark_messages = {}

    def remember_terrain(self, p, ch, turn, colour=None):
        previous = self.terrain.get(p)
        self.terrain[p] = ch
        if colour is not None:
            self.colours[p] = colour
        elif previous != ch:
            self.colours.pop(p, None)
        self.inferred_floor.discard(p)
        if ch in FEATURES:
            self.feature_turns[p] = turn
        else:
            self.feature_turns.pop(p, None)

    def observe(self, view):
        for r in range(1, 22):
            for c in range(80):
                p, ch = (r, c), view.ch(r, c)
                if p == view.hero or ch in MON or ch in WARNING or ch == "I":
                    continue
                if ch != " " and ch not in ITEMS:
                    self.remember_terrain(p, ch, view.st.get("turn"), view.col(r, c)[0])
                    if ch in "|-" and view.fg(r, c) in ("brown", "yellow"):
                        self.open_doors.add(p)
                    else:
                        self.open_doors.discard(p)
        if view.hero:
            if view.hero not in self.terrain:
                self.terrain[view.hero] = "."
                self.inferred_floor.add(view.hero)

    def corridor(self, p):
        return self.terrain.get(p) == "#" and self.colours.get(p) not in ("green", "cyan")

    def structural_obstacles(self):
        return [{"position": position(p), "glyph": "#", "colour": self.colours[p],
                 "source": "remembered terminal terrain"}
                for p, ch in sorted(self.terrain.items()) if ch == "#" and not self.corridor(p)]

    def paths(self, start, occupied=()):
        previous, queue = {start: None}, deque([start])
        blocked = set(occupied) - {start}
        while queue:
            p = queue.popleft()
            for key, q in neighbours(p):
                if q in previous or q in blocked or (self.terrain.get(q, " ") not in ".#<>^{}_\\}" and q not in self.open_doors):
                    continue
                if self.terrain.get(q) == "#" and not self.corridor(q):
                    continue
                if key in "yubn" and (p in self.open_doors or q in self.open_doors):
                    continue
                previous[q] = p
                queue.append(q)
        return previous

    @staticmethod
    def route(previous, target):
        if target not in previous:
            return None
        path = []
        while previous[target] is not None:
            path.append(target)
            target = previous[target]
        return path[::-1]

    def targets(self, previous):
        out = {}
        for p, ch in self.terrain.items():
            if p not in previous:
                continue
            if ch in "<>^{}_\\}":
                out[p] = {"<": "up stairs", ">": "down stairs", "^": "trap", "_": "altar",
                          "{": "fountain", "}": "water or lava", "\\": "throne"}.get(ch, ch)
            elif self.corridor(p):
                exits = [q for key, q in neighbours(p) if key in "hjkl" and
                         (self.terrain.get(q, " ") in ".#<>^{}_\\}+" or q in self.open_doors) and
                         (self.terrain.get(q) != "#" or self.corridor(q))]
                if len(exits) <= 1:
                    out[p] = "corridor endpoint"
                elif len(exits) > 2:
                    out[p] = "corridor junction"
            elif any(q not in self.terrain for _, q in neighbours(p)):
                out[p] = "edge of known terrain"
        for door, ch in sorted(self.terrain.items()):
            if ch != "+":
                continue
            for key, p in neighbours(door):
                if key in "hjkl" and p in previous:
                    label = "square beside closed door at %s" % position(door)
                    out[p] = out[p] + "; " + label if p in out else label
        return out

    def rows(self):
        return ["".join(self.terrain.get((r, c), " ") for c in range(80)).rstrip() for r in range(1, 22)]

    def blocked_targets(self, start, occupied):
        terrain_paths = self.paths(start)
        available = self.paths(start, occupied)
        blocked = []
        for target, description in sorted(self.targets(terrain_paths).items()):
            if target in available:
                continue
            route = self.route(terrain_paths, target)
            blocked.append({"position": position(target), "description": description,
                            "terrain_path_length": len(route),
                            "occupied_on_terrain_path": [position(p) for p in route if p in occupied]})
        return blocked

    def landmarks(self):
        return [{"position": position(p), "kind": FEATURES[ch], "glyph": ch,
                 "observed_turn": self.feature_turns.get(p), "source": "terminal map or underfoot message"}
                for p, ch in sorted(self.terrain.items()) if ch in FEATURES] + [
                    dict(value, position=position(p), kind=kind)
                    for (p, kind), value in sorted(self.landmark_messages.items())]
