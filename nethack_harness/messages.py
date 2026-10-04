"""Reading the game's messages and prompts, and answering them; prayer timing."""
import re

from . import knowledge as K
from .level import Level, cheb, nbrs
from .settings import CFG
from .base import GAME_OVER, Hard


class Messages:
    def message(self, text, v):
        if not text:
            return None
        dl = v.st.get("dlvl", 0)
        # Until the step loop has worked out the branch of a new level, keep its facts off the remembered maps.
        lv = self.level(dl) if self.resolved(dl) else Level(dl)
        if K.HIT.search(text) or re.search(r"engulfs you|swallows you|can barely breathe|You are pummeled|"
                                           r"You are laden|You are blasted", text):
            self.hit_turn = v.st.get("turn", 0)
        if K.RANGED_HIT.search(text):
            self.ranged_until = v.st.get("turn", 0) + 10
        if re.search(r"engulfs you|swallows you|You are engulfed", text):
            self.engulfed = v.st.get("turn", 0) or 1      # remembered even when blindness hides the box
        if re.search(r"You get (?:expelled|regurgitated)|You are released|regurgitates you|expels you|"
                     r"You (?:destroy|kill) ", text):
            self.engulfed = False
        self.msgs.append(text)
        self.msg_log.append((v.st.get("turn"), text))
        self.msg_count += 1
        if K.PROGRESS.search(text):
            # a kill, a hit, a door giving way: a fight or a door in progress is not a stall
            self.progress, self.progress_turn, self.progress_time = self.decisions, v.st.get("turn", 0), self.clock()
            self.trail.clear()
        self.note("msg", text[:200])
        if "You begin praying" in text and v.st.get("turn") is not None:
            self.last_prayer = v.st["turn"]
        if K.PRAY_BAD.search(text):
            self.prayer_broken = True
            self.note("prayer", "prayer failed: no more prayers this game")
        self.food_message(text, v)
        for what in re.findall(r"(?:That|The) ([a-z ]+?) is an? [a-z ]*mimic!|Wait! That's an? [a-z ]*mimic!", text):
            lv.disguises.add(what.split()[-1] if what else "object")   # e.g. "boulder": the others may be too
        if self.role == "Knight":
            # remember monsters that flee: a Knight attacking one loses alignment ("You caitiff!")
            hero, turn = v.hero, v.st.get("turn", 0)
            for name in re.findall(r"(?:The |An? )?([a-z][a-z -]*?) (?:and then )?turns to flee", text):
                near = sorted((h for h in self.hostiles if h["name"] == name), key=lambda h: cheb(h["pos"], hero))
                if near:
                    self.fleeing.append({"name": name, "pos": near[0]["pos"], "turn": turn})
            if "You caitiff" in text and self.last_act and self.last_act.target:
                for h in self.hostiles:
                    if h["pos"] == self.last_act.target:
                        self.fleeing.append({"name": h["name"], "pos": h["pos"], "turn": turn})
        if re.search(r"This door is broken|This door is already open|This door's already open|"
                     r"You see no door there|There is no door here", text) and self.last_act and self.last_act.target:
            q = self.last_act.target           # remembered as a closed door, but it is not one any more
            lv.terr[q], lv.tfg[q] = ".", "default"
            lv.locked.discard(q)
        if "This door is locked" in text and self.last_act and self.last_act.kind == "door":
            lv.locked.add(self.last_act.target)
        if re.search(r"The door opens|crashes open|shatters to pieces|You break open the lock|"
                     r"The door unlocks|You succeed in (?:unlocking|forcing)", text):
            # a way opened: forget exclusions and bans earned while it was shut
            if self.last_act and self.last_act.target:
                lv.locked.discard(self.last_act.target)
            lv.excluded.clear()
            lv.bans = {k: u for k, u in lv.bans.items() if u == -1}     # keep level-long bans
            lv.failed.clear()
            self.move_ban_until = 0
        if re.search(r"WHAMM|leg is in no shape", text) and self.last_act and self.last_act.kind == "kick":
            if "no shape" in text:
                lv.no_kick = True
        if re.search(r"[Cc]losed for inventory", text) and v.hero:
            self.mark_closed_shop(lv, v.hero)          # the engraving lies before a closed shop's door
        text = re.sub(r'You read: ".*?"|"[^"]*"', "", text)    # engravings and epitaphs are not events
        if K.SHOP_SOUND.search(text):
            lv.has_shop = True                         # a level-wide sound: says nothing about where we stand
        if K.SHOP.search(text):
            lv.shop = lv.has_shop = lv.no_dig = True
            if v.hero:                                 # greeted in the doorway: never kick around here
                lv.shop_doors.update([v.hero] + [q for _, q in nbrs(v.hero)])
        if K.BOULDER_FAIL.search(text) and self.last_try and self.last_try["kind"] == "push":
            lv.ban(self.last_try["hero"], self.last_try["key"])
        if K.BOULDER_BUSY.search(text) and self.last_try and self.last_try["kind"] == "push":
            lv.ban(self.last_try["hero"], self.last_try["key"], self.decisions + 10)
        m = K.SWAP_REFUSED.search(text)
        if m and self.last_try and self.last_try.get("target"):
            lv.cost[self.last_try["target"]] += 10
        if re.search(r"(?:stairs|ladder|throne|altar|fountain) (?:is|are) too hard to dig", text) and \
                v.hero is not None:
            lv.ban(v.hero, "dig")                       # this square only
        elif re.search(r"too hard to dig|cannot (?:dig|stay)|can't dig|too heavy to apply|while wearing a shield",
                       text):
            lv.no_dig = True
        m = re.search(r"You see here (?:an? |\d+ )?([^.]+)\.", text)
        if m and CFG["pickup_food"] and K.food_index(m.group(1)) is not None and "corpse" not in m.group(1) \
                and not lv.shop and "keys:," not in self.plan:
            self.plan.appendleft("keys:,")
            self.inv_turn = -2
        if K.STONING.search(text):
            if self.prayer_safe(v.st.get("turn", 0)):
                self.plan.appendleft("goal:pray")
                return None
            raise Hard("stoning (%s) and prayer is not safe: act now (eat a lizard or acidic corpse, or pray anyway)"
                       % text[:80])
        if K.ALARM.search(text):
            hint = next((h for rx, h in K.ALARM_HINTS if re.search(rx, text)), "")
            return self.esc("alarming message: " + text[:160] + (" (%s; prayer: %s)" % (
                hint, self.prayer_band(v.st.get("turn", 0))) if hint else ""))
        return None

    def mark_closed_shop(self, lv, spot):
        """'Closed for inventory' is engraved in front of a closed shop's door: every door beside that square is
        a shop door (kicking it in makes the shopkeeper inside kill the hero)."""
        lv.closed_shop_spots.add(spot)
        lv.shop_doors.update(q for _, q in nbrs(spot))
        lv.has_shop = True
        self.note("shop", "closed shop: the doors beside %d,%d are never kicked" % (spot[0] + 1, spot[1] + 1))

    def answer(self, v):
        """Answer a yes/no prompt from the fixed table; the model never answers prompts."""
        ans = K.prompt_answer(v.msg)
        if ans == "GAME_OVER":
            return GAME_OVER
        if ans == "ESCALATE":
            raise Hard("prompt needs you: " + v.msg[:160])
        if ans is None:
            turn = v.st.get("turn", 0) or 0
            seen = self.unknown_prompts.get(v.msg)
            self.unknown_prompts[v.msg] = turn
            self.note("prompt", v.msg[:100], answer="ESC (unknown)")
            self.send("\x1b")
            if seen is not None and turn - seen <= 50:
                raise Hard("unknown prompt seen twice: " + v.msg[:160])
            return None
        keys = {"ESC": "\x1b", "PRAY": "y" if self.praying else "n"}.get(ans, ans)
        if ans == "CORPSE":
            keys = self.floor_food_answer(v.msg)
        elif ans == "TIN":
            keys = self.tin_answer(v.msg)
        self.note("prompt", v.msg[:100], answer=keys)
        self.send(keys)
        return None

    def interrupts(self, v):
        if v.dead:
            return GAME_OVER
        if v.more:
            text = " ".join(r.strip() for r in v.rows[:3] if r.strip()).replace("--More--", "").strip()
            reason = self.message(text, v)
            self.send(" ")
            return reason
        if v.yn:
            return self.answer(v)
        if v.text and "write in the" in v.msg:
            self.send("Elbereth\r")
            return None
        if v.text and re.search(r"who are you\?|wish|genocide", v.msg):
            raise Hard("text prompt needs you: " + v.msg[:160])
        if v.menu or v.getpos or v.text or v.direction or v.obj is not None:
            self.note("cancel", v.msg[:100])
            self.send("\x1b")
            return None
        if v.msg and v.normal and (not self.msgs or self.msgs[-1] != v.msg):
            return self.message(v.msg, v)
        return None

    def flow(self, keys, until=8):
        """Send keys, then answer what follows from the table until a normal screen returns."""
        self.send(keys)
        for _ in range(until):
            w = self.term.view()
            if w.normal or w.dead:
                return w
            if w.more:
                self.message(" ".join(r.strip() for r in w.rows[:2]).replace("--More--", "").strip(), w)
                self.send(" ")
            elif w.yn:
                if self.answer(w) == GAME_OVER:
                    return w
            elif w.text and "write in the" in w.msg:
                self.send("Elbereth\r")
            else:
                self.send("\x1b")
        return self.term.view()

    def prayer_band(self, turn, trouble=None):
        """Prayer in bands. After a prayer the timeout is random (median about 350) and a prayer in major trouble
        works once it is under 200, so: fails (<200 turns since), uncertain (200-499), likely in major trouble
        (500-899), safe (the loop's own rule, 900+). Prayer only fixes major trouble: HP below 1/7 of max or 5
        or less, Weak from hunger, and the like."""
        if self.prayer_broken:
            return "broken (a prayer failed; never pray again)"
        if self.prayer_safe(turn):
            band = "safe"
        elif self.last_prayer is None:
            band = "not yet (too early in the game; safe from about T110)"
        else:
            ago = turn - self.last_prayer
            band = ("fails (last T%d, %d ago)" if ago < 200 else
                    "uncertain (last T%d, %d ago; the timeout is random after a prayer)" if ago < 500 else
                    "likely in major trouble (last T%d, %d ago; certain from about T%d)" % (
                        self.last_prayer, ago, self.last_prayer + 900) if ago < 900 else "safe")
            if "%d" in band:
                band = band % (self.last_prayer, ago)
        if trouble is False and not band.startswith(("fails", "broken")):
            band += "; but it fixes HP only below 1/7 of max or at 5 or less"
        return band

    def prayer_safe(self, turn):
        if self.prayer_broken:
            return False
        return turn >= 110 if self.last_prayer is None else turn - self.last_prayer >= 900
