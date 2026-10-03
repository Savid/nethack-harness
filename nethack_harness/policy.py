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

    def __repr__(self):
        return "Act(%s %.1f)" % (self.key, self.prior)


class Hard(Exception):
    """An escalation that budgets and calm windows never suppress."""


class Pilot:
    def __init__(self, term, decide):
        self.term, self.decide = term, decide
        self.lv = {}
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
        self.last_model_esc, self.last_new_level = 0.0, time.time()
        self.plan = collections.deque()
        self.cache, self.reused = None, 0
        self.breaker_until, self.breaker_trips, self.new_level_pending = 0.0, 0, set()
        self.hp_hist = collections.deque(maxlen=12)        # (turn, hp)
        self.trail = collections.deque(maxlen=24)          # (dlvl, hero, action key) per decision
        self.level_trail = collections.deque(maxlen=12)    # dlvl per level change
        self.edges = {}                                    # (dlvl, pos) of a down staircase -> where it led
        self.last_down = None                              # (dlvl, pos) we last went down from
        self.branch_seen, self.waits, self.disagree = set(), 0, 0
        self.overrides = self.disagreements = 0
        self.progress_time, self.inbox_lines = time.time(), []
        self.ladder = ""                  # last escape-ladder step taken on this level
        self.rng = random.Random(0)
        self.log = None
        self.hooks = Hooks()

    # ------------------------------------------------------------ plumbing
    def level(self, dl):
        if dl not in self.lv:
            self.lv[dl] = Level(dl)
            self.lv[dl].arrived = time.time()
        return self.lv[dl]

    def view(self):
        v = self.term.view()
        if v.normal and not v.engulfed:
            self.level(v.st.get("dlvl", 0)).observe(v)
        return v

    def note(self, kind, text, **kw):
        kw.update(step=self.keys, kind=kind, text=text, t=round(time.time() - self.t0, 1))
        self.hist.append(kw)
        if self.log:
            self.log.write(__import__("json").dumps(kw) + "\n")
            self.log.flush()

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
        lv = self.level(dl)
        if K.HIT.search(text):
            self.hit_turn = v.st.get("turn", 0)
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
        if re.search(r"WHAMM|leg is in no shape", text) and self.last_act and self.last_act.kind == "kick":
            if "no shape" in text:
                lv.no_kick = True
        if K.SHOP.search(text):
            lv.shop = lv.no_kick = lv.no_dig = True
        if K.BOULDER_FAIL.search(text) and self.last_try and self.last_try["kind"] == "push":
            lv.ban(self.last_try["hero"], self.last_try["key"])
        if K.BOULDER_BUSY.search(text) and self.last_try and self.last_try["kind"] == "push":
            lv.ban(self.last_try["hero"], self.last_try["key"], self.decisions + 10)
        m = K.SWAP_REFUSED.search(text)
        if m and self.last_try and self.last_try.get("target"):
            lv.cost[self.last_try["target"]] += 10
        if re.search(r"too hard to dig|cannot (?:dig|stay)|can't dig|Your .* too heavy to apply|while wearing a shield",
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

    def learn_character(self):
        for line in self.read_pages("\x18"):
            m = K.ATTRIBUTES.search(line)
            if m:
                race = m.group(2)
                self.race = {"elf": "elven", "dwarf": "dwarvish", "gnome": "gnomish", "orc": "orcish"}.get(race, race)
                self.role = m.group(3)
            m = K.ALIGNMENT.search(line)
            if m and not self.align:
                self.align = m.group(1)
        if not self.role:
            title = (self.term.view().title.split(" the ") + [""])[1].split()
            self.role = K.ROLE_TITLES.get(title[0] if title else "", None)

    def read_inventory(self):
        inv, sec = {}, ""
        for line in self.read_pages("i"):
            m = re.match(r"\s*([a-zA-Z$]) - (.+?)\s*$", line)
            if m and len(m.group(2)) > 2:
                inv.setdefault(m.group(1), (m.group(2), sec))
            elif line.strip() in ("Weapons", "Armor", "Comestibles", "Scrolls", "Spellbooks", "Potions", "Rings",
                                  "Wands", "Tools", "Amulets", "Gems/Stones", "Coins", "Boulders/Statues"):
                sec = line.strip()
        self.inv, self.inv_turn = inv, self.turn or 0
        self.note("inventory", "; ".join("%s - %s" % (k, t) for k, (t, _) in sorted(inv.items()))[:700])

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
                m["never"] = never
                m["avoid"] = bool(CFG["avoid"] and re.search(CFG["avoid"], name))
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
        c["on_elbereth"] = self.elbereth_at is not None and self.elbereth_at[:2] == (dl, hero) and \
            turn - self.elbereth_at[2] <= 50
        return c

    def descend_ok(self, c):
        lv = c["lv"]
        depth_ok = c["dl"] < c["xl"] + self.lead() or time.time() - (lv.arrived or time.time()) > CFG["cap_lift"]
        return depth_ok and c["hpf"] >= val("descend_hp")

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
                elif lv.kicks[q] < 6 and not (lv.shop or lv.no_kick or c["watch"]):
                    acts.append(Act("kick_" + k, "Kick open the locked door to the " + K.DN[k], "\x04" + k, "kick",
                                    0.8 if fr else 2.2, q))
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
        quiver = self.items(r"\(at the ready\)|\(in quiver\)|\(quivered\)")
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
                if h["dist"] <= 6 and (dr == 0 or dc == 0 or abs(dr) == abs(dc)):
                    k = next(k for k, d in K.DIRS.items() if d == ((dr > 0) - (dr < 0), (dc > 0) - (dc < 0)))
                    acts.append(Act("fire_" + k, "Fire %s at the %s %s (it must not be meleed)" % (
                        quiver[0][1], h["name"], K.DN[k]), "f" + k, "fire", 3.2, h["pos"]))
                    break
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
        if lv.mines and lv.up_branch and policy == "avoid" and not th:
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
        calm_level = not hs and not c["hit"]
        if calm_level and not fr and not usable:
            acts += self.ladder_actions(v, c)
        if calm_level and hpf < val("rest_hp"):
            acts.append(Act("rest", "Rest 20 turns to regain HP (HP %d/%d, no monsters in view)" % (
                c["hp"], c["hpmax"]), "20s", "rest", 3 + 2 * (hpf < 0.35) - (c["hungry"] is not None)))
        if (c["blind"] or c["hallu"]) and not c["hit"]:
            acts.append(Act("rest", "Wait out blindness or hallucination (20 turns)", "20s", "rest", 4))
        if c["on_elbereth"] and any(h["dist"] <= 3 for h in hs) and hpf < 0.7:
            acts.append(Act("rest_s", "Rest two turns on Elbereth", "2s", "rest", 5))
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
        if not acts:
            acts.append(Act("search", "Search here 15 turns", "15s", "search", -3))
        if adjacent or c["hit"]:   # never start a counted search, rest or wait with a hostile next to you
            acts = [a for a in acts if a.kind not in ("search", "rest", "wait") or a.key == "rest_s" and c["on_elbereth"]]
        acts = [a for a in acts if not lv.banned(hero, a.key, self.decisions)]
        if getattr(lv, "stair_ban_until", 0) > self.decisions:
            acts = [a for a in acts if a.key not in ("descend", "goto_stairs", "leave_mines", "flee_up")]
        if self.directive and self.decisions < self.calm_until:
            pass
        return sorted(acts, key=lambda a: -a.prior) or [Act("search", "Search here 15 turns", "15s", "search", -3)]

    def lv_downs_usable(self, c):
        return any(p in c["dist"] for p in c["lv"].downs)

    def ladder_actions(self, v, c):
        """No frontier and no usable stairs: the escape ladder, one rung at a time."""
        hero, lv, dist = c["hero"], c["lv"], c["dist"]
        acts = []
        lv.locked = {q for q in lv.locked if v.ch(*q) in "+ "}       # broken or opened doors are no longer locked
        doors = [q for q in lv.locked if lv.kicks[q] < 6] if not (lv.shop or lv.no_kick or c["watch"]) else []
        for q in sorted(doors, key=lambda q: min([dist.get(n, 999) for _, n in nbrs(q)] or [999])):
            spot = min((n for k, n in nbrs(q) if k in "hjkl" and n in dist), key=dist.get, default=None)
            if spot is not None and spot != hero:
                acts.append(Act("goto_door", "Go to the locked door %s to kick it open" % compass(hero, q),
                                travel(hero, spot), "travel", 3.5, spot))
                break
        if CFG["probe"] and "stairs" not in lv.probed:
            acts.append(Act("probe_stairs", "Ask the game where known down stairs are (travel prompt)", "", "probe",
                            4.5))
        if "frontier" not in lv.probed:
            acts.append(Act("probe_frontier", "Ask the game for an unexplored edge (travel prompt)", "", "probe", 4))
        if CFG["mapping"] and "mapping" not in lv.probed and self.items(K.MAPPING.pattern) and \
                (lv.search_turns >= CFG["search_budget"] // 2 or lv.dlvl <= 2 or lv.mines):
            acts.append(Act("read_mapping", "Read a scroll of magic mapping to reveal the level", "r", "read", 3.8))
        budget_left = lv.search_turns < CFG["search_budget"] and lv.search_actions < 25
        spots = lv.spots(v, dist) if budget_left else []
        if spots:
            here = next((s for s, p in spots if p == hero), None)
            best_s, best = spots[0]
            if here is not None and here >= best_s - 20:
                acts.append(Act("search", "Search here 15 turns for hidden passages", "15s", "search", 3))
            else:
                acts.append(Act("goto_search", "Go to a likely hidden-passage spot %s to search" % compass(hero, best),
                                travel(hero, best), "explore", 3, best))
        elif not (lv.dlvl <= 2 or lv.mines) and budget_left:
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
        if CFG["elbereth"] and adjacent_threat and not c["on_elbereth"] and "Lev" not in v.cond and \
                not any(h["ch"] in K.ELBERETH_IGNORERS or re.search(r"minotaur|shopkeeper|guard|priest", h["name"])
                        for h in adjacent_threat):
            acts.append(Act("elbereth", "Engrave Elbereth in the dust to scare monsters away", "", "elbereth", 6.5))

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
    def do(self, v, c, a):
        lv = c["lv"]
        self.last_act = a
        if a.kind == "read":
            lv.probed.add("mapping")
        if a.kind == "pray":
            self.praying = True
            try:
                self.last_prayer = c["turn"]
                self.flow("#pray\r", until=14)
            finally:
                self.praying = False
            return
        if a.kind == "elbereth":
            self.flow("E-", until=8)
            self.elbereth_at = (c["dl"], c["hero"], c["turn"])
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
            if c["hpf"] >= target or c["hostiles"]:
                self.plan.popleft()
                return False
            self.send("20s")
            return True
        if goal == "search":
            left = int(param or 15)
            self.plan[0] = "goal:search:%d" % (left - 15) if left > 15 else self.plan.popleft() and None
            if self.plan and self.plan[0] is None:
                self.plan.popleft()
            lv.credit_search(hero, min(15, left))
            self.send("%ds" % min(15, left))
            return True
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
        if len(levels) >= 6 and len(set(levels)) <= 2 and self.max_dl <= max(levels):
            self.level_trail.clear()
            for d in set(levels):
                self.level(d).stair_ban_until = self.decisions + 60
            return "oscillating: Dlvl %s <-> %s %d times; the involved stairs are now avoided" % (
                levels[-1], levels[-2], len(levels))
        t = list(self.trail)[-12:]
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
        if v.engulfed:
            self.engulf_sends += 1
            if self.engulf_sends > 15:
                self.engulf_sends = 0
                raise Hard("engulfed for 15 attacks")
            self.send("Fh")
            return None
        self.engulf_sends = 0
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
                prev = self.lv.get(self.prev_dl)
                if dl > self.prev_dl:
                    if prev and prev.mines:
                        lv.mines = True
                    if self.last_down and self.last_down[0] == self.prev_dl:
                        self.edges[self.last_down] = dl
                    if dl - self.prev_dl >= 2 or dl > c["xl"] + 3:
                        branch_reason = "depth jump: Dlvl %d -> %d at XL %d" % (self.prev_dl, dl, c["xl"])
                elif prev and prev.mines and not lv.mines:
                    lv.downs[c["hero"]] = "branch"     # we climbed out of the Mines: the > underfoot leads back in
            if dl > self.max_dl:
                self.last_new_level = time.time()
            self.prev_dl, self.ladder = dl, ""
        if lv.up_branch and dl >= 2 and not lv.mines and self.lv.get(dl - 1) is not None:
            lv.mines = True
        if lv.mines and self.last_down and self.last_down[0] == dl - 1 and \
                self.lv.get(dl - 1) and not self.lv[dl - 1].mines:
            self.lv[dl - 1].downs[self.last_down[1]] = "branch"   # the staircase that brought us here
        self.max_dl = max(self.max_dl, dl)
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
            self.progress_time = time.time()
        acts = self.actions(v, c)
        near = [h for h in c["hostiles"] if h["dist"] <= 3]
        reason = None
        drop = max([hp for t, hp in self.hp_hist if c["turn"] - t <= 5] or [c["hp"]]) - c["hp"]
        adjacent = [h for h in c["hostiles"] if h["dist"] == 1]
        osc = self.oscillation(dl)
        if drop >= CFG["hp_drop"] * c["hpmax"] and self.low_noted != ("drop", c["turn"] // 10):
            self.low_noted = ("drop", c["turn"] // 10)
            reason = "losing fast: HP %d/%d, down %d in 5 turns (%s)" % (
                c["hp"], c["hpmax"], drop, ", ".join(h["name"] for h in adjacent[:3]) or "unseen attacker")
        elif len(adjacent) >= 3 and self.low_noted != ("swarm", len(adjacent), c["hp"] // 5):
            self.low_noted = ("swarm", len(adjacent), c["hp"] // 5)
            reason = "surrounded: %d adjacent hostiles (%s), HP %d/%d" % (
                len(adjacent), ", ".join(h["name"] for h in adjacent[:4]), c["hp"], c["hpmax"])
        elif osc:
            reason = osc
        elif (near or c["hit"]) and hpf < val("hp_escalate") and not any(a.prior >= 6.5 for a in acts):
            if self.low_noted != (dl, c["hp"] // 3):
                self.low_noted = (dl, c["hp"] // 3)
                reason = "low HP %d/%d with %s and no safe prayer, potion or Elbereth" % (
                    c["hp"], c["hpmax"], (near[0]["name"] + " near") if near else "an unseen attacker")
        elif c["hungry"] in ("Weak", "Fainting") and not any(a.kind in ("eat", "eat_corpse", "pray") for a in acts):
            reason = "%s from hunger, no food, no safe prayer" % c["hungry"]
        elif c["hungry"] == "Hungry" and not self.food_letters() and not c["can_pray"] and \
                self.low_noted != ("hungry", c["turn"] // 300):
            self.low_noted = ("hungry", c["turn"] // 300)
            reason = "Hungry with no food in the pack and prayer not safe yet: plan food (corpses, shops, prayer at T%s)" % (
                (self.last_prayer or 0) + 900)
        elif self.decisions - self.progress > CFG["stall"] or c["turn"] - self.progress_turn > CFG["stall_turns"] \
                or time.time() - self.progress_time > CFG["stall_secs"]:
            self.progress, self.progress_turn, self.progress_time = self.decisions, c["turn"], time.time()
            reason = "stalled: no new squares or depth (ladder tried: %s%s)" % (
                ", ".join(sorted(lv.probed)) or "-",
                "; blocked by " + ", ".join("%s %s" % (h["name"], compass(c["hero"], h["pos"])) for h in c["obst"][:3])
                if c["obst"] else "")
        elif acts[0].prior <= -2 and not near and (c["frontier"] or self.lv_downs_usable(c)):
            acts.insert(0, Act("wait_blocked", "Wait two turns: the way is blocked for now", "2s", "wait", 0))
        elif acts[0].prior <= -2 and not near:
            reason = "level exhausted: no frontier, stairs, search budget or tools left (%d search turns)" % \
                lv.search_turns
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
                if builtin:
                    self.cache = (sig, self.decisions, ans)
                if due:
                    self.hooks.answered([k for k in due if k in ans], self.decisions)
                    if any(k in ans for k in due):
                        self.new_level_pending.discard(dl)
                if took > CFG["decide_timeout"]:
                    raise Unhealthy("slow answer (%.1fs)" % took)
            except Exception as e:     # endpoint trouble: play on the rules for a while
                self.breaker_until, self.breaker_trips = time.time() + CFG["breaker"], self.breaker_trips + 1
                self.note("model_error", "%s; rules only for %ds" % (str(e)[:160], CFG["breaker"]))
                ans = ans if isinstance(ans, dict) else {}
            if "act" in ans and any(a.key in ans["act"]["probabilities"] for a in acts):
                probs = {k: x for k, x in ans["act"]["probabilities"].items() if any(a.key == k for a in acts)}
                ranked = sorted(probs.items(), key=lambda kv: -kv[1])
                conf = confidence(probs)
                danger = ans.get("danger", {}).get("noul", 0.0)
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
                budget_ok = time.time() - self.last_model_esc >= CFG["esc_gap"] and self.decisions >= self.calm_until
                if budget_ok and danger > CFG["danger_max"] and (pick is not top or hpf < 0.5):
                    reason = "danger %.2f (rules want %s, model wants %s %.2f)" % (danger, top.key, pick.key,
                                                                                  ranked[0][1])
                elif budget_ok and danger > 0.5 and hpf < 0.5 and conf < CFG["p_min"]:
                    reason = "uncertain in a risky spot: %s" % ", ".join("%s %.2f" % kv for kv in ranked[:3])
                if reason:
                    self.last_model_esc = time.time()
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
