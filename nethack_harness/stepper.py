"""One decision: bookkeeping, outcomes, escalations, the model's say, then the action."""
import collections
import time

from . import knowledge as K
from .escalation import BY_CODE, classify
from .hooks import HookError
from .level import cheb, compass, pos1
from .settings import CFG, val
from .base import Act, Hard


class Stepper:
    def milestone(self, c):
        """milestone=depth|xl|both: pause once at each new deepest level and/or new XL, when healthy (HP at or
        above milestone_hp, no hostile in view); otherwise wait for a healthy moment. Never on the first step."""
        dl, xl = c["dl"], c["xl"]
        if self.ms_dl is None:
            self.ms_dl, self.ms_xl = dl, xl
            return
        want = CFG["milestone"]
        depth = dl > self.ms_dl and dl >= CFG["milestone_from"] and want in ("depth", "both")
        level = xl > self.ms_xl and want in ("xl", "both")
        if not depth and dl > self.ms_dl:
            self.ms_dl = dl              # not wanted: remember silently, so switching it on later is not stale
        if not level and xl > self.ms_xl:
            self.ms_xl = xl
        if not (depth or level) or c["hpf"] < CFG["milestone_hp"] or c["hostiles"]:
            return
        what = "new deepest Dlvl %d" % dl if depth else "new XL %d" % xl
        self.ms_dl, self.ms_xl = max(self.ms_dl, dl), max(self.ms_xl, xl)
        self.milestones.append({"kind": "depth" if depth else "xl", "dlvl": dl, "xl": xl, "turn": c["turn"]})
        raise Hard("milestone: %s (XL %d, HP %d/%d, T%d; down stairs known: %s)" % (
            what, xl, c["hp"], c["hpmax"], c["turn"], "yes" if c["lv"].downs else "no"))

    def judge_outcome(self, v, c):
        t = self.last_try
        if not t:
            return
        lv, hero = c["lv"], c["hero"]
        if c["dl"] != t["dl"]:
            out = "level"
        elif len(lv.near) > t["known"]:
            out = "new"
        elif c["turn"] == t["turn"] and hero == t["hero"]:
            out = "noop"
        elif t.get("target") and t["kind"] in ("explore", "travel"):
            now = c["dist"].get(t["target"])
            out = "nocloser" if now is None or (t["tdist"] is not None and now >= t["tdist"] and hero != t["target"]) \
                else "time"
        else:
            out = "time"
        if c["dl"] == t["dl"] and cheb(t["hero"], hero) > 1:
            lv.walked(v, t["hero"], hero)
            c["dist"] = lv.paths(v, hero)
            c["frontier"] = lv.frontier(v, c["dist"])
        if out == "noop":
            lv.noops[(t["hero"], t["key"])] += 1
            if lv.noops[(t["hero"], t["key"])] >= 2:
                # Moves can be blocked for a while (a pet, a peaceful): ban them briefly; other no-ops for good.
                temporary = t["kind"] in ("explore", "travel", "move", "door")
                lv.ban(t["hero"], t["key"], self.decisions + 15 if temporary else -1)
                lv.noops[(t["hero"], t["key"])] = 0
            self.frozen += 1
        else:
            self.frozen = 0
        if out == "nocloser" and t.get("target"):
            lv.failed[t["target"]] += 1
            if lv.failed[t["target"]] >= 2:
                lv.excluded[t["target"]] = self.decisions + 40   # try other targets for a while
                lv.failed[t["target"]] = 0
        self.outcomes.append(out)
        if t["kind"] in ("wait", "search", "rest"):
            return               # standing still on purpose is not an oscillation between squares
        self.visits.append(hero)
        top = collections.Counter(self.visits).most_common(1)
        if top and top[0][1] >= 3 and not ({"new", "level"} & set(self.outcomes)) and len(self.outcomes) >= 6:
            self.move_ban_until = self.boost_until = self.decisions + 10
            self.visits.clear()
            self.note("stall", "oscillation: moves banned for 10 decisions")

    def oscillation(self, dl):
        """A generic futility check: two positions or two actions alternating, or the level toggling."""
        levels = list(self.level_trail)[-8:]
        if len(levels) >= 4 and len(set(levels[-4:])) <= 2 and levels[-1] == levels[-3] and \
                levels[-2] == levels[-4] and self.max_dl <= max(levels):
            self.level_trail.clear()
            for d in set(levels):
                self.level(d).stair_ban_until = self.decisions + 60
            return "oscillating: Dlvl %s <-> %s %d times; the involved stairs are now avoided" % (
                levels[-1], levels[-2], len(levels))
        t = [x for x in self.trail if x[2] not in ("search_more", "linger", "rest_s")][-12:]   # deliberate waits
        if len(t) >= 10:
            spots = collections.Counter((x[0], x[1]) for x in t)
            keys = collections.Counter(x[2] for x in t)
            if len(spots) <= 2 and len(keys) <= 2 and self.decisions - self.progress >= 8:
                lv = self.level(dl)
                for (d, pos, key) in t:
                    lv.ban(pos, key, self.decisions + 30)
                self.trail.clear()
                return "oscillating: %s at %s, %d times without progress (those actions are paused for 30 decisions)" % (
                    " / ".join(sorted(keys)), " / ".join(pos1(p) for _, p in spots), len(t))
        return None

    def step(self):
        try:
            return self._step()
        except Hard as e:
            return self.esc(str(e))

    def _step(self):
        v = self.view()
        self.turn = v.st.get("turn", self.turn)
        reason = self.interrupts(v)
        if reason:
            return reason
        if not v.normal:
            self.term.settle()          # output may still be arriving
            v = self.view()
            if v.asking and not v.prompt and v.msg:
                time.sleep(0.3)
                self.term.poll()
                v = self.view()
                if v.asking and not v.prompt and v.msg:
                    raise Hard("unknown text prompt (nothing sent): " + v.msg[:160])
            if not v.normal:
                self.bad_screens += 1
                if self.bad_screens > 6:
                    self.bad_screens = 0
                    raise Hard("unrecognised screen; inspect it and resume")
                if self.bad_screens > 3:
                    self.send("\x1b")
                else:
                    time.sleep(0.1)
                return None
        self.bad_screens = 0
        if not self.options:
            self.setup()
            return None
        if not self.briefed:
            self.briefed = True
            self.learn_character()
            self.learn_spells()
            self.read_inventory()
            if CFG["briefing"]:
                raise Hard("briefing")
            return None
        if self.inv_turn == -2 or (self.turn or 0) - self.inv_turn > 1500:
            self.read_inventory()
            return None
        if self.engulfed and not v.engulfed and v.st.get("turn", 0) - self.engulfed > 30:
            self.engulfed = False          # no box and no news for 30 turns: it is over
        if v.engulfed or self.engulfed:
            self.engulf_sends += 1
            hp, hpmax, xl = v.st.get("hp", 1), max(1, v.st.get("hpmax", 1)), v.st.get("xl", 1)
            div = 5 if xl <= 5 else 6 if xl <= 13 else 7
            if (hp <= 5 or hp * div <= min(hpmax, 15 * xl)) and self.prayer_safe(v.st.get("turn", 0)):
                self.do(v, {"turn": v.st.get("turn", 0), "lv": self.level(v.st.get("dlvl", 0)),
                            "hero": v.hero, "dl": v.st.get("dlvl", 0)}, Act("pray", "pray", "", "pray"))
                return None
            if self.engulf_sends > 15:
                self.engulf_sends = 0
                raise Hard("engulfed for 15 attacks")
            self.send("Fh")
            self.note("act", "attack the engulfer (Fh)", turn=v.st.get("turn", 0))
            return None
        self.engulf_sends = 0
        if v.st.get("dlvl") is not None and v.st["dlvl"] != self.branch_dl:
            self.track_branch(v.st["dlvl"], v)
            v = self.view()
        c = self.context(v)
        v = self.view()
        if not v.normal:
            return None
        self.bookkeep(v, c)
        self.hp_hist.append((c["turn"], c["hp"]))
        if not self.hp_trail or self.hp_trail[-1][:2] != (c["turn"], c["hp"]):
            self.hp_trail.append((c["turn"], c["hp"], c["hpmax"]))
        if self.plan and self.run_plan(v, c):
            self.last_try = None
            return None
        mark = (self.max_dl, sum(len(x.near) for x in self.lv.values()))   # level toggles are not progress
        if mark != self.mark:
            self.mark, self.progress, self.progress_turn = mark, self.decisions, c["turn"]
            self.progress_time = self.clock()
        acts = self.actions(v, c)
        reason, acts, near = self.situation(v, c, acts)
        if reason and self.esc(reason, hp=c["hp"], hpmax=c["hpmax"]):
            return reason
        return self.decide_and_act(v, c, acts, near)

    def bookkeep(self, v, c):
        """After each look: outcome of the last action, frozen turns, level arrivals, milestones, blindness and
        branch points (the last raise Hard: they always reach the outer loop)."""
        dl, lv = c["dl"], c["lv"]
        self.judge_outcome(v, c)
        if self.frozen >= 16:
            self.frozen = 0
            raise Hard("frozen: 16 actions without the turn counter moving")
        if self.frozen >= 8 and self.frozen % 8 == 0:
            self.send("\x1b\x1b")
            self.term.sync()
        branch_reason = None
        if dl != self.prev_dl:
            if self.prev_dl is not None:
                self.note("level", "arrived on Dlvl %d (turn %s)" % (dl, c["turn"]))
                self.level_trail.append(dl)
                if dl > self.prev_dl:
                    if self.last_down and self.last_down[0] == self.prev_dl:
                        self.edges[self.last_down] = dl
                        if c["under"] == "?" and not any(K.FELL.search(m) for m in list(self.msgs)[-3:]):
                            lv.terr[c["hero"]], lv.tfg[c["hero"]] = "<", "default"   # came down the stairs
                            c["under"], lv.up = "<", c["hero"]
                    if dl - self.prev_dl >= 2 or dl > c["xl"] + 3:
                        branch_reason = "depth jump: Dlvl %d -> %d at XL %d" % (self.prev_dl, dl, c["xl"])
            self.prev_dl = dl
        if c["under"] == "?" and c["turn"] <= 1:
            lv.terr[c["hero"]], lv.tfg[c["hero"]] = "<", "default"   # the game starts on the up stairs
            c["under"] = "<"
        self.max_dl = max(self.max_dl, dl)
        self.milestone(c)
        if c["blind"]:
            self.blind_since = self.blind_since if self.blind_since is not None else c["turn"]
            if c["turn"] - self.blind_since > 300:
                self.blind_since = c["turn"]
                raise Hard("blind for 300 turns")
        else:
            self.blind_since = None
        if lv.mines and ("mines", dl) not in self.branch_seen and not any(k[0] == "mines" for k in self.branch_seen):
            self.branch_seen.add(("mines", dl))
            if CFG["mines"] == "escalate" or CFG["branch_points"]:
                branch_reason = "branch point: entered the Gnomish Mines (Dlvl %d); mines policy is %s" % (
                    dl, self.mines_policy())
        trapdoors = [p for p, t in lv.traps.items() if t in ("trap door", "hole")]
        if trapdoors and ("trapdoor", dl) not in self.branch_seen:
            self.branch_seen.add(("trapdoor", dl))
            branch_reason = branch_reason or "branch point: a %s on Dlvl %d (a free descent)" % (
                lv.traps[trapdoors[0]], dl)
        if len(lv.downs) >= 2 and ("two_downs", dl) not in self.branch_seen:
            self.branch_seen.add(("two_downs", dl))
            branch_reason = branch_reason or "branch point: two down staircases on Dlvl %d" % dl
        if branch_reason and CFG["branch_points"]:
            raise Hard(branch_reason + " (--set branch_points=0 skips these pauses)")

    def situation(self, v, c, acts):
        """Does this moment need the outer loop? Returns (reason or None, the actions, nearby hostiles). Runs the
        crisis ladder's bookkeeping and the throttles; may re-rank the actions."""
        dl, lv, hpf = c["dl"], c["lv"], c["hpf"]
        near = [h for h in c["hostiles"] if h["dist"] <= 3]
        reason = None
        drop = max([hp for t, hp in self.hp_hist if c["turn"] - t <= 5] or [c["hp"]]) - c["hp"]
        adjacent = [h for h in c["hostiles"] if h["dist"] == 1]
        osc = self.oscillation(dl)
        who = tuple(sorted({h["name"] for h in adjacent})) or ("unseen",)
        last_fight = self.fight_noted
        step = max(3, 0.15 * c["hpmax"])
        same_fight = last_fight and last_fight[0] == who and c["turn"] - last_fight[2] < 100 and \
            c["hp"] > last_fight[1] - step       # the same attackers, and HP has not fallen another step
        cr = self.crisis
        if cr and not self.crisis_active(c):
            # the ladder's window is over: done if the bleeding stopped, hand over if HP is still falling
            self.crisis, self.last_crisis = None, dict(cr, ended=c["turn"], hp_end=c["hp"])
            if cr["dl"] == dl and c["hp"] < cr["hp"] - step and (c["hit"] or near):
                reason = "losing fast: HP still falling after the crisis ladder, %d -> %d/%d (%s; tried: %s)" % (
                    cr["hp"], c["hp"], c["hpmax"], cr["why"], ", ".join(cr["tried"]) or "nothing applied")
        elif cr and cr.get("empty"):
            self.crisis, self.last_crisis = None, dict(cr, ended=c["turn"], hp_end=c["hp"])
            reason = "losing fast: the crisis ladder is exhausted at HP %d/%d (%s; tried: %s)" % (
                c["hp"], c["hpmax"], cr["why"], ", ".join(cr["tried"]) or "nothing applied")
        if reason:
            pass
        elif drop >= max(CFG["hp_drop"] * c["hpmax"], CFG["hp_drop_min"]) and not same_fight and \
                self.low_noted != ("drop", c["turn"] // 10):
            self.low_noted = ("drop", c["turn"] // 10)
            self.fight_noted = (who, c["hp"], c["turn"])
            why = "HP %d/%d, down %d in 5 turns (%s)" % (
                c["hp"], c["hpmax"], drop, ", ".join(h["name"] for h in adjacent[:3]) or "unseen attacker")
            if CFG["fight_handoff"] == "ladder" and not self.crisis:
                # keep the fight in the loop: the outer loop is too slow for it; escalate only if the ladder fails
                self.crisis = {"until": c["turn"] + CFG["crisis_turns"], "dl": dl, "hp": c["hp"], "tried": [],
                               "why": why}
                self.note("crisis", "losing fast (%s): running the crisis ladder" % why)
                acts = self.actions(v, c)
            elif not self.crisis:
                reason = "losing fast: " + why
        elif len(adjacent) >= 3 and self.low_noted != ("swarm", len(adjacent), c["hp"] // 5):
            self.low_noted = ("swarm", len(adjacent), c["hp"] // 5)
            reason = "surrounded: %d adjacent hostiles (%s), HP %d/%d" % (
                len(adjacent), ", ".join(h["name"] for h in adjacent[:4]), c["hp"], c["hpmax"])
        elif osc:
            reason = osc
        elif (near or c["hit"]) and hpf < val("hp_escalate") and not any(a.prior >= 6.5 for a in acts):
            if self.low_noted != (dl, c["hp"] // 3):
                self.low_noted = (dl, c["hp"] // 3)
                reason = "low HP %d/%d with %s and no safe prayer, potion or Elbereth (prayer: %s)%s" % (
                    c["hp"], c["hpmax"], (near[0]["name"] + " near") if near else "an unseen attacker",
                    self.prayer_band(c["turn"], c["trouble"]),
                    " (crisis ladder tried: %s)" % (", ".join(self.crisis["tried"]) or "nothing applied")
                    if self.crisis else "")
        elif c["hungry"] in ("Weak", "Fainting") and not any(a.kind in ("eat", "eat_corpse", "pray") for a in acts) \
                and self.hunger_noted != (c["hungry"], c["turn"] // 100):
            self.hunger_noted = (c["hungry"], c["turn"] // 100)    # again at Fainting, or 100 turns later
            reason = "%s from hunger, no food, no safe prayer" % c["hungry"]
        elif c["hungry"] == "Hungry" and self.inv_complete and not self.food_letters() and not c["can_pray"] and \
                self.low_noted != ("hungry", c["turn"] // 300):
            self.low_noted = ("hungry", c["turn"] // 300)
            reason = "Hungry with no food in the pack and prayer not safe yet: plan food (corpses, shops, prayer at T%s)" % (
                (self.last_prayer or 0) + 900)
        elif self.decisions - self.progress > CFG["stall"] or c["turn"] - self.progress_turn > CFG["stall_turns"] \
                or self.clock() - self.progress_time > CFG["stall_secs"]:
            self.progress, self.progress_turn, self.progress_time = self.decisions, c["turn"], self.clock()
            reason = "stalled: no new squares or depth (ladder tried: %s%s)" % (
                ", ".join(sorted(lv.probed)) or "-",
                "; blocked by " + ", ".join("%s %s" % (h["name"], compass(c["hero"], h["pos"])) for h in c["obst"][:3])
                if c["obst"] else "")
        elif acts[0].prior <= -2 and not near and not c["hit"] and not c["frontier"] and \
                self.lv_downs_usable(c) and not self.descend_ok(c) and c["hpf"] >= val("descend_hp"):
            cap, _, how = self.depth_limits(c["xl"], c["hpmax"], c["ac"])
            acts.insert(0, self.linger_act(v, c, cap))
            if self.gate_noted != (dl, c["xl"]):
                self.gate_noted = (dl, c["xl"])
                where = "already %d below the cap; it holds here" % (dl - cap) if dl > cap else \
                    "may not go below Dlvl %d" % cap
                reason = ("depth gate: Dlvl %d is explored and the loop %s at XL %d (%s). It wanders the level for "
                          "experience meanwhile; or play on by hand" % (dl, where, c["xl"], how))
        elif acts[0].prior <= -2 and not near and (c["frontier"] or self.lv_downs_usable(c)):
            if self.decisions < self.move_ban_until:
                # the only thing left is waiting because moves are banned: lift the ban instead of deadlocking
                self.move_ban_until = 0
                self.note("stall", "move ban lifted: waiting was the only option left")
                acts = self.actions(v, c)
            if acts and acts[0].prior <= -2:
                self.note("stall", "blocked; best options: " + ", ".join("%s %.1f" % (a.key, a.prior)
                                                                       for a in acts[:5]))
                acts.insert(0, Act("wait_blocked", "Wait two turns: the way is blocked for now", "2s", "wait", 0))
        elif acts[0].prior <= -2 and not near:
            known = [p for p in lv.downs if p not in c["dist"]]
            if known:
                reason = "level exhausted: down stairs at %s unreachable (no known path: locked door, boulder or " \
                    "blocker%s)" % (", ".join(pos1(p) for p in known[:2]),
                                   "; in the way: " + ", ".join("%s %s" % (h["name"], compass(c["hero"], h["pos"]))
                                                                for h in c["obst"][:3]) if c["obst"] else "")
            else:
                reason = "level exhausted: no frontier, stairs, search budget or tools left (%d search turns%s)" % (
                    lv.search_turns, "; in the way: " + ", ".join("%s %s" % (h["name"], compass(c["hero"], h["pos"]))
                                                                    for h in c["obst"][:3]) if c["obst"] else "")
            if c["obst"]:
                reason += "; options: throw an item at it (t), wait for it to move, another route, dig"
        if reason:
            # An unchanged situation is not news. After a resume, the same kind of escalation waits until the
            # turn counter moves (150 turns for exhausted/stalled verdicts), the hero moves, or the level changes.
            kind = classify(reason)
            last = self.esc_seen.get(kind)
            repeats = self.esc_counts.get((kind, dl), 0)
            base = BY_CODE[kind].window if kind in BY_CODE else 1
            window = base * 2 ** min(repeats, 5) if base > 1 else base
            if last and last[1:] == (dl, c["hero"]) and c["turn"] - last[0] < window:
                if kind == "level_exhausted":
                    acts.insert(0, Act("search_more", "Search 20 turns (this level was already reported exhausted)",
                                       "20s", "search", 0))
                reason = None
            else:
                self.esc_seen[kind] = (c["turn"], dl, c["hero"])
                self.esc_counts[(kind, dl)] = repeats + 1
                if kind == "level_exhausted":
                    lv.extra_budget += CFG["search_budget"]   # then search on
        return reason, acts, near

    def decide_and_act(self, v, c, acts, near):
        """Pick among the legal actions (the rules' order, the decision model on contested steps, plugins),
        escalate if the model or a hook says so, then act."""
        dl, lv, hpf = c["dl"], c["lv"], c["hpf"]
        reason = None
        self.decisions += 1
        if dl not in self.seen_levels:
            self.seen_levels.add(dl)
            self.new_level_pending.add(dl)
        facts = self.facts(v, c, dl in self.new_level_pending)
        top = acts[0]
        calm = not near and not c["hit"] and hpf >= 0.5 and \
            not set(v.cond) & {"Weak", "Fainting", "Conf", "Stun", "Blind", "Hallu"}
        info = {"src": "rule"}
        chosen = top
        contested = not (top.prior >= 6.5 or len(acts) == 1 or (calm and top.prior - acts[1].prior >= 1))
        ans, took, reason, fresh = self.fetch_answers(v, c, acts, near, contested, facts)
        if fresh and "act" in ans:         # a reused answer only stands in for the call, as before
            chosen, info, reason = self.weigh(c, acts, ans, took, reason)
        if not reason:
            try:
                reason, keys = self.hooks.judge(facts, ans)
            except HookError as e:
                reason, keys = str(e), None
            if reason:
                self.hook_answers = {k: x for k, x in ans.items() if k not in ("act", "danger")}
            elif keys:
                self.note("hook_action", repr(keys)[:80], turn=c["turn"])
                self.last_try = None
                self.send(keys)
                return None
        if reason and self.esc(reason, hp=c["hp"], hpmax=c["hpmax"], **info):
            self.pending = (acts, info) if "top" in info else None
            return reason
        self.trail.append((dl, c["hero"], chosen.key))
        self.waits = self.waits + 1 if chosen.kind == "wait" else 0
        if self.waits >= 5:
            self.boost_until = self.decisions + 30     # monsters that never come are scenery
        if chosen.kind == "descend" or (chosen.kind == "travel" and chosen.keys.endswith(">")):
            self.last_down = (dl, c["hero"] if chosen.kind == "descend" else chosen.target)
        self.note("act", "%s: %s" % (chosen.key, chosen.desc), dlvl=dl, hp="%d/%d" % (c["hp"], c["hpmax"]),
                  turn=c["turn"], **info)
        tdist = c["dist"].get(chosen.target) if chosen.target else None
        self.last_try = {"dl": dl, "hero": c["hero"], "turn": c["turn"], "known": len(lv.near), "key": chosen.key,
                         "kind": chosen.kind, "target": chosen.target, "tdist": tdist}
        self.do(v, c, chosen)
        return None
