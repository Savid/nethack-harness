"""The legal actions for one decision, each with a rule priority."""
import re

from . import knowledge as K
from .level import cheb, compass, door, nbrs, passable, travel
from .settings import CFG, val
from .base import Act, RACE_MONSTER


class Candidates:
    def actions(self, v, c):
        hero, lv, dist, hpf, th, hs = c["hero"], c["lv"], c["dist"], c["hpf"], c["threats"], c["hostiles"]
        acts, hurt, dl, fr = [], hpf < 0.5, c["dl"], c["frontier"]
        moving_banned = self.decisions < self.move_ban_until
        adjacent = [h for h in hs if h["dist"] == 1]
        for k, q in nbrs(hero):
            ch, fg = v.ch(*q), v.fg(*q)
            m = next((h for h in hs if h["pos"] == q and not h["avoid"]), None)
            if m:
                over = c["xl"] < m["threat"]
                pr = -5 if c["on_elbereth"] else (1 if over else 2 if hurt else 4)
                acts.append(Act("attack_" + k, "Attack the %s adjacent %s%s" % (
                    m["name"], K.DN[k], " (dangerous at your level)" if over else ""), "F" + k, "attack", pr, q))
            elif (ch == "I" or ch in K.WARNING) and (c["hit"] or c["blind"]):
                acts.append(Act("attack_" + k, "Attack the unseen monster to the " + K.DN[k], "F" + k, "attack", 3.5,
                                q))
            elif ch in K.BOULDERS and v.ch(2 * q[0] - hero[0], 2 * q[1] - hero[1]) in " .#":
                acts.append(Act("push_" + k, "Push the boulder " + K.DN[k], k, "push", -1 if fr else 3, q))
            elif ch == "+" and door(ch, fg) and k in "hjkl":
                if q not in lv.locked:
                    blocked = any(re.search(r"door is closed|bump into a door", x) for x in list(self.msgs)[-2:])
                    new = q in lv.door_frontier or v.ch(2 * q[0] - hero[0], 2 * q[1] - hero[1]) == " "
                    acts.append(Act("open_" + k, "Open the closed door to the " + K.DN[k], "o" + k, "door",
                                    6 if blocked else 5 if new else 1 if fr else 2.4, q))     # before probes
                elif lv.kicks[q] < K.KICK_TRIES and self.kickable(q, c):
                    acts.append(Act("kick_" + k, "Kick open the locked door to the " + K.DN[k], "\x04" + k, "kick",
                                    0.8 if fr else 4.0, q))
            elif q in lv.traps and lv.traps[q] in ("trap door", "hole") and CFG["trapdoors"] and \
                    self.descend_ok(c) and dist.get(q) == 1:
                acts.append(Act("trapdoor_" + k, "Step onto the %s %s (a free descent)" % (lv.traps[q], K.DN[k]), k,
                                "move", 6.5, q))
            elif not moving_banned and any(h["dist"] <= 3 for h in hs) and dist.get(q) == 1 and passable(ch, fg) \
                    and ch not in K.MON:
                t = (th or hs)[0]
                nd = cheb(q, t["pos"])
                rel = "away from" if nd > t["dist"] else "toward" if nd < t["dist"] else "beside"
                what = {"#": "corridor", "<": "the up stairs", ">": "the down stairs", "^": "a TRAP"}.get(ch, "floor")
                acts.append(Act("move_" + k, "Step %s onto %s (%s the %s)" % (K.DN[k], what, rel, t["name"]), k,
                                "move", 0.5 if rel == "away from" and hurt and t["dist"] == 1 else -1, q))
        wielded = next((t for _, t in self.items() if "wielded" in K.item_state(t)), "")
        quiver = [(k, t) for k, t in self.items() if "quivered" in K.item_state(t) and K.fireable(t, wielded)]
        if quiver and CFG["ranged"] and not adjacent:
            for h in hs:
                dr, dc = h["pos"][0] - hero[0], h["pos"][1] - hero[1]
                if 2 <= h["dist"] <= 6 and (dr == 0 or dc == 0 or abs(dr) == abs(dc)) and not h.get("peaceful"):
                    k = next(k for k, d in K.DIRS.items() if d == ((dr > 0) - (dr < 0), (dc > 0) - (dc < 0)))
                    acts.append(Act("fire_" + k, "Fire %s at the approaching %s %s" % (quiver[0][1], h["name"], K.DN[k]),
                                    "f" + k, "fire", 4.2 if c["xl"] < h["threat"] or hurt else 3.6, h["pos"]))
                    break
        if quiver and not adjacent and not fr:
            for h in c["obst"]:
                dr, dc = h["pos"][0] - hero[0], h["pos"][1] - hero[1]
                if self.min_range(h) <= h["dist"] <= 6 and (dr == 0 or dc == 0 or abs(dr) == abs(dc)):
                    k = next(k for k, d in K.DIRS.items() if d == ((dr > 0) - (dr < 0), (dc > 0) - (dc < 0)))
                    acts.append(Act("fire_" + k, "Fire %s at the %s %s (it must not be meleed)" % (
                        quiver[0][1], h["name"], K.DN[k]), "f" + k, "fire", 3.2, h["pos"]))
                    break
        bolt = self.spell("attack", v)
        if bolt:
            for h in hs + c["obst"]:     # force bolt: a dangerous foe, or a blocker that must not be meleed
                dr, dc = h["pos"][0] - hero[0], h["pos"][1] - hero[1]
                straight = dr == 0 or dc == 0 or abs(dr) == abs(dc)
                worth = h in c["obst"] or c["xl"] < h["threat"] or hurt or h["dist"] == 1
                if straight and self.min_range(h) <= h["dist"] <= 6 and worth and not h.get("peaceful"):
                    k = next(k for k, d in K.DIRS.items() if d == ((dr > 0) - (dr < 0), (dc > 0) - (dc < 0)))
                    acts.append(Act("zap_" + k, "Cast %s at the %s %s" % (bolt[1], h["name"], K.DN[k]),
                                    "Z%s%s" % (bolt[0], k), "zap", 4.6 if h not in c["obst"] else 3.3, h["pos"]))
                    break
        spare = self.spare_missile() if not quiver else None
        if spare and not adjacent and not fr:
            for h in c["obst"]:          # a passive blocker on the way: throw something at it, never melee it
                dr, dc = h["pos"][0] - hero[0], h["pos"][1] - hero[1]
                if self.min_range(h) <= h["dist"] <= 4 and (dr == 0 or dc == 0 or abs(dr) == abs(dc)):
                    k = next(k for k, d in K.DIRS.items() if d == ((dr > 0) - (dr < 0), (dc > 0) - (dc < 0)))
                    acts.append(Act("throw_" + k, "Throw %s at the %s %s (it must not be meleed)" % (
                        spare[1], h["name"], K.DN[k]), "t" + spare[0] + k, "throw", 3.0, h["pos"]))
                    break
        self.door_commitment(v, c, acts)
        ok = self.descend_ok(c)
        policy = self.mines_policy()
        trapdoor_here = lv.traps.get(hero) in ("trap door", "hole")
        if c["under"] == ">" or (trapdoor_here and CFG["trapdoors"]):
            branch = lv.downs.get(hero) == "branch"
            escape = th and hpf >= 0.4 and dl < self.depth_cap(c["xl"], c["hpmax"], c["ac"])   # never past the cap
            pr = 7 if escape else 6 if ok else -3
            if branch and policy == "avoid":
                pr = 0.2
            acts.append(Act("descend", "Go down here to Dlvl %d%s" % (dl + 1, " (Gnomish Mines)" * branch), ">",
                            "descend", pr))
        downs = []
        for p, kind in list(lv.downs.items()) + [(p, "trapdoor") for p, t in lv.traps.items()
                                                 if t in ("trap door", "hole") and CFG["trapdoors"]]:
            if p in dist and p != hero:
                rank = 0 if kind == "trapdoor" and hpf >= 0.5 else 2 if kind == "branch" and policy != "allow" else 1
                downs.append((rank, dist[p], p, kind))
        downs.sort()
        if downs and not (downs[0][3] == "branch" and policy == "avoid"):
            _, d, p, kind = downs[0]
            label = {"branch": " (Gnomish Mines)", "trapdoor": " (trap door: free descent)"}.get(kind, "")
            keys = travel(hero, p) + (">" if ok else "")      # travel and descend in one send
            pr = (1.5 if th else 5.5 if kind == "trapdoor" else 5) if ok else -2
            if CFG["mode"] == "explore" and fr and pr > 1.5:
                pr = 1.5          # explore mode: see the level first
            acts.append(Act("goto_stairs", "Travel to the down stairs %d squares %s%s" % (d, compass(hero, p), label),
                            keys, "travel", pr, p))
        if lv.mines and (lv.up or c["under"] == "<") and policy == "avoid" and not th:
            acts.append(Act("leave_mines", "Leave the Gnomish Mines by the up stairs",
                            "<" if c["under"] == "<" else travel(hero, lv.up or hero) + "<", "travel", 7, lv.up))
        if c["under"] == "<" and dl > 1 and hpf < 1 / 3 and th and min(h["dist"] for h in th) >= 2:
            acts.append(Act("flee_up", "Escape up the stairs you stand on", "<", "flee", 6))
        usable = [x for x in downs if not (x[3] == "branch" and policy == "avoid")]
        imported = [q for q in lv.imported.get("down", []) if q not in lv.downs and
                     lv.excluded.get(q, -1) <= self.decisions] if not usable else []
        if imported and ok and not th:
            # another copy of this game saw down stairs here: head for them (the game's travel finds a way)
            q = min(imported, key=lambda q: cheb(q, hero))
            acts.append(Act("goto_imported_stairs", "Travel toward the down stairs another copy saw at %d,%d" % (
                q[0] + 1, q[1] + 1), travel(hero, q), "travel", 4.6, q))
        if fr:
            if imported:      # explore toward the imported stairs first
                d, p = min(fr, key=lambda x: x[0] + 2 * cheb(x[1], min(imported, key=lambda q: cheb(q, hero))))
            else:
                d, p = self.rng.choice([x for x in fr if x[0] <= fr[0][0] + 1])
            keys = travel(hero, p)
            if d == 1 and lv.cell(v, p)[0] == "#":
                keys = "G" + next(k for k, q in nbrs(hero) if q == p)    # follow a corridor in one command
            pr = (2.5 if not usable or CFG["mode"] == "explore" or not ok else 0.5) - (2.5 if th else 0)
            pr += 2 if self.decisions < self.boost_until else 0
            acts.append(Act("explore", "Explore toward the nearest unexplored area, %d squares %s" % (
                d, compass(hero, p)), keys, "explore", pr, p))
        if CFG["mapping"] == 2 and "mapping" not in lv.probed and not th and self.items(K.MAPPING.pattern):
            acts.append(Act("read_mapping", "Read a scroll of magic mapping on arrival (mapping=2)", "r", "read", 6.0))
        calm_level = not hs and not c["hit"]
        if calm_level and not fr and not usable:
            acts += self.ladder_actions(v, c)
        if calm_level and hpf < val("rest_hp"):
            acts.append(Act("rest", "Rest 20 turns to regain HP (HP %d/%d, no monsters in view)" % (
                c["hp"], c["hpmax"]), "20s", "rest", 3 + 2 * (hpf < 0.35) - (c["hungry"] is not None)))
        if (c["blind"] or c["hallu"]) and not c["hit"]:
            acts.append(Act("rest", "Wait out blindness or hallucination (20 turns)", "20s", "rest", 4))
        if c["on_elbereth"] and any(h["dist"] <= 3 for h in hs) and hpf < 0.7 and not c["ranged"]:
            acts.append(Act("rest_s", "Rest one turn on the verified Elbereth", "ms", "rest", 5))
        if hs and not adjacent and self.waits < 5:
            acts.append(Act("wait", "Wait two turns and let monsters come to you", "2s", "wait",
                            0.8 if hurt and th else -1.5))
        self.food_actions(v, c, acts)
        self.emergency_actions(v, c, acts)
        wet = any(v.ch(*q) == "}" for _, q in nbrs(hero))
        if CFG["dig"] and not (lv.no_dig or lv.shop or wet) and c["under"] not in "<>{_\\" and ok and not th:
            tool = self.items("|".join(K.DIG_TOOLS))
            if tool:
                acts.append(Act("dig", "Dig down through the floor with the " + tool[0][1], "a" + tool[0][0], "dig",
                                4.8 if usable else 5.6))
        if c["ranged"]:
            acts = [a for a in acts if a.kind not in ("search", "rest", "wait", "elbereth")] + self.leave_line(v, c)
        if self.crisis_active(c):
            ladder = self.crisis_ladder(v, c, acts)
            self.crisis["empty"] = not ladder
            if ladder:
                return ladder
        acts = self.through_doors(v, c, acts)
        if not acts:
            acts.append(Act("search", "Search here 15 turns", "15s", "search", -3))
        if adjacent or c["hit"]:   # never start a counted search, rest or wait with a hostile next to you
            acts = [a for a in acts if a.kind not in ("search", "rest", "wait") or a.key == "rest_s" and c["on_elbereth"]]
        acts = [a for a in acts if not lv.banned(hero, a.key, self.decisions)]
        if lv.stair_ban_until > self.decisions:
            acts = [a for a in acts if a.key not in ("descend", "goto_stairs", "leave_mines", "flee_up")]
        return sorted(acts, key=lambda a: -a.prior) or [Act("search", "Search here 15 turns", "15s", "search", -3)]

    def linger_act(self, v, c, cap):
        """While the depth gate holds: walk to a random known square some way off (monsters come to a moving
        hero, and walking costs less food than searching in place); search only when nowhere is reachable."""
        lv = c["lv"]
        if c["dl"] > cap and lv.up and lv.up in c["dist"]:
            # below the cap (a trap door, a fall): climb back toward it rather than linger where it is too deep
            return Act("linger", "Climb back toward the depth cap (Dlvl %d) by the up stairs %s" % (
                cap, compass(c["hero"], lv.up)), ("<" if c["hero"] == lv.up else travel(c["hero"], lv.up) + "<"),
                "travel", 0, lv.up)
        far = [p for p, d in c["dist"].items() if 6 <= d <= 40]
        if far:
            p = self.rng.choice(far)
            return Act("linger", "Wander %s while the depth gate holds (Dlvl %d at most for now)" % (
                compass(c["hero"], p), cap), travel(c["hero"], p), "travel", 0, p)
        return Act("linger", "Search 20 turns while the depth gate holds (Dlvl %d at most for now)" % cap, "20s",
                   "search", 0)

    def leave_line(self, v, c):
        """Steps out of every hostile's row, column and diagonal (a ranged attacker needs a straight line)."""
        hero, dist = c["hero"], c["dist"]
        shooters = [h["pos"] for h in c["hostiles"] if 2 <= h["dist"] <= 8]

        def in_line(p):
            return any(p[0] == s[0] or p[1] == s[1] or abs(p[0] - s[0]) == abs(p[1] - s[1]) for s in shooters)

        if not shooters or not in_line(hero):
            return []
        out = []
        for k, q in nbrs(hero):
            if dist.get(q) == 1 and v.ch(*q) not in K.MON and not in_line(q):
                out.append(Act("leave_line_" + k, "Step %s out of the ranged attacker's line" % K.DN[k], k, "move",
                               7.5, q))
        return out[:1]

    def lv_downs_usable(self, c):
        return any(p in c["dist"] for p in c["lv"].downs)

    def door_commitment(self, v, c, acts):
        """Once the loop sets off for a locked door, it finishes the job (travel, then kick until it opens or
        the kicks run out) before exploring or searching elsewhere; fights still come first."""
        dp, lv, hero = self.door_plan, c["lv"], c["hero"]
        if dp and (dp[0] != c["dl"] or dp[1] not in lv.locked or lv.kicks[dp[1]] >= K.KICK_TRIES or
                   self.decisions > dp[3] or not self.kickable(dp[1], c)):
            self.door_plan = dp = None
        if not dp or c["threats"]:
            return
        kick = [a for a in acts if a.kind == "kick" and a.target == dp[1]]
        for a in kick:
            a.prior = 6
        if not kick and hero != dp[2] and dp[2] in c["dist"]:
            act = Act("goto_door", "Continue to the locked door %s to kick it open" % compass(hero, dp[1]),
                      travel(hero, dp[2]), "travel", 6, dp[2])
            act.door = dp[1]
            acts.append(act)

    def through_doors(self, v, c, acts):
        """The game's travel command stops at closed doors. Rewrite each travel whose known route crosses one:
        travel to the square before the first door, then open it (or kick it when locked)."""
        hero, lv = c["hero"], c["lv"]
        for i, a in enumerate(acts):
            if not (a.keys.startswith("_") and a.target and a.kind in ("travel", "explore")):
                continue
            target = a.target if a.target in c["dist"] else None
            route = lv.route(v, hero, target) if target else None
            if not route:
                continue
            j = next((n for n, q in enumerate(route) if lv.cell(v, q)[0] == "+"), None)
            if j is None:
                continue
            q, before = route[j], (route[j - 1] if j else hero)
            where = compass(hero, q)
            if before != hero:
                acts[i] = Act(a.key, "%s (first to the closed door %s)" % (a.desc, where), travel(hero, before),
                              a.kind, a.prior, before)
                continue
            k = next(k for k, n in nbrs(hero) if n == q)
            if q in lv.locked:
                if lv.kicks[q] < K.KICK_TRIES and self.kickable(q, c):
                    acts[i] = Act("kick_" + k, "%s: kick the locked door %s on the way" % (a.desc, K.DN[k]),
                                  "\x04" + k, "kick", a.prior, q)
                else:
                    acts[i] = Act(a.key, a.desc + " (unreachable: locked door %s)" % K.DN[k], a.keys, a.kind, -3,
                                  a.target)
            else:
                acts[i] = Act("open_" + k, "%s: open the closed door %s on the way" % (a.desc, K.DN[k]), "o" + k,
                              "door",
                              a.prior, q)
        return acts

    def kickable(self, q, c):
        """Kick a locked door, but never a shop's (an angry shopkeeper kills) or in front of the watch."""
        lv = c["lv"]
        if lv.no_kick or c["watch"] or q in lv.shop_doors:
            return False
        return not any("shopkeeper" in h["name"] and cheb(h["pos"], q) <= 3 for h in c["peace"] + c["obst"])

    def ladder_actions(self, v, c):
        """No frontier and no usable stairs: the escape ladder, one rung at a time."""
        hero, lv, dist = c["hero"], c["lv"], c["dist"]
        acts = []
        lv.locked = {q for q in lv.locked if v.ch(*q) in "+ "}       # broken or opened doors are no longer locked
        doors = [q for q in lv.locked if lv.kicks[q] < K.KICK_TRIES and self.kickable(q, c)]
        if any(cheb(q, hero) == 1 and (q[0] == hero[0] or q[1] == hero[1]) for q in doors):
            doors = []          # kick the door beside you first (the kick action outranks travelling)
        for q in sorted(doors, key=lambda q: min([dist.get(n, 999) for _, n in nbrs(q)] or [999])):
            spot = min((n for k, n in nbrs(q) if k in "hjkl" and n in dist), key=dist.get, default=None)
            if spot is not None and spot != hero:
                act = Act("goto_door", "Go to the locked door %s to kick it open" % compass(hero, q),
                          travel(hero, spot), "travel", 3.5, spot)
                act.door = q
                acts.append(act)
                break
        if CFG["probe"] and "stairs" not in lv.probed:
            acts.append(Act("probe_stairs", "Ask the game where known down stairs are (travel prompt)", "", "probe",
                            4.5))
        if "frontier" not in lv.probed:
            acts.append(Act("probe_frontier", "Ask the game for an unexplored edge (travel prompt)", "", "probe", 4))
        if CFG["mapping"] and "mapping" not in lv.probed and self.items(K.MAPPING.pattern) and \
                (lv.search_turns >= CFG["search_budget"] // 2 or lv.dlvl <= 2 or lv.mines):
            acts.append(Act("read_mapping", "Read a scroll of magic mapping to reveal the level", "r", "read", 3.8))
        extra = lv.extra_budget          # granted each time the level is reported exhausted
        budget_left = lv.search_turns < CFG["search_budget"] + extra and \
            lv.search_actions < (CFG["search_budget"] + extra) // 6     # searches are 15 turns; slack for cut-offs
        spots = lv.spots(v, dist) if budget_left else []
        if spots:
            here = next((s for s, p in spots if p == hero), None)
            best_s, best = spots[0]
            if here is not None and here >= best_s - 20:
                acts.append(Act("search", "Search here 15 turns for hidden passages", "15s", "search", 3))
            else:
                acts.append(Act("goto_search", "Go to a likely hidden-passage spot %s to search" % compass(hero, best),
                                travel(hero, best), "explore", 3, best))
        elif budget_left and (not (lv.dlvl <= 2 or lv.mines) or lv.search_turns < 45 + extra):
            acts.append(Act("search", "Search here 15 turns for hidden passages", "15s", "search", 1))
        return acts

    def food_actions(self, v, c, acts):
        hero, dl, hungry = c["hero"], c["dl"], c["hungry"]
        cs = self.corpse
        if hungry and cs and cs[0] == dl and c["turn"] - cs[3] <= 50 and cs[2].endswith(K.SAFE_CORPSES) \
                and not K.UNSAFE_CORPSE.search(cs[2]) and not c["threats"] \
                and not (self.race and RACE_MONSTER.get(self.race, "?") in cs[2]):
            if hero == cs[1]:
                acts.append(Act("eat_corpse", "Eat the fresh %s corpse here (you are %s)" % (cs[2], hungry), "e",
                                "eat_corpse", 5))
            elif c["dist"].get(cs[1], 99) <= 4:
                acts.append(Act("goto_corpse", "Step to the fresh %s corpse to eat it" % cs[2],
                                travel(hero, cs[1]), "travel", 4.5, cs[1]))
        if hungry and c["turn"] >= self.food_off_until:
            food = self.food_letters()
            if food or self.inv_turn < 0:
                acts.append(Act("eat", "Eat %s (you are %s)" % (food[0][1] if food else "food", hungry), "e", "eat",
                                (0 if c["threats"] else 3.5) + 3 * (hungry != "Hungry")))

    def food_letters(self):
        out = []
        for k, (t, sec) in self.inv.items():
            if "corpse" in t and not re.search(r"lichen|lizard", t):
                continue
            i = K.food_index(t)
            if sec == "Comestibles" or i is not None:
                out.append((50 if i is None else i, t, k))
        return [(k, t) for _, t, k in sorted(out)]

    def emergency_actions(self, v, c, acts):
        hpf, th = c["hpf"], c["threats"]
        if c["can_pray"] and c["trouble"]:
            acts.append(Act("pray", "Pray (in serious trouble and the prayer timeout looks safe)", "", "pray", 9))
        low = hpf < 1 / 3 or c["hp"] < 8
        heal = self.items(K.HEALING.pattern)
        if low and CFG["potions"] and heal and (th or c["hit"] or hpf < 0.2):
            acts.append(Act("quaff", "Quaff the %s" % heal[0][1], "q" + heal[0][0], "quaff", 8.5))
        heal_spell = self.spell("heal", v)
        if heal_spell and hpf < 0.5:
            acts.append(Act("cast_heal", "Cast %s on yourself" % heal_spell[1], "Z%s." % heal_spell[0], "cast",
                            7 if th else 4))
        adjacent_threat = [h for h in th if h["dist"] == 1 and (hpf < val("elbereth_hp") or c["xl"] < h["threat"])]
        if CFG["elbereth"] and adjacent_threat and self.elbereth_useful(v, c, adjacent_threat):
            acts.append(Act("elbereth", "Engrave Elbereth in the dust to scare monsters away", "", "elbereth", 6.5))

    def elbereth_useful(self, v, c, attackers):
        """Engrave only where it has not just failed, against attackers that respect it, with no ranged attack."""
        failed = self.elbereth_failed
        return not c["on_elbereth"] and "Lev" not in v.cond and not c["ranged"] and \
            not (failed and failed[:2] == (c["dl"], c["hero"]) and c["turn"] - failed[2] < 100) and \
            not any(h["ch"] in K.ELBERETH_IGNORERS or K.ELBERETH_IGNORER_NAMES.search(h["name"]) for h in attackers)
