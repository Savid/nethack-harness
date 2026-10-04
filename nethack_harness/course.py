"""Trips and progress: judging each action's outcome, futility (oscillation) and keeping a trip under way."""
import collections

from . import knowledge as K
from .level import cheb, pos1



class Course:
    def judge_outcome(self, v, c):
        t = self.last_try
        if not t:
            return
        lv, hero = c["lv"], c["hero"]
        if c["dl"] != t["dl"]:
            out = "level"
        elif len(lv.near) > t["known"]:
            out = "new"
        elif c["turn"] == t["turn"] and hero == t["hero"] and self.msg_count == t["msgs"] and \
                self.map_sig(v) == t["map"]:
            out = "noop"        # nothing happened at all: no time, no message, no change on the map
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
            step = K.DIRS.get(t.get("keys", ""))
            if step:
                # a single step the game refused without a word or a turn (a doorway diagonal, a tight squeeze):
                # route around that step for a while
                lv.no_step[(t["hero"], (t["hero"][0] + step[0], t["hero"][1] + step[1]))] = self.decisions + 200
            lv.noops[(t["hero"], t["key"])] += 1
            if lv.noops[(t["hero"], t["key"])] >= 2:
                # Something blocks it for a while (a pet, a peaceful, a held prompt): ban it briefly, never for
                # the level, so a door or a fight is always tried again.
                lv.ban(t["hero"], t["key"], self.decisions + 15)
                lv.noops[(t["hero"], t["key"])] = 0
            self.frozen += 1
        else:
            self.frozen = 0
        if t.get("target") and hero == t["target"]:
            lv.failed.pop(t["target"], None)          # got there: the game's travel may be trusted again
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
            deep, shallow = max(levels[-2:]), min(levels[-2:])
            if not self.fled_from.get(deep):
                self.level(shallow).stair_ban_until = self.decisions + 40
                return "oscillating: Dlvl %d <-> %d %d times; going down is on hold for a while" % (
                    shallow, deep, len(levels))
            # keep every staircase usable for escape; only hold off going back down for a while
            self.level(shallow).stair_ban_until = self.decisions + 40
            campers = self.fled_from.get(deep) or []
            who = ", ".join("%s at %s" % (n, pos1(p)) for n, p in campers) or "something that keeps driving the hero off"
            return ("camped: the Dlvl %d arrival is camped by %s (Dlvl %d <-> %d %d times); the loop explores Dlvl %d "
                    "for a while. Options: --plan goal:stairs then goal:fight:DIR or goal:elbereth there, explore "
                    "here (--plan goal:explore:40), or other stairs") % (
                deep, who, shallow, deep, len(levels), shallow)
        t = [x for x in self.trail if not x[2].startswith(K.STEADY_ACTIONS)][-12:]   # fights, kicks, waits
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

    def keep_course(self, c, acts):
        """Two trips of similar worth (explore here, the stairs there) can win turn about and walk the hero back
        and forth. Once a trip is under way, keep it while nothing threatens and it is still nearly the best."""
        course = self.course
        if course and (course[2] < self.decisions or c["hero"] == course[1] or c["threats"] or c["hit"]):
            course = self.course = None
        if course and acts and acts[0].kind in ("travel", "explore", "wait", "search", "move", "door"):
            keep = next((a for a in acts if (a.key, a.target) == course[:2]), None)
            if keep and keep is not acts[0] and keep.prior >= acts[0].prior - 1.5 and keep.prior > 0:
                acts = [keep] + [a for a in acts if a is not keep]
        top = acts[0] if acts else None
        if top and top.kind in ("travel", "explore") and top.target and (not course or course[:2] != (top.key, top.target)):
            self.course = (top.key, top.target, self.decisions + 12)
        return acts
