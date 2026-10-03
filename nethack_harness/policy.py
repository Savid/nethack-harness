"""The inner loop. Rules own safety and mechanics; the decision model judges contested steps; anything tricky is
escalated to the outer loop. step() plays one decision and returns an escalation reason or None."""
import collections
import random
import re
import time

from . import knowledge as K
from .decide import Unhealthy, builtin_questions, confidence, normalize
from .hooks import HOOK_API, HookError, Hooks
from .level import Level, cheb, compass, door, nbrs, on_map, passable, travel
from .settings import CFG, effort, val

GAME_OVER = "game_over"
RACE_MONSTER = {"dwarvish": "dwarf", "gnomish": "gnome", "elven": "elf", "orcish": "orc", "human": "human"}


class Act:
    def __init__(self, key, desc, keys, kind, prior=0.0, target=None):
        self.key, self.desc, self.keys, self.kind, self.prior, self.target = key, desc, keys, kind, prior, target
        self.door = None           # goto_door: the locked door this trip is for

    def __repr__(self):
        return "Act(%s %.1f)" % (self.key, self.prior)


class Hard(Exception):
    """An escalation that budgets and calm windows never suppress."""


class Pilot:
    def __init__(self, term, decide):
        self.term, self.decide = term, decide
        self.paused_total, self.paused_at = 0.0, None     # the play clock stops while the outer loop has it
        self.lv = {}
        self.branch, self.branch_dl = "main", None          # which branch the current level is in (track_branch)
        self.species = {}                 # (dlvl, symbol, colour, bright) -> farlook name
        self.inv, self.inv_turn = {}, -1  # letter -> (text, section)
        self.role = self.race = self.align = None
        self.options = self.briefed = False
        self.last_prayer, self.prayer_broken, self.praying, self.eating_corpse = None, False, False, False
        self.food_off_until, self.eat_fail = -1, 0
        self.hist, self.msgs = collections.deque(maxlen=80), collections.deque(maxlen=12)
        self.keys = self.decisions = self.calls = self.escs = 0
        self.mtime, self.t0, self.mark, self.progress, self.progress_turn = 0.0, time.time(), None, 0, 0
        self.directive, self.max_dl, self.prev_dl, self.mines_noted = "", 0, None, False
        self.obst, self.hostiles, self.pending, self.last_act, self.last_try = [], [], None, None, None
        self.corpse, self.low_noted, self.turn, self.bad_screens, self.hit_turn = None, None, None, 0, -99
        self.calm_until, self.hook_answers, self.seen_levels = 0, {}, set()
        self.visits, self.outcomes = collections.deque(maxlen=10), collections.deque(maxlen=10)
        self.move_ban_until = self.boost_until = self.frozen = self.engulf_sends = 0
        self.unknown_prompts, self.elbereth_at, self.blind_since = {}, None, None
        self.last_model_esc, self.last_new_level = -1e9, self.clock()
        self.plan = collections.deque()
        self.cache, self.reused = None, 0
        self.latencies, self.engulfed, self.esc_seen = collections.deque(maxlen=5), False, {}
        self.esc_counts, self.last_model_error, self.fight_noted = {}, "", None
        self.door_plan = None              # (dlvl, door, approach square, give up after this decision)
        self.crisis = None                 # the in-loop fight ladder: {"until", "dl", "hp", "tried", "why"}
        self.fight_plan = None
        self.ms_dl = self.ms_xl = None     # milestone counters (deepest Dlvl and XL already reported)
        self.milestones = []             # goal:fight bookkeeping: (dlvl, HP at start, adjacent hostiles)
        self.ranged_until = -1             # a ranged attack hit or missed us recently: leave its line
        self.elbereth_failed = None        # (dlvl, pos, turn): Elbereth did not hold here
        self.gate_noted = None
        self.overviewed, self.mines_entry, self.branch, self.branch_dl = set(), None, "main", None
        self.breaker_until, self.breaker_trips, self.new_level_pending = 0.0, 0, set()
        self.hp_hist = collections.deque(maxlen=12)        # (turn, hp)
        self.trail = collections.deque(maxlen=24)          # (dlvl, hero, action key) per decision
        self.level_trail = collections.deque(maxlen=12)    # dlvl per level change
        self.edges = {}                                    # (dlvl, pos) of a down staircase -> where it led
        self.last_down = None                              # (dlvl, pos) we last went down from
        self.branch_seen, self.waits, self.disagree = set(), 0, 0
        self.overrides = self.disagreements = 0
        self.progress_time, self.inbox_lines = self.clock(), []
        self.ladder = ""                  # last escape-ladder step taken on this level
        self.rng = random.Random(0)
        self.log = None
        self.hooks = Hooks()

    # ------------------------------------------------------------ plumbing
    def level(self, dl, branch=None):
        """Levels are remembered per branch: Mines level 5 and Dlvl 5 of the main dungeon are different maps."""
        branch = branch or self.branch
        k = dl if branch == "main" else (branch, dl)
        if k not in self.lv:
            self.lv[k] = Level(dl)
            self.lv[k].mines = branch == "mines"
            self.lv[k].arrived = self.clock()
        return self.lv[k]

    def resolved(self, dl):
        """True once the branch of the level we are on is known (see track_branch)."""
        return self.branch_dl in (None, dl)

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

    def track_branch(self, dl, v):
        """On a level change, work out which branch we are in before anything reads the level's map."""
        prev_dl, prev_branch = self.branch_dl, self.branch
        self.branch_dl = dl
        if prev_dl is None or dl == prev_dl:
            return
        prev = self.level(prev_dl)
        if dl > prev_dl and self.last_down and self.last_down[0] == prev_dl and \
                prev.downs.get(self.last_down[1]) == "branch" and prev_branch == "main":
            self.branch = "mines"
        elif dl < prev_dl and prev_branch == "mines" and self.mines_entry is not None and dl <= self.mines_entry:
            self.branch = "main"
        if 3 <= dl <= 14 or prev_branch == "mines":
            here = self.overview(dl)
            if here:
                self.branch = here
        if self.branch == "mines" and prev_branch == "main" and dl > prev_dl and self.last_down and \
                self.last_down[0] == prev_dl:
            prev.downs[self.last_down[1]] = "branch"         # the staircase we just took leads into the Mines
            self.mines_entry = prev_dl
        if self.branch == "main" and prev_branch == "mines" and dl < prev_dl:
            hero = v.hero
            if hero:
                self.level(dl).downs[hero] = "branch"         # we climbed out: the > underfoot leads back in
            self.mines_entry = dl

    def view(self):
        v = self.term.view()
        if v.normal and not v.engulfed and self.resolved(v.st.get("dlvl", 0)):
            self.level(v.st.get("dlvl", 0)).observe(v)
        return v

    def note(self, kind, text, **kw):
        kw.update(step=self.keys, kind=kind, text=text, t=round(time.time() - self.t0, 1))
        self.hist.append(kw)
        if self.log:
            try:
                self.log.write(__import__("json").dumps(kw) + "\n")
                self.log.flush()
            except (OSError, ValueError):
                pass              # a full disk must not stop play

    def clock(self):
        """Seconds of play: wall time minus the time spent paused, so a long think by the outer loop never looks
        like a stall, lifts a depth cap or spends an escalation budget."""
        now = time.time()
        return now - self.paused_total - ((now - self.paused_at) if self.paused_at is not None else 0.0)

    def stop_clock(self):
        if self.paused_at is None:
            self.paused_at = time.time()

    def start_clock(self):
        if self.paused_at is not None:
            self.paused_total += time.time() - self.paused_at
            self.paused_at = None

    def send(self, keys):
        self.term.send(keys)
        self.keys += 1

    def esc(self, reason, **kw):
        self.note("escalate", reason, turn=self.turn, **kw)
        return None if CFG.get("auto") else reason   # auto: benchmarks log escalations and play on

    def prayer_safe(self, turn):
        if self.prayer_broken:
            return False
        return turn >= 110 if self.last_prayer is None else turn - self.last_prayer >= 900

    # ------------------------------------------------------------ messages and prompts
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
        self.note("msg", text[:200])
        if "You begin praying" in text and v.st.get("turn") is not None:
            self.last_prayer = v.st["turn"]
        if K.PRAY_BAD.search(text):
            self.prayer_broken = True
            self.note("prayer", "prayer failed: no more prayers this game")
        m = re.search(r"You (?:kill|destroy) (?:the |an? )?([a-z -]+?)!", text)
        if m and self.last_act and self.last_act.kind == "attack":
            self.corpse = (dl, self.last_act.target, m.group(1), v.st.get("turn", 0))
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
            lv.shop_doors.update(q for _, q in nbrs(v.hero))       # the engraving lies before a shop door
        text = re.sub(r'You read: ".*?"|"[^"]*"', "", text)    # engravings and epitaphs are not events
        if K.SHOP.search(text):
            lv.shop = lv.no_dig = True
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
        if m and CFG["pickup_food"] and any(f in m.group(1) for f in K.FOODS) and "corpse" not in m.group(1) \
                and not lv.shop and "keys:," not in self.plan:
            self.plan.appendleft("keys:,")
            self.inv_turn = -2
        if "You don't have anything to eat" in text:
            self.food_off_until = v.st.get("turn", 0) + 300
        if K.STONING.search(text):
            if self.prayer_safe(v.st.get("turn", 0)):
                self.plan.appendleft("goal:pray")
                return None
            raise Hard("stoning (%s) and prayer is not safe: act now (eat a lizard or acidic corpse, or pray anyway)"
                       % text[:80])
        if "You feel feverish" in text and self.prayer_safe(v.st.get("turn", 0)) and "goal:pray" not in self.plan:
            self.plan.appendleft("goal:pray")    # lycanthropy is major trouble: a safe prayer cures it
            self.note("lycanthropy", "feverish: praying while the timeout is safe")
            return None
        if K.ALARM.search(text):
            return self.esc("alarming message: " + text[:160])
        return None

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
        keys = {"ESC": "\x1b", "PRAY": "y" if self.praying else "n",
                "CORPSE": "y" if self.eating_corpse else "n"}.get(ans, ans)
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

    # ------------------------------------------------------------ one-time setup and knowledge
    def setup(self):
        """hilite_pet (pets in reverse video), time (turn counter), autoopen; no sparkle or timed_delay."""
        want = {"hilite_pet": True, "time": True, "sparkle": False, "timed_delay": False, "autoopen": True}

        def toggle(rows):
            for line in rows:
                m = re.match(r"\s*([a-zA-Z]) - (\S+)\s+\[(.)\]", line)
                if m and m.group(2) in want and (m.group(3) == "X") != want[m.group(2)]:
                    return m.group(1)
            return None

        self.send("O")
        for _ in range(12):
            rows = self.term.view().rows
            k = toggle(rows)
            if not k and re.search(r"\(1 of \d+\)", "\n".join(rows)):
                self.send(">")
                k = toggle(self.term.view().rows)
            if not k:
                break
            self.send(k)
        for _ in range(4):
            if self.term.view().normal:
                break
            self.send("\x1b")
        self.options = True
        self.note("setup", "options: hilite_pet, time, autoopen on; sparkle, timed_delay off")

    def read_pages(self, keys):
        """Send keys that open a text window or menu; return all its lines over every page."""
        self.send(keys)
        lines = []
        for _ in range(6):
            w = self.term.view()
            lines += [r.rstrip() for r in w.rows]
            if any(re.search(r"\((\d+) of (\d+)\)", r) and not re.search(r"\((\d+) of \1\)", r) for r in w.rows):
                self.send(">" if keys == "i" else " ")
                continue
            break
        for _ in range(3):
            if self.term.view().normal:
                break
            self.send("\x1b")
        return lines

    def overview(self, dl):
        """Read the dungeon overview (^O): is this level in the Gnomish Mines, and where does the branch start?"""
        section, here, last_level = None, None, None
        for line in self.read_pages("\x0f"):
            if re.search(r"The Gnomish Mines\b", line) and "Stairs" not in line:
                section = "mines"
            elif re.search(r"The Dungeons of Doom|Gehennom|Sokoban|Fort Ludios|The Quest|Vlad's Tower", line) \
                    and "Stairs" not in line:
                section = "other"
            m = re.search(r"Level (\d+):", line)
            if m and "You are here" in line:
                here = {"mines": "mines", "other": "main"}.get(section)
            if m:
                last_level = int(m.group(1))
            if "Stairs down to The Gnomish Mines" in line and section == "other":
                self.mines_entry = last_level
        self.overviewed.add(dl)
        return here

    def learn_character(self):
        for line in self.read_pages("\x18"):
            m = K.ATTRIBUTES.search(line)
            if m:
                race = m.group(2)
                self.race = K.RACE_WORDS.get(race, race)
                self.role = K.ROLE_NAMES.get(m.group(3), m.group(3))
            m = K.ALIGNMENT.search(line)
            if m and not self.align:
                self.align = m.group(1)
        if not self.role:
            title = (self.term.view().title.split(" the ") + [""])[1].split()
            self.role = K.ROLE_TITLES.get(title[0] if title else "", None)

    def read_inventory(self):
        """Re-read the pack. A read that did not show the whole inventory menu (every page, or an (end)
        marker) only adds to what is known, so a swallowed key or a missed page never empties the pack."""
        inv, sec, pages, total, ended = {}, "", set(), None, False
        lines = self.read_pages("i")
        for line in lines:
            m = re.search(r"\((\d+) of (\d+)\)\s*$", line)
            if m:
                pages.add(int(m.group(1)))
                total = int(m.group(2))
            if re.search(r"\(end\)\s*$", line):
                ended = True
            m = re.match(r"\s*([a-zA-Z$]) - (.+?)\s*$", line)
            if m and len(m.group(2)) > 2:
                inv.setdefault(m.group(1), (m.group(2), sec))
            elif line.strip() in ("Weapons", "Armor", "Comestibles", "Scrolls", "Spellbooks", "Potions", "Rings",
                                  "Wands", "Tools", "Amulets", "Gems/Stones", "Coins", "Boulders/Statues"):
                sec = line.strip()
        complete = inv and (ended or (total is not None and pages >= set(range(1, total + 1))))
        if complete or not self.inv:
            self.inv = inv
        else:
            self.inv = dict(self.inv, **inv)        # partial read: keep what we knew
        self.inv_turn = self.turn or 0
        self.note("inventory", "%s%s" % ("" if complete else "(partial read) ", "; ".join(
            "%s - %s" % (k, t) for k, (t, _) in sorted(self.inv.items()))[:700]))

    def items(self, rx=None, section=None):
        return [(k, t) for k, (t, s) in sorted(self.inv.items())
                if (rx is None or re.search(rx, t)) and (section is None or s == section)]

    def kit(self):
        food = [t for k, t in self.items(section="Comestibles")]
        return {"food": food, "healing": [t for _, t in self.items(K.HEALING.pattern)],
                "dig": [t for _, t in self.items("|".join(K.DIG_TOOLS))],
                "mapping": [t for _, t in self.items(K.MAPPING.pattern)],
                "wands": [t for _, t in self.items(section="Wands")],
                "ranged": [t for _, t in self.items(r"dagger|dart|arrow|\bya\b|yumi|bow|shuriken|spear|knife|boomerang|sling")],
                "spellbooks": [t for _, t in self.items(section="Spellbooks")]}

    @staticmethod
    def min_range(h):
        """Closest distance at which to kill this monster: exploders must die at least 2 squares away."""
        return 2 if K.EXPLODERS.search(h["name"]) else 1

    def spare_missile(self):
        """(letter, name) of something safe to throw: missiles and rocks first, then plain fruit."""
        for rx in (K.SPARE_MISSILES, K.SPARE_FOOD):
            for k, t in self.items(rx):
                if not K.item_state(t) and not re.search(r"\bcursed|loadstone", t):
                    return k, t
        return None

    def mines_policy(self):
        m = CFG["mines"]
        if m == "auto":
            return "allow" if self.race in ("dwarvish", "gnomish") else "avoid"
        return m

    def lead(self):
        if CFG["lead"] is not None:
            return int(CFG["lead"])
        return K.ROLE_LEAD.get(self.role, 3) + val("lead")

    def farlook(self, v, p, cache=True):
        ch = v.ch(*p)
        base, bright = v.col(*p)
        key = (v.st.get("dlvl"), ch, base, bright) if ch not in K.AMBIGUOUS and cache else \
            (v.st.get("dlvl"), p, ch, base)
        if key not in self.species:
            self.term.send(travel(v.hero, p, ";"))
            text = self.term.view().msg
            for _ in range(3):
                w = self.term.view()
                if w.more:
                    self.term.send(" ")
                elif w.menu or w.getpos:
                    self.term.send("\x1b")
                else:
                    break
            m = re.findall(r"\(([^()]*)\)", text)
            self.species[key] = (m[-1] if m else re.sub(r"^\S\s+", "", text)).strip()[:60] or "unknown"
        return self.species[key]

    # ------------------------------------------------------------ the decision context
    def context(self, v):
        st, hero = v.st, v.hero
        dl = st.get("dlvl", 0)
        lv = self.level(dl)
        lv.now = self.decisions
        hp, hpmax, xl, turn = st.get("hp", 1), max(1, st.get("hpmax", 1)), st.get("xl", 1), st.get("turn", 0)
        c = {"dl": dl, "lv": lv, "hero": hero, "hp": hp, "hpmax": hpmax, "hpf": hp / hpmax, "turn": turn, "xl": xl,
             "ac": st.get("ac", 10),
             "hallu": "Hallu" in v.cond, "blind": "Blind" in v.cond}
        hostile, peace, obst = [], [], []
        watch = False
        for r in range(1, 22):
            for col in range(80):
                p, ch = (r, col), v.rows[r][col]
                if p == hero or not (ch in K.MON or ch in K.WARNING) or v.pet(r, col) or p in lv.statues:
                    continue
                d = cheb(p, hero)
                base, bright = v.col(r, col)
                name = None
                if ch in K.WARNING:
                    name = "unseen monster (warning %s)" % ch
                elif c["hallu"]:
                    name = "hallucinated monster"
                elif d <= 5 and ch != "~":
                    name = self.farlook(v, p)
                    if K.NOT_A_MONSTER.search(name):
                        # farlook named terrain or nothing on a monster glyph: never melee it on that word;
                        # forget the answer so the next look can confirm what it is
                        self.species = {k: x for k, x in self.species.items() if x != name}
                        name = "unidentified %s (farlook said %r)" % (ch, name[:30])
                    if "tame" in name:
                        continue
                    if "statue" in name:
                        lv.statues.add(p)
                        continue
                name = name or "unseen %s%s %s" % ("bright " if bright else "", base, ch)
                if "watchman" in name or "watch captain" in name:
                    watch = True
                m = {"pos": p, "ch": ch, "base": base, "bright": bright, "dist": d, "name": name}
                never = None if c["hallu"] else K.never_melee(ch, base, bright, name)
                if name.startswith("unidentified ") and d <= 5:
                    never = "unconfirmed"
                m["never"] = never
                m["avoid"] = bool(CFG["avoid"] and re.search(CFG["avoid"], name)) or \
                    (not c["hallu"] and K.keep_away(name, self.race, self.role))
                m["threat"] = 99 if m["avoid"] else 0 if c["hallu"] else K.threat_xl(ch, base, bright, name)
                (obst if never else peace if "peaceful" in name else hostile).append(m)
        for p, desc in list(lv.traps.items()):   # identify nearby traps once: trap doors are free descents
            if desc is None and v.ch(*p) == "^" and cheb(p, hero) <= 7 and not c["hallu"]:
                name = self.farlook(v, p, cache=False)
                lv.traps[p] = next((t for t in ("trap door", "hole", "level teleporter", "magic portal")
                                    if t in name), name[:30] or "trap")
                if lv.traps[p] in ("level teleporter", "magic portal"):
                    lv.cost[p] += 60
        hostile.sort(key=lambda h: h["dist"])
        if self.decisions < self.boost_until:   # oscillating: treat monsters that keep their distance as scenery
            hostile = [h for h in hostile if h["dist"] <= 1]
        self.hostiles, self.obst = hostile, obst
        c.update(hostiles=hostile, peace=peace, obst=obst, watch=watch)
        lv.blocked = {h["pos"] for h in obst} | lv.statues
        c["threats"] = [h for h in hostile if h["dist"] <= 2]
        c["dist"] = lv.paths(v, hero)
        c["under"] = lv.terr.get(hero, "?")
        c["frontier"] = lv.frontier(v, c["dist"])
        div = 5 if xl <= 5 else 6 if xl <= 13 else 7    # the game's own low-HP rule for prayer
        c["trouble"] = hp <= 5 or hp * div <= min(hpmax, 15 * xl) or any(x in v.cond for x in K.MAJOR)
        c["can_pray"] = self.prayer_safe(turn)
        c["hungry"] = next((x for x in v.cond if x in ("Hungry", "Weak", "Fainting", "Fainted")), None)
        c["hit"] = turn - self.hit_turn <= 2
        e = self.elbereth_at
        if e is not None and e[:2] == (dl, hero) and self.hit_turn > e[2]:
            # hit while standing on it: it is not holding (misspelt, scuffed, or the attacker ignores it)
            self.elbereth_failed, self.elbereth_at = (dl, hero, turn), None
            self.note("elbereth", "hit while standing on Elbereth at T%d: no longer trusted here" % self.hit_turn)
            e = None
        c["on_elbereth"] = e is not None and e[:2] == (dl, hero) and turn - e[2] <= 50
        c["ranged"] = turn <= self.ranged_until
        return c

    def fragile(self, hpmax, ac):
        return hpmax < CFG["sturdy_hp"] or ac > CFG["sturdy_ac"]

    def depth_cap(self, xl, hpmax, ac):
        """The deepest Dlvl the loop may descend to on its own: XL + lead, or XL + fragile_lead for a fragile
        hero (max HP below sturdy_hp or AC worse than sturdy_ac), whichever is shallower."""
        cap = xl + self.lead()
        if self.fragile(hpmax, ac):
            cap = min(cap, xl + int(CFG["fragile_lead"]) + (1 if CFG["risk"] == "high" else 0))
        return cap

    def descend_ok(self, c):
        lv = c["lv"]
        cap = self.depth_cap(c["xl"], c["hpmax"], c.get("ac", 10))
        lift = self.clock() - (lv.arrived or self.clock()) > CFG["cap_lift"] and \
            (c["xl"] >= 3 or not self.fragile(c["hpmax"], c.get("ac", 10)))
        return (c["dl"] < cap or lift) and c["hpf"] >= val("descend_hp")

    # ------------------------------------------------------------ legal actions
    def actions(self, v, c):
        hero, lv, dist, hpf, th, hs = c["hero"], c["lv"], c["dist"], c["hpf"], c["threats"], c["hostiles"]
        acts, hurt, dl, fr, turn = [], hpf < 0.5, c["dl"], c["frontier"], c["turn"]
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
                    acts.append(Act("open_" + k, "Open the closed door to the " + K.DN[k], k, "door",
                                    6 if blocked else 3 if new else 1 if fr else 2.4, q))
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
        quiver = [(k, t) for k, t in self.items() if "quivered" in K.item_state(t)]
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
            escape = th and hpf >= 0.4
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
        if fr:
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
        if self.directive and self.decisions < self.calm_until:
            pass
        return sorted(acts, key=lambda a: -a.prior) or [Act("search", "Search here 15 turns", "15s", "search", -3)]

    def crisis_active(self, c):
        cr = self.crisis
        return bool(cr) and cr["dl"] == c["dl"] and c["turn"] <= cr["until"]

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
                acts[i] = Act("open_" + k, "%s: open the closed door %s on the way" % (a.desc, K.DN[k]), k, "door",
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
        budget_left = lv.search_turns < CFG["search_budget"] + extra and lv.search_actions < 25 + extra // 6
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
            if sec == "Comestibles" or any(f in t for f in K.FOODS):
                out.append((next((i for i, f in enumerate(K.FOODS) if f in t), 50), t, k))
        return [(k, t) for _, t, k in sorted(out)]

    def emergency_actions(self, v, c, acts):
        hpf, th = c["hpf"], c["threats"]
        if c["can_pray"] and c["trouble"]:
            acts.append(Act("pray", "Pray (in serious trouble and the prayer timeout looks safe)", "", "pray", 9))
        low = hpf < 1 / 3 or c["hp"] < 8
        heal = self.items(K.HEALING.pattern)
        if low and CFG["potions"] and heal and (th or c["hit"] or hpf < 0.2):
            acts.append(Act("quaff", "Quaff the %s" % heal[0][1], "q" + heal[0][0], "quaff", 8.5))
        if CFG["spells"] and self.role == "Healer" and hpf < 0.5 and v.st.get("pw", 0) >= 5:
            acts.append(Act("cast_heal", "Cast healing on yourself", "Za.", "cast", 7 if th else 4))
        adjacent_threat = [h for h in th if h["dist"] == 1 and (hpf < val("elbereth_hp") or c["xl"] < h["threat"])]
        if CFG["elbereth"] and adjacent_threat and self.elbereth_useful(v, c, adjacent_threat):
            acts.append(Act("elbereth", "Engrave Elbereth in the dust to scare monsters away", "", "elbereth", 6.5))

    def elbereth_useful(self, v, c, attackers):
        """Engrave only where it has not just failed, against attackers that respect it, with no ranged attack."""
        failed = self.elbereth_failed
        return not c["on_elbereth"] and "Lev" not in v.cond and not c["ranged"] and \
            not (failed and failed[:2] == (c["dl"], c["hero"]) and c["turn"] - failed[2] < 100) and \
            not any(h["ch"] in K.ELBERETH_IGNORERS or K.ELBERETH_IGNORER_NAMES.search(h["name"]) for h in attackers)

    def read_engraving(self):
        """What is engraved under the hero, read with ':' (takes no game time); '' when nothing is."""
        text = " ".join(line.strip() for line in self.read_pages(":"))
        m = re.search(r'You read: "(.*?)"', text)
        return m.group(1) if m else ""

    def retreat_act(self, v, c):
        """BotHack-style retreat: stairs within 8 steps along a path no threat is next to (then take them), else
        a step to a neighbouring square next to fewer threats, preferring corridors and doorways."""
        hero, lv, dist = c["hero"], c["lv"], c["dist"]
        threats = [h["pos"] for h in c["hostiles"] if h["dist"] <= 3]
        exits = [(lv.up, "<")] if lv.up and c["dl"] > 1 else []
        exits += [(p, ">") for p, kind in lv.downs.items() if kind == "main" and self.descend_ok(dict(c, hpf=1.0))]
        for p, key in sorted(exits, key=lambda e: dist.get(e[0], 999)):
            if p == hero or dist.get(p, 999) > 8:
                continue
            route = lv.route(v, hero, p) or []
            if route and all(cheb(q, t) > 1 for q in route[1:] for t in threats) and \
                    not any(lv.cell(v, q)[0] == "+" for q in route):
                return Act("retreat", "Retreat to the %s stairs %d steps %s and take them" % (
                    "up" if key == "<" else "down", len(route), compass(hero, p)), travel(hero, p) + key, "retreat",
                    8.8, p)
        here = sum(cheb(hero, t) == 1 for t in threats)
        best = None
        for k, q in nbrs(hero):
            if dist.get(q) != 1 or v.ch(*q) in K.MON or q in lv.blocked:
                continue
            near = sum(cheb(q, t) == 1 for t in threats)
            exposed = sum(1 for _, n in nbrs(q) if dist.get(n) is not None)
            if near < here and (best is None or (near, exposed) < best[0]):
                best = ((near, exposed), k, q)
        if best:
            (near, exposed), k, q = best
            return Act("retreat", "Step %s away from the attackers (next to %d instead of %d%s)" % (
                K.DN[k], near, here, ", a narrow spot" if exposed <= 2 else ""), k, "retreat", 8.6, q)
        return None

    def crisis_ladder(self, v, c, acts):
        """A fight going badly: the standard bot ladder, in order. Pray, quaff, stairs underfoot, Elbereth,
        retreat, then fight. Returns the ladder steps plus the fighting moves; empty means the ladder is done."""
        hero, th = c["hero"], c["threats"]
        ladder = []
        if c["can_pray"] and c["trouble"]:
            ladder.append(Act("pray", "Pray (in serious trouble and the prayer timeout looks safe)", "", "pray", 9.6))
        heal = self.items(K.HEALING.pattern)
        if CFG["potions"] and heal and c["hpf"] < 0.5:
            ladder.append(Act("quaff", "Quaff the %s" % heal[0][1], "q" + heal[0][0], "quaff", 9.4))
        if CFG["spells"] and self.role == "Healer" and c["hpf"] < 0.5 and v.st.get("pw", 0) >= 5:
            ladder.append(Act("cast_heal", "Cast healing on yourself", "Za.", "cast", 9.3))
        if c["under"] == "<" and c["dl"] > 1:
            ladder.append(Act("flee_up", "Escape up the stairs you stand on", "<", "flee", 9.2))
        elif c["under"] == ">" and self.descend_ok(dict(c, hpf=1.0)) and \
                not (c["lv"].downs.get(hero) == "branch" and self.mines_policy() == "avoid"):
            ladder.append(Act("descend", "Escape down the stairs you stand on", ">", "descend", 9.2))
        adjacent = [h for h in th if h["dist"] == 1]
        if CFG["elbereth"] and adjacent and self.elbereth_useful(v, c, adjacent):
            ladder.append(Act("elbereth", "Engrave Elbereth in the dust (verified after engraving)", "", "elbereth",
                              9.0))
        if c["on_elbereth"] and not c["ranged"]:       # (a hit since engraving already voids on_elbereth)
            ladder.append(Act("rest_s", "Rest one turn on the verified Elbereth", "ms", "rest", 8.9))
        if c["ranged"]:
            ladder += [Act(a.key, a.desc, a.keys, a.kind, 9.1, a.target) for a in self.leave_line(v, c)]
        retreat = self.retreat_act(v, c)
        if retreat:
            ladder.append(retreat)
        fight = [a for a in acts if a.kind in ("attack", "fire", "throw")]
        tried = self.crisis["tried"] if self.crisis else []
        for a in ladder:         # a step tried twice in this crisis without ending it goes behind the others
            if a.key != "rest_s" and tried.count(a.key) >= 2:
                a.prior -= 6
        return sorted(ladder + fight, key=lambda a: -a.prior)

    # ------------------------------------------------------------ the model's view
    def state(self, v, c):
        h = c["hero"]
        crop = ["".join("P" if (r, x) != h and 1 <= r <= 21 and v.ch(r, x) in K.MON and v.pet(r, x) else v.ch(r, x)
                        for x in range(h[1] - 8, h[1] + 9)) for r in range(h[0] - 4, h[0] + 5)]
        s = {"goal": "Get as deep as possible in NetHack without dying (mode: %s, risk: %s)." % (
                CFG["mode"], CFG["risk"]),
             "hero": {"Dlvl": c["dl"], "HP": "%d of %d" % (c["hp"], c["hpmax"]), "HP_percent": int(100 * c["hpf"]),
                      "AC": v.st.get("ac"), "XL": c["xl"], "turn": c["turn"], "status": v.cond or ["normal"],
                      "who": v.title, "role": self.role},
             "hostile_monsters": ["%s, %d squares %s" % (x["name"], x["dist"], compass(h, x["pos"]))
                                  for x in c["hostiles"][:6]] or ["none in view"],
             "standing_on": {">": "down stairs", "<": "up stairs"}.get(c["under"], "floor"),
             "messages": list(self.msgs)[-3:],
             "recent_actions": [x["text"].split(":")[0] for x in self.hist if x["kind"] == "act"][-8:],
             "map_around_hero": crop,
             "map_legend": "@ hero (center), P pet, letters monsters, . floor, # corridor, | - walls, + door, "
                           "> down stairs, < up stairs, blank unknown"}
        if c["peace"]:
            s["peaceful_monsters"] = [x["name"] for x in c["peace"][:4]]
        if c["obst"]:
            s["never_melee"] = [x["name"] for x in c["obst"][:4]]
        if self.directive:
            s["orders"] = self.directive
        s["screen"] = v.text_screen()
        return s

    def facts(self, v, c, new_level=False):
        return {"api": HOOK_API, "dlvl": c["dl"], "hp": c["hp"], "hpmax": c["hpmax"], "hp_percent": int(100 * c["hpf"]),
                "xl": c["xl"], "turn": c["turn"], "conditions": list(v.cond), "new_level": new_level,
                "hostiles": [h["name"] for h in c["hostiles"]], "standing_on": c["under"], "role": self.role,
                "race": self.race, "messages": list(self.msgs)[-3:], "decisions": self.decisions, "keys": self.keys,
                "mode": CFG["mode"], "risk": CFG["risk"], "orders": self.directive, "screen": v.text_screen(),
                "state": self.state(v, c)}

    def ask(self, v, c, acts, extra=None, size="full"):
        questions = builtin_questions(acts, self.rng) if acts else {}
        questions.update(extra or {})
        state = self.state(v, c)
        if size == "compact":
            for k in ("screen", "recent_actions", "map_legend"):
                state.pop(k, None)
        started = time.monotonic()
        res = self.decide({"state": state, "questions": questions}, timeout=CFG["decide_timeout"])
        took = time.monotonic() - started
        self.calls += 1
        self.mtime += took
        return normalize(res.get("answers")), took

    # ------------------------------------------------------------ doing things
    def engrave_elbereth(self, c):
        """Engrave, read it back, and engrave once more if this build's dust errors misspelt it."""
        for attempt in (1, 2):
            self.flow("E-", until=8)
            text = self.read_engraving()
            if text.lower() == "elbereth":
                self.elbereth_at = (c["dl"], c["hero"], c["turn"])
                self.note("elbereth", "engraved and verified" + (" on the second try" if attempt == 2 else ""))
                return True
            self.note("elbereth", "engraving reads %r, not Elbereth" % text[:30])
        self.elbereth_at, self.elbereth_failed = None, (c["dl"], c["hero"], c["turn"])
        return False

    def do(self, v, c, a):
        lv = c["lv"]
        self.last_act = a
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
                self.last_prayer = c["turn"]
                self.flow("#pray\r", until=14)
            finally:
                self.praying = False
            return
        if self.crisis_active(c):
            self.crisis["tried"].append(a.key)
        if a.kind == "elbereth":
            self.engrave_elbereth(c)
            return
        if a.key == "rest_s":
            if self.read_engraving().lower() != "elbereth":      # scuffed since: do not rest on it
                self.elbereth_failed, self.elbereth_at = (c["dl"], c["hero"], c["turn"]), None
                self.note("elbereth", "the engraving no longer reads Elbereth: not resting on it")
                return
            self.send("ms")
            return
        if a.kind == "eat_corpse":
            self.eating_corpse = True
            try:
                self.flow("e", until=6)
            finally:
                self.eating_corpse = False
            self.corpse = None
            return
        if a.kind == "eat":
            self.send("e")
            for _ in range(5):
                w = self.term.view()
                if w.yn:
                    self.answer(w)
                    continue
                if w.obj is not None:
                    letters = re.sub(r"[^a-zA-Z]", "", w.obj.split(" or ")[0])
                    pick = next((k for k, _ in self.food_letters() if k in letters), None)
                    if pick is None and self.inv_turn < 0:
                        pick = letters[:1] or None
                    self.send(pick or "\x1b")
                    if pick is None:
                        self.food_off_until = c["turn"] + 300
                    self.inv_turn = -2 if pick else self.inv_turn     # re-read the pack later
                break
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
            w = self.flow(a.keys, until=6) if False else None
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

    def run_plan(self, v, c):
        """Execute the next queued plan item. Returns True when it acted."""
        item = self.plan[0]
        kind, _, arg = item.partition(":")
        if kind == "keys" or kind == "hex":
            self.plan.popleft()
            keys = arg if kind == "keys" else bytes.fromhex(arg)
            self.note("plan", "keys %r" % (arg[:40],))
            self.send(keys)
            return True
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
        if goal == "stairs":
            self.plan.popleft()
            downs = [p for p in lv.downs if p in c["dist"]]
            if c["under"] == ">":
                self.send(">")
            elif downs:
                self.send(travel(hero, min(downs, key=c["dist"].get)) + ">")
            else:
                self.probe(v, c, Act("probe_stairs", "", "", "probe"))
            return True
        if goal == "up":
            self.plan.popleft()
            self.send("<" if c["under"] == "<" else travel(hero, lv.up) + "<" if lv.up else "\x1b")
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

    # ------------------------------------------------------------ outcome bookkeeping
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
                    " / ".join(sorted(keys)), " / ".join(str(p) for _, p in spots), len(t))
        return None

    # ------------------------------------------------------------ the step
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
        dl, lv, hpf = c["dl"], c["lv"], c["hpf"]
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
            if dl > self.max_dl:
                self.last_new_level = self.clock()
            self.prev_dl, self.ladder = dl, ""
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
            raise Hard(branch_reason)
        self.hp_hist.append((c["turn"], c["hp"]))
        if self.plan and self.run_plan(v, c):
            self.last_try = None
            return None
        mark = (self.max_dl, sum(len(x.near) for x in self.lv.values()))   # level toggles are not progress
        if mark != self.mark:
            self.mark, self.progress, self.progress_turn = mark, self.decisions, c["turn"]
            self.progress_time = self.clock()
        acts = self.actions(v, c)
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
            self.crisis = None
            if cr["dl"] == dl and c["hp"] < cr["hp"] - step and (c["hit"] or near):
                reason = "losing fast: HP still falling after the crisis ladder, %d -> %d/%d (%s; tried: %s)" % (
                    cr["hp"], c["hp"], c["hpmax"], cr["why"], ", ".join(cr["tried"]) or "nothing applied")
        elif cr and cr.get("empty"):
            self.crisis = None
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
                reason = "low HP %d/%d with %s and no safe prayer, potion or Elbereth%s" % (
                    c["hp"], c["hpmax"], (near[0]["name"] + " near") if near else "an unseen attacker",
                    " (crisis ladder tried: %s)" % (", ".join(self.crisis["tried"]) or "nothing applied")
                    if self.crisis else "")
        elif c["hungry"] in ("Weak", "Fainting") and not any(a.kind in ("eat", "eat_corpse", "pray") for a in acts):
            reason = "%s from hunger, no food, no safe prayer" % c["hungry"]
        elif c["hungry"] == "Hungry" and not self.food_letters() and not c["can_pray"] and \
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
            cap = self.depth_cap(c["xl"], c["hpmax"], c["ac"])
            acts.insert(0, Act("linger", "Search 20 turns while the depth gate holds (Dlvl %d at most for now)" % cap,
                               "20s", "search", 0))
            if self.gate_noted != (dl, c["xl"]):
                self.gate_noted = (dl, c["xl"])
                reason = ("depth gate: Dlvl %d is explored but the loop may not go below Dlvl %d at XL %d (max HP %d, "
                          "AC %d%s); it will search and wait for experience. Lift it with --set fragile_lead=N, lead=N "
                          "or risk=high, or play on by hand" % (
                              dl, cap, c["xl"], c["hpmax"], c["ac"],
                              ", fragile" if self.fragile(c["hpmax"], c["ac"]) else ""))
        elif acts[0].prior <= -2 and not near and (c["frontier"] or self.lv_downs_usable(c)):
            self.note("stall", "blocked; best options: " + ", ".join("%s %.1f" % (a.key, a.prior) for a in acts[:5]))
            acts.insert(0, Act("wait_blocked", "Wait two turns: the way is blocked for now", "2s", "wait", 0))
        elif acts[0].prior <= -2 and not near:
            known = [p for p in lv.downs if p not in c["dist"]]
            if known:
                reason = "level exhausted: down stairs at %s unreachable (no known path: locked door, boulder or " \
                    "blocker%s)" % (", ".join("(%d,%d)" % (p[0] + 1, p[1] + 1) for p in known[:2]),
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
            kind = " ".join(re.findall(r"[A-Za-z]+", reason)[:2])
            last = self.esc_seen.get(kind)
            repeats = self.esc_counts.get((kind, dl), 0)
            window = 150 * 2 ** min(repeats, 5) if kind in ("level exhausted", "stalled") else 1
            if last and last[1:] == (dl, c["hero"]) and c["turn"] - last[0] < window:
                if kind == "level exhausted":
                    acts.insert(0, Act("search_more", "Search 20 turns (this level was already reported exhausted)",
                                       "20s", "search", 0))
                reason = None
            else:
                self.esc_seen[kind] = (c["turn"], dl, c["hero"])
                self.esc_counts[(kind, dl)] = repeats + 1
                if kind == "level exhausted":
                    lv.extra_budget += CFG["search_budget"]   # then search on
        if reason and self.esc(reason, hp=c["hp"], hpmax=c["hpmax"]):
            return reason
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
        eff = effort()
        risky = bool(near) or c["hit"] or hpf < 0.5
        healthy = time.time() >= self.breaker_until
        builtin = self.decide is not None and healthy and {
            "never": False, "danger": contested and (bool([h for h in near if h["dist"] <= 1]) or hpf < 0.5),
            "risky": contested and risky, "contested": contested}.get(eff["ask"], contested)
        due = {}
        if self.decide is not None and healthy and eff["ask"] != "never":
            ungated = eff["ungated_hooks"] == "always" or (eff["ungated_hooks"] == "with_calls" and builtin)
            try:
                due = self.hooks.due(facts, ungated)
            except HookError as e:
                raise Hard(str(e))
        sig = (dl, c["hero"], tuple(a.key for a in acts), c["hp"] // 3, tuple(h["name"] for h in near[:3]),
               self.directive)
        ans, took = {}, 0
        if builtin and not due and self.cache and self.cache[0] == sig and \
                self.decisions - self.cache[1] <= eff["cache"]:
            ans = self.cache[2]          # same situation as a moment ago: reuse the answer
            self.reused += 1
        elif builtin or due:
            try:
                ans, took = self.ask(v, c, acts if builtin else None, due, eff["state"])
                if builtin and not ("act" in ans and isinstance(ans["act"].get("probabilities"), dict)
                                    and any(a.key in ans["act"]["probabilities"] for a in acts)):
                    raise Unhealthy("malformed answer: no usable probabilities for act")
                if builtin and not all(isinstance(x, (int, float)) and 0.0 <= x <= 1.0
                                       for x in ans["act"]["probabilities"].values()):
                    raise Unhealthy("malformed answer: act probabilities must be numbers in [0, 1]")
                for x in ans.values():
                    for k in ("noul", "confidence"):
                        if k in x and not (0.0 <= x[k] <= 1.0):     # NaN fails this too
                            raise Unhealthy("malformed answer: %s out of range" % k)
                if builtin:
                    self.cache = (sig, self.decisions, ans)
                if due:
                    self.hooks.answered([k for k in due if k in ans], self.decisions)
                    if any(k in ans for k in due):
                        self.new_level_pending.discard(dl)
                self.latencies.append(took)
                slow = sorted(self.latencies)[len(self.latencies) // 2] if len(self.latencies) >= 5 else 0
                if took > CFG["decide_timeout"] or slow > CFG["slow_ms"] / 1000:
                    self.latencies.clear()
                    raise Unhealthy("slow answers (last %.1fs, median %.1fs)" % (took, slow))
            except Exception as e:     # endpoint trouble: play on the rules for a while
                self.breaker_until, self.breaker_trips = time.time() + CFG["breaker"], self.breaker_trips + 1
                self.note("model_error", "%s; rules only for %ds" % (str(e)[:160], CFG["breaker"]))
                self.last_model_error = str(e)[:160]
                ans = {}
                if self.breaker_trips in (3, 10) or (self.directive and self.breaker_trips == 1):
                    reason = "decision endpoint degraded: rules only (%d failures, last: %s)%s" % (
                        self.breaker_trips, self.last_model_error,
                        "; your orders cannot be applied while it is down" if self.directive else "")
            if "act" in ans:
                probs = {k: x for k, x in ans["act"]["probabilities"].items() if any(a.key == k for a in acts)}
                ranked = sorted(probs.items(), key=lambda kv: -kv[1])
                conf = confidence(probs)
                danger = min(1.0, max(0.0, ans.get("danger", {}).get("noul", 0.0)))
                pick = next(a for a in acts if a.key == ranked[0][0])
                info = {"src": "model", "p": round(ranked[0][1], 3), "conf": round(conf, 3), "danger": round(danger, 3),
                        "ms": int(took * 1000), "rule": top.key, "agree": pick is top,
                        "top": [(k, round(x, 3)) for k, x in ranked[:4]]}
                # The model may choose only among options the rules allow, and never an unsafe melee.
                margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)
                if pick is not top:
                    self.disagreements += 1
                vetoed = pick.prior <= -2 or "(Gnomish Mines)" in pick.desc and self.mines_policy() == "avoid" or \
                    (pick.kind == "wait" and not c["threats"]) or \
                    top.kind in ("pray", "quaff", "cast", "elbereth", "flee")
                if self.directive and conf >= 0.5 and margin >= 0.15 and not vetoed:
                    if pick is not top:
                        self.overrides += 1
                    chosen = pick
                budget_ok = self.clock() - self.last_model_esc >= CFG["esc_gap"] and self.decisions >= self.calm_until
                if budget_ok and danger > CFG["danger_max"] and (pick is not top or hpf < 0.5):
                    reason = "danger %.2f (rules want %s, model wants %s %.2f)" % (danger, top.key, pick.key,
                                                                                  ranked[0][1])
                elif budget_ok and danger > 0.5 and hpf < 0.5 and conf < CFG["p_min"]:
                    reason = "uncertain in a risky spot: %s" % ", ".join("%s %.2f" % kv for kv in ranked[:3])
                if reason:
                    self.last_model_esc = self.clock()
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
