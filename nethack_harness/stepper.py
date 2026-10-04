"""One decision: bookkeeping, the model's say, then the action. Outcomes and trips are in course.py, the
escalation checks in pauses.py."""
import time

from . import knowledge as K
from .hooks import HookError
from .settings import CFG
from .base import GAME_OVER, Act, Dead, Hard
from .notes import anchor_of



class Stepper:
    def step(self):
        try:
            return self._step()
        except Hard as e:
            return self.esc(str(e))
        except Dead:
            self.note("game_over", "the game ended during a multi-key action")
            return GAME_OVER

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
        sf = self.swarm_fled
        if sf and c["dl"] < sf[0]:
            # safely up from a swarm: hold off going back down for a while, and say so once
            self.swarm_fled = None
            c["lv"].stair_ban_until = self.decisions + CFG["swarm_hold"]
            raise Hard("swarm: %s, poisonous and fast, on Dlvl %d; left by the up stairs and holding off going back "
                       "down for %d decisions. Options: another way down, or come back stronger" % (
                           sf[1], sf[0], CFG["swarm_hold"]))
        if not self.endgame():
            self.endgame_noted = False
        elif not self.endgame_noted:
            self.endgame_noted = True
            left = int(self.seconds_left() or 0)
            raise Hard("endgame: %d s left: depth caps lifted, stairs at HP 50%% or more, any descent "
                       "preferred (the Mines included)" % max(0, left))
        if self.anchor is None and c["dl"] == 1 and c["turn"] <= 1:
            self.anchor = anchor_of(v)                 # the game's identity for level notes
        if self.last_seen and self.last_seen[0] == c["dl"]:
            c["lv"].turns += max(0, c["turn"] - self.last_seen[1])
        self.last_seen = (c["dl"], c["turn"])
        c["lv"].hazards.update(h["name"] for h in c["obst"] if not h["name"].startswith("likely "))
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
                    cap = self.depth_cap(c["xl"], c["hpmax"], c["ac"])
                    if dl - self.prev_dl >= 2 or (dl > cap and self.prev_dl <= cap):
                        branch_reason = "depth jump: Dlvl %d -> %d at XL %d (depth cap Dlvl %d)" % (
                            self.prev_dl, dl, c["xl"], cap)
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
                friendly = self.race in ("gnomish", "dwarvish")
                branch_reason = ("branch point: entered the Gnomish Mines (Dlvl %d); mines policy is %s; for a %s "
                                 "hero most gnomes, dwarves and hobbits here are %s%s") % (
                    dl, self.mines_policy(), self.race or "?", "peaceful" if friendly else "hostile",
                    "" if self.mines_policy() != "avoid" else "; resume to leave by the up stairs")
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
        acts = self.keep_course(c, acts)
        top = acts[0]
        calm = not near and not c["hit"] and hpf >= 0.5 and \
            not set(v.cond) & {"Weak", "Fainting", "Conf", "Stun", "Blind", "Hallu"}
        info = {"src": "rule"}
        chosen = top
        contested = not (top.prior >= 6.5 or len(acts) == 1 or (calm and top.prior - acts[1].prior >= 1))
        if CFG["fight_question"] and self.crisis_active(c) and len(acts) > 1 and top.kind not in ("pray", "quaff", "cast") \
                and (top.prior - acts[1].prior < 1 or c["incoming"] >= c["hp"]):
            contested = True          # a close call inside the crisis ladder: one fight-or-retreat question
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
                self.key_source = "plugin"
                try:
                    self.send(keys)
                finally:
                    self.key_source = "loop"
                return None
        if reason and self.esc(reason, hp=c["hp"], hpmax=c["hpmax"], **info):
            self.pending = (acts, info) if "top" in info else None
            return reason
        if chosen.prior <= -2 and chosen.key in ("descend", "goto_stairs", "leave_mines"):
            # the stairs are the best of a bad lot but forbidden (the depth cap, HP): never take them anyway
            self.note("stall", "stairs forbidden (%s %.1f): waiting a turn instead" % (chosen.key, chosen.prior))
            chosen = Act("wait_s", "Wait one turn: the stairs are forbidden right now", "ms", "wait", -1)
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
                         "kind": chosen.kind, "target": chosen.target, "tdist": tdist, "msgs": self.msg_count,
                         "map": self.map_sig(v), "keys": chosen.keys}
        self.do(v, c, chosen)
        return None
