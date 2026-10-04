"""What the loop knows: the screen, the pack, the character, the level's branch, farlook, depth limits."""
import os
import re
import time

from . import knowledge as K
from .level import pos1, cheb, Level, travel
from .settings import CFG, val


def process_identity():
    """This process on this boot: monotonic times are only comparable within it."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
    except OSError:
        boot = ""
    return "%s/%d" % (boot, os.getpid())


class Perception:
    def view(self):
        v = self.term.view()
        if v.st.get("dlvl") is not None and v.st.get("hp") is not None:
            self.last_st = dict(v.st)
        if v.normal and not v.engulfed and self.resolved(v.st.get("dlvl", 0)):
            self.level(v.st.get("dlvl", 0)).observe(v)
        return v

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

    def setup(self):
        """hilite_pet (pets in reverse video), time (turn counter), autoopen; no sparkle or timed_delay."""
        want = {"hilite_pet": True, "time": True, "sparkle": False, "timed_delay": False, "autoopen": True,
                "fireassist": False}     # fireassist would swap the melee weapon for a launcher on f

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
        self.note("setup", "options: hilite_pet, time, autoopen on; sparkle, timed_delay, fireassist off")

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

    def learn_spells(self):
        """The + menu: letter, level and failure rate of every known spell."""
        self.spells = {}
        for line in self.read_pages("+"):
            m = K.SPELL_LINE.search(line)
            if m:
                self.spells[m.group(2).strip()] = (m.group(1), int(m.group(3)), int(m.group(4)))

    def spell(self, use, v):
        """(letter, name) of a known spell for this use that Pw allows and rarely fails, or None."""
        if not CFG["spells"]:
            return None
        for name, (letter, _, fail) in self.spells.items():
            cost, kind = K.SPELLS.get(name, (None, None))
            if kind == use and fail <= 30 and v.st.get("pw", 0) >= cost:
                return letter, name
        return None

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
        raw = self.read_pages("i")
        lines = []
        for n in range(0, len(raw), 24):          # one screen per page; a small menu is drawn over the map
            page = raw[n:n + 24]
            col = next((m.start() for r in page for m in [re.search(r"\((?:end|\d+ of \d+)\)\s*$", r)] if m), 0)
            lines += [r[col:] for r in page]
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
        if complete:
            self.inv_complete = True
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
        if self.endgame():
            return "allow"                 # in the endgame any descent counts
        m = CFG["mines"]
        if m == "auto":
            return "allow" if self.race in ("dwarvish", "gnomish") else "avoid"
        return m

    def lead(self):
        if CFG["lead"] is not None:
            return int(CFG["lead"])
        return K.ROLE_LEAD.get(self.role, 3) + val("lead")

    def farlook(self, v, p, cache=True):
        """What the game says is at p (the ; command), cached per glyph or square. A `lookup` function, when
        set, answers instead of the game (replay fixtures and tests)."""
        if self.lookup is not None:
            return self.lookup(p) or "unknown"
        ch = v.ch(*p)
        base, bright = v.col(*p)
        key = (v.st.get("dlvl"), ch, base, bright) if ch not in K.AMBIGUOUS and cache else \
            (v.st.get("dlvl"), p, ch, base)
        if key not in self.species:
            self.term.send(travel(v.hero, p, ";"))
            text = self.term.view().msg
            for _ in range(20):               # the answer can arrive late or in pieces: wait up to about a second
                if "(" in text and ")" in text:
                    break
                time.sleep(0.05)
                self.term.poll()
                again = self.term.view().msg
                if again == text and text:
                    break
                text = again
            if not text:
                self.note("farlook", "empty answer at %s; screen top: %r" % (pos1(p), self.term.view().rows[0][:80]))
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
                    if K.not_a_monster(name):
                        # farlook gave nothing usable: forget it (look again next time) and judge the monster by
                        # its glyph and colour, assuming the most dangerous monster that looks like this
                        self.species = {k: x for k, x in self.species.items() if x != name}
                        self.note("farlook", "no monster named at %s (%r): judged by glyph" % (pos1(p), name[:40]))
                        name = "likely %s" % K.guess_monster(ch, base, bright)
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
                m["avoid"] = bool(CFG["avoid"] and re.search(CFG["avoid"], name)) or \
                    (not c["hallu"] and K.keep_away(name, self.race, self.role))
                m["threat"] = 99 if m["avoid"] else 0 if c["hallu"] else K.threat_xl(ch, base, bright, name)
                m["dmg"], m["rdmg"], m["speed"], level = K.monster_power(name, ch, base, bright)
                if not c["hallu"] and m["speed"] < 12 and level >= xl + 3 and "peaceful" not in name:
                    m["avoid"] = True        # far stronger but slower than the hero: walk away, never melee
                (obst if never else peace if "peaceful" in name else hostile).append(m)
        for p, desc in list(lv.traps.items()):   # identify nearby traps once: trap doors are free descents
            if desc is None and v.ch(*p) == "^" and cheb(p, hero) <= 7 and not c["hallu"]:
                name = self.farlook(v, p, cache=False)
                lv.traps[p] = next((t for t in ("trap door", "hole", "level teleporter", "magic portal")
                                    if t in name), name[:30] or "trap")
                if lv.traps[p] in ("level teleporter", "magic portal"):
                    lv.cost[p] += 60
        hostile.sort(key=lambda h: h["dist"])
        c["seen_hostiles"] = list(hostile)      # every hostile in view, fleeing or far ones included
        if self.decisions < self.boost_until:   # oscillating: treat monsters that keep their distance as scenery
            hostile = [h for h in hostile if h["dist"] <= 1]
        self.hostiles, self.obst = hostile, obst
        c.update(hostiles=hostile, peace=peace, obst=obst, watch=watch)
        lv.blocked = {h["pos"] for h in obst} | lv.statues
        c["threats"] = [h for h in hostile if h["dist"] <= 2]
        # the most damage the hostiles can do before the hero acts again: melee from those that can reach the
        # hero this turn, missiles and spells from those in a straight line
        c["incoming"] = sum(h["dmg"] for h in hostile if h["dist"] <= max(1, -(-h["speed"] // 12))) + \
            sum(h["rdmg"] for h in hostile if 2 <= h["dist"] <= 8 and (
                h["pos"][0] == hero[0] or h["pos"][1] == hero[1] or
                abs(h["pos"][0] - hero[0]) == abs(h["pos"][1] - hero[1])))
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

    @staticmethod
    def fragile(hpmax, ac, xl=1):
        """Fragile for this level: max HP below sturdy_hp + sturdy_hp_per_xl * XL, or AC worse than sturdy_ac."""
        return hpmax < CFG["sturdy_hp"] + CFG["sturdy_hp_per_xl"] * xl or ac > CFG["sturdy_ac"]

    def depth_limits(self, xl, hpmax, ac):
        """(cap, which limit binds, how to lift it). Two limits apply and the shallower wins:
        the role lead, XL + lead; and the pace, XL + fragile_lead until pace_xl, then one more for a sturdy hero
        (+1 at risk=high)."""
        lead = self.lead()
        fragile = self.fragile(hpmax, ac, xl)
        pace = int(CFG["fragile_lead"]) + (1 if xl >= CFG["pace_xl"] and not fragile else 0) + \
            (1 if CFG["risk"] == "high" else 0)
        if xl + lead <= xl + pace:
            how = "role lead: XL %d + lead %d (%s%s); lift with --set lead=N or risk=high" % (
                xl, lead, "set" if CFG["lead"] is not None else "%s %d" % (self.role or "role", K.ROLE_LEAD.get(
                    self.role, 3)), "" if CFG["lead"] is not None else ", risk %s %+d" % (CFG["risk"], val("lead")))
            return xl + lead, "lead", how
        bar = CFG["sturdy_hp"] + CFG["sturdy_hp_per_xl"] * xl
        why = " and ".join(x for x in ("max HP %d < %d" % (hpmax, bar) if hpmax < bar else "",
                                       "AC %d > %d" % (ac, CFG["sturdy_ac"]) if ac > CFG["sturdy_ac"] else "") if x)
        how = "pace: XL %d + %d (%s); lift with --set fragile_lead=N" % (
            xl, pace, "fragile: " + why if fragile else
            "XL+%d until XL %d, then XL+%d" % (CFG["fragile_lead"], CFG["pace_xl"], CFG["fragile_lead"] + 1))
        return xl + pace, "pace", how

    def depth_cap(self, xl, hpmax, ac):
        return 99 if self.endgame() else self.depth_limits(xl, hpmax, ac)[0]

    def endgame(self):
        """The final endgame_secs of time_left: depth caps are lifted, the HP gate for stairs is 0.5 at most, and
        any descent (the Mines included) is preferred."""
        left = self.seconds_left()
        return left is not None and left <= CFG["endgame_secs"]

    def set_time_left(self, seconds):
        """Remember the outer loop's remaining time against this process's monotonic clock."""
        self.time_budget = (float(seconds), time.monotonic(), process_identity())

    def seconds_left(self):
        """Seconds of play left, or None when unknown: never set, or set in another process (a restart or a copy
        of the state directory), whose monotonic clock means nothing here."""
        b = self.time_budget
        if not b or b[2] != process_identity():
            return None
        return max(0.0, b[0] - (time.monotonic() - b[1]))

    def descend_ok(self, c):
        lv = c["lv"]
        cap = self.depth_cap(c["xl"], c["hpmax"], c.get("ac", 10))
        lift = self.clock() - (lv.arrived or self.clock()) > CFG["cap_lift"] and \
            (c["xl"] >= 3 or not self.fragile(c["hpmax"], c.get("ac", 10), c["xl"]))
        # patience: gated on an explored level for gate_patience turns allows one level more (never two)
        if CFG["gate_patience"] and lv.gated_since is not None and c["turn"] - lv.gated_since >= \
                CFG["gate_patience"] and c["dl"] < cap + 1:
            lift = True
        hp_gate = min(val("descend_hp"), 0.5) if self.endgame() else val("descend_hp")
        return (c["dl"] < cap or lift) and c["hpf"] >= hp_gate

    def read_engraving(self):
        """What is engraved under the hero, read with ':' (takes no game time); '' when nothing is."""
        text = " ".join(line.strip() for line in self.read_pages(":"))
        m = re.search(r'You read: "(.*?)"', text)
        return m.group(1) if m else ""

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

    def resolved(self, dl):
        """True once the branch of the level we are on is known (see track_branch)."""
        return self.branch_dl in (None, dl)

    def level(self, dl, branch=None):
        """Levels are remembered per branch: Mines level 5 and Dlvl 5 of the main dungeon are different maps."""
        branch = branch or self.branch
        k = dl if branch == "main" else (branch, dl)
        if k not in self.lv:
            self.lv[k] = Level(dl)
            self.lv[k].mines = branch == "mines"
            self.lv[k].arrived = self.clock()
        return self.lv[k]
