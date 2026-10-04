"""When to hand back to the outer loop: milestones, shop-door notes and the escalation checks of each step."""

from . import knowledge as K
from .escalation import BY_CODE, classify
from .level import compass, pos1
from .settings import CFG, val
from .base import Act, Hard



class Pauses:
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

    def door_notes(self, c):
        """Locked doors the loop gave up on, and banned actions here: named, with the keys to try by hand."""
        lv, notes = c["lv"], []
        hint = False
        for q in sorted(lv.locked | (lv.shop_doors & set(lv.terr))):
            if q in lv.shop_doors:
                if lv.terr.get(q) in ("+", "|", "-") or q in lv.locked:
                    notes.append("door at %s: closed shop, do not kick (the shopkeeper inside kills)" % pos1(q))
                continue
            why = "%d kicks failed" % lv.kicks[q] if lv.kicks[q] >= K.KICK_TRIES else \
                "not kicked (watch)" if not self.kickable(q, c) else "%d kicks so far" % lv.kicks[q]
            notes.append("locked door at %s: %s" % (pos1(q), why))
            hint = hint or self.kickable(q, c)
        bans = sorted({key for (pos, key), until in lv.bans.items() if pos == c["hero"] and
                       (until == -1 or until > self.decisions)})
        if bans:
            notes.append("banned here: " + ", ".join(bans))
        if not notes:
            return ""
        return "; " + "; ".join(notes) + (" (kick a locked door by hand: stand beside it, send --hex '04' then the "
                                           "direction; never a closed shop's)" if hint else "")

    def situation(self, v, c, acts):
        """Does this moment need the outer loop? Returns (reason or None, the actions, nearby hostiles). Runs the
        crisis ladder's bookkeeping and the throttles; may re-rank the actions."""
        dl, lv, hpf = c["dl"], c["lv"], c["hpf"]
        near = [h for h in c["hostiles"] if h["dist"] <= 3]
        reason = None
        hunger = self.hunger_reason(c, acts)
        drop = max([hp for t, hp in self.hp_hist if c["turn"] - t <= 5] or [c["hp"]]) - c["hp"]
        adjacent = [h for h in c["hostiles"] if h["dist"] == 1]
        osc = self.oscillation(dl)
        who = tuple(sorted({h["name"] for h in adjacent})) or ("unseen",)
        last_fight = self.fight_noted
        step = max(3, 0.15 * c["hpmax"])
        same_fight = last_fight and last_fight[0] == who and c["turn"] - last_fight[2] < 100 and \
            c["hp"] > last_fight[1] - step       # the same attackers, and HP has not fallen another step
        cr = self.crisis
        holding = c["on_elbereth"] and c["hpf"] < val("rest_hp") and \
            any(h["dist"] <= max(2, 2 * h["speed"] // 12) for h in c["seen_hostiles"])
        if cr and not self.crisis_active(c) and cr["dl"] == dl and holding and cr.get("holds", 0) < 3:
            # still on a verified Elbereth with the threat near: hold the square instead of walking off
            cr["until"], cr["holds"] = c["turn"] + CFG["crisis_turns"], cr.get("holds", 0) + 1
            self.note("crisis", "holding the Elbereth square: %s still near" % ", ".join(
                h["name"] for h in c["seen_hostiles"][:2]))
        elif cr and not self.crisis_active(c) and cr["dl"] == dl and holding:
            self.crisis, self.last_crisis = None, dict(cr, ended=c["turn"], hp_end=c["hp"])
            reason = "camped: held Elbereth at %s for %d turns; %s still near, HP %d/%d. Options: --plan " \
                "goal:fight:DIR, goal:retreat, goal:quaff or goal:pray (prayer: %s)" % (
                    pos1(c["hero"]), 3 * CFG["crisis_turns"], ", ".join(
                        "%s at %s" % (h["name"], pos1(h["pos"])) for h in c["seen_hostiles"][:2]),
                    c["hp"], c["hpmax"], self.prayer_band(c["turn"], c["trouble"]))
        elif cr and not self.crisis_active(c):
            # the ladder's window is over: done if the bleeding stopped, hand over if HP is still falling
            self.crisis, self.last_crisis = None, dict(cr, ended=c["turn"], hp_end=c["hp"])
            if cr["dl"] == dl and c["hp"] < cr["hp"] - step and (c["hit"] or near):
                reason = "losing fast: HP still falling after the crisis ladder, %d -> %d/%d (%s; tried: %s)" % (
                    cr["hp"], c["hp"], c["hpmax"], cr["why"], ", ".join(cr["tried"]) or "nothing applied")
        elif cr and cr.get("empty"):
            self.crisis, self.last_crisis = None, dict(cr, ended=c["turn"], hp_end=c["hp"])
            if (c["hit"] or near) and c["hpf"] < 0.7:     # re-check: a prayer or a kill may have ended it
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
        elif hunger:
            self.hunger_noted, reason = hunger
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
            if lv.gated_since is None:
                lv.gated_since = c["turn"]
            if self.gate_noted != (dl, c["xl"]):
                self.gate_noted = (dl, c["xl"])
                where = "already %d below the cap; it holds here" % (dl - cap) if dl > cap else \
                    "may not go below Dlvl %d" % cap
                below = ", ".join("%s up to %d a turn" % kv for kv in K.dangers_at(cap + 1, c["xl"]))
                reason = ("depth gate: Dlvl %d is explored and the loop %s at XL %d, max HP %d (%s). Below: %s. It "
                          "wanders the level for experience meanwhile; to go down once anyway: --plan goal:stairs" % (
                              dl, where, c["xl"], c["hpmax"], how.split("; lift")[0], below))
        elif acts[0].prior <= -2 and not near and (c["frontier"] or self.lv_downs_usable(c)):
            here = [k for k, until in lv.bans.items() if k[0] == c["hero"] and until > self.decisions]
            if self.decisions < self.move_ban_until or here:
                # the only thing left is waiting because moves or actions here are banned for a while: lift those
                # temporary bans instead of deadlocking (level-long bans stay)
                self.move_ban_until = 0
                for k in here:
                    del lv.bans[k]
                self.note("stall", "bans lifted (%s): waiting was the only option left" % (
                    ", ".join(sorted(k[1] for k in here)) or "moves"))
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
            reason += self.door_notes(c)
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
