"""Per-level memory and geometry: terrain, paths, frontiers, search spots, bans."""
import collections
import heapq

from .knowledge import BOULDERS, DIRS, FLOOR, MON, WARNING


def door(ch, fg):
    return ch in "+|-" and fg in ("brown", "yellow")


def passable(ch, fg, doors=False):
    if ch in FLOOR:
        return not (ch == "#" and fg in ("green", "cyan"))   # trees, iron bars
    return (door(ch, fg) and (ch != "+" or doors)) or ch in MON or ch == "I" or ch in WARNING


def pos1(p):
    """A screen position as the 1-based ROW,COL that goal:travel takes."""
    return "%d,%d" % (p[0] + 1, p[1] + 1)


def cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def compass(a, b):
    dr, dc = b[0] - a[0], b[1] - a[1]
    return ("north" if dr < 0 else "south" if dr > 0 else "") + ("west" if dc < 0 else "east" if dc > 0 else "")


def nbrs(p):
    for k, (dr, dc) in DIRS.items():
        yield k, (p[0] + dr, p[1] + dc)


def on_map(p):
    return 1 <= p[0] <= 21 and 0 <= p[1] < 80


def travel(a, b, prefix="_"):
    """Keys that move the travel/farlook cursor from the hero ('@') to b and confirm it."""
    dr, dc = b[0] - a[0], b[1] - a[1]
    keys = prefix + "@"
    for n, big, small in ((dc, "L", "l"), (-dc, "H", "h"), (dr, "J", "j"), (-dr, "K", "k")):
        if n > 0:
            keys += big * (n // 8) + small * (n % 8)
    return keys + "."


class Level:
    def __init__(self, dlvl=0):
        self.dlvl = dlvl
        self.terr, self.tfg, self.near, self.downs = {}, {}, set(), {}
        self.searched = collections.Counter()      # search turns credited per square (3x3 around the hero)
        self.search_turns = self.search_actions = 0
        self.failed, self.kicks, self.cost = collections.Counter(), collections.Counter(), collections.Counter()
        self.locked, self.blocked, self.statues, self.door_frontier = set(), set(), set(), set()
        self.excluded = {}                         # target -> decision index the exclusion expires
        self.bans = {}                             # (pos, action key) -> decision index it expires (-1: level)
        self.noops = collections.Counter()
        self.traps = {}                            # pos -> farlook description
        self.up, self.up_branch, self.shop, self.mines = None, False, False, False
        self.has_shop = False                      # a shop is somewhere on this level (sounds or a greeting)
        self.no_kick = self.no_dig = False
        self.extra_budget = 0
        self.stair_ban_until = 0                   # decision index until which this level's stairs are not taken                      # search turns granted after an "exhausted" report
        self.shop_doors = set()                    # doors (and doorways) of shops: never kicked
        self.probed = set()                        # escape-ladder probes already tried here
        self.arrived = None                        # wall-clock time of arrival
        self.now = 0                               # the pilot's decision counter, for expiring exclusions

    def observe(self, v):
        for r in range(1, 22):
            for c in range(80):
                ch = v.rows[r][c]
                if ch == " " or ch in MON or ch in "I" or ch in WARNING or (r, c) == v.hero:
                    continue
                self.terr[(r, c)], self.tfg[(r, c)] = ch, v.fgs[r][c]
                base, bright = v.col(r, c)
                if ch == ">":
                    if self.downs.get((r, c)) != "branch":      # an unused branch staircase looks like a main one
                        self.downs[(r, c)] = "branch" if base == "brown" and bright else "main"
                elif ch == "<":
                    self.up, self.up_branch = (r, c), base == "brown" and bright
                elif ch == "^" and (r, c) not in self.traps:
                    self.traps[(r, c)] = None
        if v.hero:
            self.see(v.hero)

    def see(self, p):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                self.near.add((p[0] + dr, p[1] + dc))

    def credit_search(self, p, turns):
        self.search_turns += turns
        self.search_actions += 1
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                self.searched[(p[0] + dr, p[1] + dc)] += turns

    def banned(self, pos, key, now):
        until = self.bans.get((pos, key))
        return until is not None and (until < 0 or now < until)

    def ban(self, pos, key, until=-1):
        self.bans[(pos, key)] = until

    def cell(self, v, p):
        ch = v.ch(*p)
        if ch == " " or ch in MON or ch == "I" or ch in WARNING or p == v.hero:
            if p in self.terr:
                return self.terr[p], self.tfg[p]
            if ch != " ":
                return ".", "default"
        return ch, v.fg(*p)

    def route(self, v, start, goal):
        """The squares of a shortest known path from start to goal (excluding start), or None."""
        prev = {}
        self.paths(v, start, prev)
        if goal != start and goal not in prev:
            return None
        out, p = [], goal
        while p != start:
            out.append(p)
            p = prev[p]
        return out[::-1]

    def paths(self, v, start, prev=None):
        """Shortest known-square distances; doors block diagonal steps; traps, boulders, doors cost extra.
        Closed doors are passable at a cost (the hero opens them); locked ones only once broken."""
        dist, heap = {start: 0}, [(0, start)]
        while heap:
            d, p = heapq.heappop(heap)
            if d > dist[p]:
                continue
            c0 = self.cell(v, p)
            for k, q in nbrs(p):
                if not on_map(q) or q in self.blocked:
                    continue
                ch, fg = self.cell(v, q)
                if ch in BOULDERS or not passable(ch, fg, q not in self.locked):
                    continue
                if k in "yubn" and (door(ch, fg) or door(*c0)):
                    continue
                trap = ch == "^" and self.traps.get(q) not in ("trap door", "hole")
                nd = d + 1 + 8 * trap + 3 * (ch == "+") + self.cost[q]
                if nd < dist.get(q, 1e9):
                    dist[q] = nd
                    if prev is not None:
                        prev[q] = p
                    heapq.heappush(heap, (nd, q))
        return dist

    def frontier(self, v, dist):
        """Reachable squares beside blank squares the hero has never been next to."""
        out, self.door_frontier = [], set()
        for p, d in dist.items():
            if not d or self.failed[p] >= 3 or self.excluded.get(p, -1) > self.now:
                continue
            if any(on_map(q) and v.ch(*q) == " " and q not in self.terr and q not in self.near for _, q in nbrs(p)):
                if self.cell(v, p)[0] == "+":   # aim beside a closed door; opening it is its own action
                    self.door_frontier.add(p)
                    p = min((q for k, q in nbrs(p) if k in "hjkl" and q in dist), key=dist.get, default=p)
                    if not dist.get(p) or self.failed[p] >= 3 or self.excluded.get(p, -1) > self.now:
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
        """Where to search for hidden passages, best first: (score, pos). None on levels without secrets."""
        if self.dlvl <= 2 or self.mines:
            return []          # no secret doors or corridors on Dlvl 1-2 or in the Mines
        out = []
        for p, d in dist.items():
            if self.searched[p] >= 30:
                continue
            ch, fg = self.cell(v, p)
            blank = sum(1 for _, q in nbrs(p) if v.ch(*q) == " " and q not in self.terr)
            walls = sum(1 for _, q in nbrs(p) if self.cell(v, q)[0] in "|-")
            if not (blank or walls):
                continue
            s = -1 - 2 * (self.searched[p] // 15) ** 2
            if door(ch, fg) and ch != "+" and blank > 3:
                s += 250       # a doorway that opens onto nothing
            if ch == "#" and sum(1 for k, q in nbrs(p) if k in "hjkl" and passable(*self.cell(v, q))
                                 and self.cell(v, q)[0] != " ") <= 1:
                s += 250       # a dead-end corridor
            if any(self.cell(v, q)[0] in BOULDERS and v.ch(2 * q[0] - p[0], 2 * q[1] - p[1]) == " "
                   for _, q in nbrs(p)):
                s += 200       # a boulder with unknown space behind it
            s += 3 * min(20, self.blank_band(v, p))
            out.append((s - 4 * d, p))
        return sorted(out, reverse=True)

    def blank_band(self, v, p):
        """Unknown cells beyond a wall within a 6-row, 20-column window: a missing room shows as a blank band."""
        n = 0
        for dr in range(-3, 4):
            for dc in range(-10, 11, 2):
                q = (p[0] + dr, p[1] + dc)
                if on_map(q) and v.ch(*q) == " " and q not in self.terr:
                    n += 1
        return n
