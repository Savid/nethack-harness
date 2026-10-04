"""Carrying out actions and plan items, with the game's prompts in between."""
import os
import re

from . import knowledge as K
from .level import on_map, travel
from .base import unescape, Act, Hard


class Execution:
    def do(self, v, c, a):
        lv = c["lv"]
        self.last_act = a
        if a.key == "flee_swarm" and c["swarm"]:
            self.swarm_fled = (c["dl"], "%d %s" % (len(c["swarm"]), c["swarm"][0]["name"]))
            lv.hazards.add("swarm: " + self.swarm_fled[1])
        if a.key == "leave_mines":
            self.level_trail.clear()    # out of the Mines and on to the main stairs: not a stair ping-pong
        if a.key == "flee_up" or (a.kind == "retreat" and a.target == lv.up):
            # remember who drove us off this level: a stair ping-pong is then reported as a camped arrival
            self.fled_from[c["dl"]] = [(h["name"], h["pos"]) for h in c["hostiles"] if h["dist"] <= 3][:3]
        if a.kind == "read":
            lv.probed.add("mapping")
        if a.kind == "throw":
            self.inv_turn = -2           # the pack changed: re-read it
        if a.key == "goto_door" and a.door:
            if not self.door_plan or self.door_plan[1] != a.door:
                self.door_plan = (c["dl"], a.door, a.target, self.decisions + 30)
        if a.kind == "pray":
            self.praying = True
            try:
                self.flow("#pray\r", until=14)        # the prayer is recorded from "You begin praying"
            finally:
                self.praying = False
            return
        if self.crisis_active(c):
            self.crisis["tried"].append(a.key)
        if a.kind == "elbereth":
            self.engrave_elbereth(c)
            return
        if a.key == "rest_s":
            if self.read_engraving().lower() != "elbereth":      # scuffed since: engrave it again, once
                self.note("elbereth", "the engraving no longer reads Elbereth: engraving again")
                if not self.engrave_elbereth(c):
                    self.elbereth_failed, self.elbereth_at = (c["dl"], c["hero"], c["turn"]), None
                return
            self.send("ms")
            return
        if a.kind == "eat_corpse":
            self.eat_floor(c)
            return
        if a.kind == "eat":
            self.eat_pack(c)
            return
        if a.kind in ("quaff", "read"):
            want = K.HEALING if a.kind == "quaff" else K.MAPPING
            self.send(a.keys[0])
            for _ in range(4):
                w = self.term.view()
                if w.obj is not None:
                    letters = re.sub(r"[^a-zA-Z]", "", w.obj.split(" or ")[0])
                    pick = next((k for k, t in self.items(want.pattern) if k in letters), None)
                    self.send(pick or "\x1b")
                    self.inv_turn = -2                 # the pack changed: re-read it soon
                    if not pick:
                        self.inv = {k: x for k, x in self.inv.items() if not want.search(x[0])}
                elif w.more:
                    self.message(" ".join(r.strip() for r in w.rows[:2]).replace("--More--", "").strip(), w)
                    self.send(" ")
                elif w.yn:
                    self.answer(w)
                else:
                    break
            return
        if a.kind == "probe":
            return self.probe(v, c, a)
        if a.kind == "dig":
            self.send(a.keys)
            for _ in range(6):
                w = self.term.view()
                if w.yn or (w.msg and "direction" in w.msg and "dig" in w.msg):
                    self.send(">")
                elif w.more:
                    self.message(" ".join(r.strip() for r in w.rows[:2]).replace("--More--", "").strip(), w)
                    self.send(" ")
                elif not w.normal:
                    self.send("\x1b")
                else:
                    break
            return
        if a.kind == "search":
            lv.credit_search(c["hero"], 15)
        if a.kind == "kick":
            # read the engraving underfoot first (no game time): a closed shop announces itself there
            if re.search(r"[Cc]losed for inventory", self.read_engraving()):
                self.mark_closed_shop(lv, c["hero"])
                return
            lv.kicks[a.target] += 1
        self.send(a.keys)
        if a.kind in ("explore", "travel"):
            w = self.term.view()
            if w.getpos:
                self.send("\x1b")

    def probe(self, v, c, a):
        lv, hero = c["lv"], c["hero"]
        which = "stairs" if a.key == "probe_stairs" else "frontier"
        lv.probed.add(which)
        self.send("_" + (">" if which == "stairs" else "x"))
        w = self.term.view()
        if "Can't find" in w.msg or w.cursor == hero or not on_map(w.cursor):
            self.send("\x1b\x1b")
            self.note("probe", "%s probe: nothing known" % which)
            return
        p = w.cursor
        if which == "stairs" and (v.ch(*p) == "<" or p == lv.up):
            # the cursor's stair keys visit up staircases too: this one is no way down
            self.send("\x1b\x1b")
            self.note("probe", "stairs probe: only the up stairs at %s" % (p,))
            return
        if which == "stairs":
            lv.downs.setdefault(p, "main")
            self.note("probe", "stairs probe found > at %s" % (p,))
            self.send("." + (">" if self.descend_ok(c) else ""))
        else:
            self.note("probe", "frontier probe: travel to %s" % (p,))
            self.send(".")

    @staticmethod
    def check_plan(item):
        """Raise ValueError unless ITEM is a plan item run_plan understands."""
        kind, _, arg = item.partition(":")
        if kind == "keys" and arg:
            return
        if kind == "replay":
            path = re.sub(r":\d+$", "", arg)
            if not path or not os.path.isfile(path):
                raise ValueError("replay:FILE wants a readable file of key lines (keys --raw prints them)")
            return
        if kind == "hex":
            bytes.fromhex(arg)
            return
        goal, _, param = arg.partition(":")
        if kind != "goal" or goal not in ("pray", "rest", "search", "dig", "stairs", "up", "travel", "explore",
                                          "elbereth", "quaff", "retreat", "fight"):
            raise ValueError("unknown plan item %r (help plan)" % item)
        if goal == "quaff" and param and not re.fullmatch(r"[a-zA-Z]", param):
            raise ValueError("goal:quaff takes an inventory letter, e.g. goal:quaff:f")
        if goal == "fight":
            d, _, n = param.partition(":")
            if d not in K.DIRS or (n and not 1 <= int(n) <= 20):
                raise ValueError("goal:fight wants a direction and an optional count 1-20, e.g. goal:fight:h:4")
        if goal == "rest" and param:
            if not 0 < float(param) <= 1:
                raise ValueError("goal:rest wants an HP fraction in (0, 1]")
        elif goal in ("search", "explore") and param:
            if int(param) <= 0:
                raise ValueError("goal:%s wants a positive whole number" % goal)
        elif goal == "travel":
            r, col = (int(x) for x in param.split(","))
            if not (2 <= r <= 22 and 1 <= col <= 80):
                raise ValueError("goal:travel wants ROW,COL on the map (rows 2-22, columns 1-80)")

    def plan_fight(self, v, c, param):
        """goal:fight:DIR[:N]: one attack per step, at most N, stopping when HP falls 15% of max since the
        fight began, a new hostile comes adjacent, or nothing is left to hit in that direction."""
        d, _, n = param.partition(":")
        left = int(n or 4)
        q = (c["hero"][0] + K.DIRS[d][0], c["hero"][1] + K.DIRS[d][1])
        adjacent = {h["pos"] for h in c["hostiles"] if h["dist"] == 1}
        start = self.fight_plan if self.fight_plan and self.fight_plan[0] == c["dl"] else None
        if start is None:
            start = self.fight_plan = (c["dl"], c["hp"], adjacent)
        why = None
        if c["hp"] <= start[1] - 0.15 * c["hpmax"]:
            why = "HP fell %d -> %d" % (start[1], c["hp"])
        elif adjacent - start[2] - {q}:
            why = "a new hostile came adjacent"
        elif v.ch(*q) not in K.MON and v.ch(*q) != "I":
            why = "nothing left to attack %s" % K.DN[d]
        if why or left <= 0:
            self.plan.popleft()
            self.fight_plan = None
            if why and why.startswith(("HP", "a new")):
                raise Hard("goal:fight stopped: " + why)
            return False
        self.plan[0] = "goal:fight:%s:%d" % (d, left - 1)
        if left - 1 <= 0:
            self.plan.popleft()
            self.fight_plan = None
        self.send("F" + d)
        return True

    def plan_replay(self, v, c, arg):
        """replay:FILE[:N]: send FILE's key lines (as `keys --raw` prints them) one per step, from line N; stop
        and escalate when HP falls 15% of max below where the replay began or the screen is not a normal one."""
        path, _, start = arg.rpartition(":") if re.search(r":\d+$", arg) else (arg, "", "0")
        try:
            with open(path) as f:
                lines = [line.rstrip("\n") for line in f if line.strip()]
        except OSError as e:
            self.plan.popleft()
            raise Hard("plan replay: cannot read %s (%s)" % (path, e.strerror))
        i = int(start or 0)
        if i == 0:
            self.replay_hp = c["hp"]
        if i >= len(lines):
            self.plan.popleft()
            self.note("plan", "replay of %s done (%d sends)" % (path, len(lines)))
            return False
        if c["hp"] <= (self.replay_hp or c["hp"]) - 0.15 * c["hpmax"]:
            self.plan.popleft()
            raise Hard("plan replay stopped at line %d of %s: HP fell %d -> %d" % (i + 1, path, self.replay_hp,
                                                                                   c["hp"]))
        self.plan[0] = "replay:%s:%d" % (path, i + 1)
        self.key_source = "replay"
        try:
            self.send(unescape(lines[i]))
        finally:
            self.key_source = "loop"
        return True

    def run_plan(self, v, c):
        """Execute the next queued plan item. Returns True when it acted."""
        item = self.plan[0]
        kind, _, arg = item.partition(":")
        if kind == "keys" or kind == "hex":
            self.plan.popleft()
            keys = arg if kind == "keys" else bytes.fromhex(arg)
            self.note("plan", "keys %r" % (arg[:40],))
            self.key_source = "plan"
            try:
                self.send(keys)
            finally:
                self.key_source = "loop"
            return True
        if kind == "replay":
            return self.plan_replay(v, c, arg)
        goal, _, param = arg.partition(":")
        lv, hero = c["lv"], c["hero"]
        if goal == "pray":
            self.plan.popleft()
            self.do(v, c, Act("pray", "pray (plan)", "", "pray"))
            return True
        if goal == "rest":
            target = float(param or 0.95)
            if c["hpf"] >= target or c["hostiles"] or c["hit"] or c["threats"]:
                self.plan.popleft()
                return False
            self.send("20s")
            return True
        if goal == "search":
            left = int(param or 15)
            if left > 15:
                self.plan[0] = "goal:search:%d" % (left - 15)
            else:
                self.plan.popleft()
            lv.credit_search(hero, min(15, left))
            self.send("%ds" % min(15, left))
            return True
        if goal == "elbereth":
            self.plan.popleft()
            if not self.engrave_elbereth(c):
                raise Hard("goal:elbereth: the engraving did not read Elbereth after two tries")
            return True
        if goal == "quaff":
            self.plan.popleft()
            letter = param or next((k for k, _ in self.items(K.HEALING.pattern)), None)
            if not letter:
                raise Hard("goal:quaff: no known healing potion; name one with goal:quaff:LETTER")
            self.flow("q" + letter, until=6)
            self.inv_turn = -2
            return True
        if goal == "retreat":
            self.plan.popleft()
            act = self.retreat_act(v, c)
            if not act:
                raise Hard("goal:retreat: no stairs within 8 steps clear of the attackers and no square next to "
                           "fewer of them")
            self.do(v, c, act)
            if act.desc.startswith("Retreat to the"):     # then take the stairs, once standing on them
                self.plan.appendleft("goal:up" if " up " in act.desc else "goal:stairs")
            return True
        if goal == "fight":
            return self.plan_fight(v, c, param)
        if goal == "dig":
            tool = self.items("|".join(K.DIG_TOOLS))
            self.plan.popleft()
            if not tool:
                raise Hard("plan goal dig: no pick-axe or mattock in the pack")
            self.do(v, c, Act("dig", "dig (plan)", "a" + tool[0][0], "dig"))
            return True
        if goal in ("stairs", "up"):
            # travel first; the stairs key goes only once standing on the stairs; re-plan if travel stops short
            key = ">" if goal == "stairs" else "<"
            targets = [p for p in lv.downs if p in c["dist"]] if goal == "stairs" else \
                ([lv.up] if lv.up and lv.up in c["dist"] else [])
            if c["under"] == key:
                self.plan.popleft()
                self.plan_tries = 0
                self.send(key)
                return True
            self.plan_tries += 1
            if not targets or self.plan_tries > 3:
                self.plan.popleft()
                self.plan_tries = 0
                if goal == "stairs" and not targets:
                    self.probe(v, c, Act("probe_stairs", "", "", "probe"))
                    return True
                raise Hard("goal:%s: %s" % (goal, "no known way to the stairs" if not targets
                                             else "travel stopped short three times"))
            self.send(travel(hero, min(targets, key=c["dist"].get)))
            return True
        if goal == "travel":
            self.plan.popleft()
            r, col = (int(x) for x in param.split(","))
            self.send(travel(hero, (r - 1, col - 1)))
            return True
        if goal == "explore":
            self.plan.popleft()
            self.boost_until = self.decisions + int(param or 20)
            return False
        self.plan.popleft()
        raise Hard("unknown plan item %r" % item)
