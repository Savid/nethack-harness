"""Food: what is safe to eat, which food goes first, the corpses the hero made, and when hunger needs a prayer or
the outer loop. These are general NetHack rules, the same for every game.

Hunger runs Hungry (nutrition 150), Weak (50), Fainting (0); the hero starves below -(100 + 10 x Con). Prayer
fixes Weak and worse (major trouble) once the prayer timeout allows it, and sets nutrition back to 900."""
import re

from .base import Act
from .level import cheb, travel
from .monsters import CORPSES, MONSTERS

# A corpse eaten within this many turns of the kill can neither be tainted nor make the hero ill: its rot is
# age / (10 + rn2(20)), illness needs rot above 3 and tainting above 5.
FRESH_TURNS = 40
NEVER_ROTS = ("lichen", "lizard")
# Corpses never eaten by the loop, whatever the hero resists: petrification, sliming, polymorph, lycanthropy,
# stun, hallucination, mimicking, a lost intrinsic, the Riders.
BAD_CORPSE = re.compile(r"^(?:cockatrice|chickatrice|Medusa|green slime|chameleon|doppelganger|sandestin|"
                        r"genetic engineer|(?:giant |vampire )?bat|violet fungus|(?:small |large |giant )?mimic|"
                        r"disenchanter|Death|Pestilence|Famine)$|^were|zombie$|mummy$")
VEGAN_CLASSES = set("Fbjvy")      # fungi, blobs, jellies, vortices, lights: a Monk eats only these
CANNIBAL_FLAG = {"human": "h", "elven": "e", "dwarvish": "d", "gnomish": "g"}     # orcs may eat their own kind
POISON_RESISTANT_ROLES = ("Barbarian", "Healer")

# Pack food in the order it is eaten. Cheap, heavy and perishable food goes first; light rations that keep are
# saved for when nothing else is left; cures and unknowns only when Weak with no safe prayer.
EAT_FIRST = ("fortune cookie", "apple", "carrot", "pear", "orange", "banana", "melon", "kelp frond", "slime mold",
             "candy bar", "cream pie", "pancake", "tortilla", "meatball", "meat stick", "meat ring",
             "huge chunk of meat", "lump of royal jelly", "clove of garlic", "lichen corpse", "tripe ration",
             "food ration", "cram ration")
KEEP = ("C-ration", "K-ration", "lembas wafer")
RESERVE = ("tin", "sprig of wolfsbane", "eucalyptus leaf", "lizard corpse")
NUTRITION = {"fortune cookie": 40, "apple": 50, "carrot": 50, "pear": 50, "orange": 80, "banana": 80, "melon": 100,
             "kelp frond": 30, "slime mold": 80, "candy bar": 100, "cream pie": 100, "pancake": 200, "tortilla": 80,
             "meatball": 5, "meat stick": 5, "meat ring": 5, "huge chunk of meat": 2000, "lump of royal jelly": 200,
             "clove of garlic": 40, "lichen corpse": 200, "tripe ration": 200, "food ration": 800,
             "cram ration": 600, "C-ration": 300, "K-ration": 400, "lembas wafer": 800, "tin": 50,
             "sprig of wolfsbane": 40, "eucalyptus leaf": 30, "lizard corpse": 40}
FOOD_ORDER = EAT_FIRST + KEEP + RESERVE
PLURALS = {"huge chunk of meat": "huge chunks of meat", "lump of royal jelly": "lumps of royal jelly",
           "clove of garlic": "cloves of garlic", "sprig of wolfsbane": "sprigs of wolfsbane",
           "eucalyptus leaf": "eucalyptus leaves", "lichen corpse": "lichen corpses", "lizard corpse": "lizard corpses"}
FOOD_RE = [(name, re.compile(r"\b(?:%s|%s)\b" % (re.escape(name), re.escape(PLURALS.get(name, name + "s")))))
           for name in FOOD_ORDER]
NOT_FOOD = re.compile(r"\bunpaid\b|\beggs?\b|\bglob\b|tin opener|tinning kit")
RESERVE_FOOD = 1000    # fruit is thrown at blockers only while the rest of the pack holds this much nutrition
SPARE_PACK = 1500      # with this much pack food, a hero that is not hungry leaves corpses alone
FLOOR_FOOD = re.compile(r"There (?:is|are) (.+?) here; eat (?:it|one)\?")


def food_kind(text):
    """The FOOD_ORDER name of a pack line ("3 uncursed apples" -> "apple"), or None (eggs, globs, other corpses,
    unpaid goods and anything unknown are never eaten by the loop)."""
    if NOT_FOOD.search(text):
        return None
    if "corpse" in text and not re.search(r"\b(?:lichen|lizard) corpse", text):
        return None
    return next((name for name, rx in FOOD_RE if rx.search(text)), None)


def quantity(text):
    m = re.match(r"\s*(\d+) ", text)
    return int(m.group(1)) if m else 1


def corpse_name(text):
    """The monster in a floor or pack description: "2 uncursed partly eaten jackal corpses" -> "jackal"."""
    t = re.sub(r"^(?:an?|the|\d+)\s+", "", text.strip())
    t = re.sub(r"^(?:(?:blessed|uncursed|cursed|partly eaten)\s+)+", "", t)
    m = re.match(r"(.+?) corpses?\b", t)
    return m.group(1) if m else None


def corpse_hazard(name, role=None, race=None, xl=1, poison_res=False):
    """Why this monster's corpse is not safe for this hero, or None when it is (freshness aside)."""
    if not name or BAD_CORPSE.search(name):
        return "dangerous to eat"
    flags = CORPSES.get(name, "")
    if name not in MONSTERS and not flags:
        return "unknown monster"
    if "n" in flags:
        return "leaves no corpse"
    if "a" in flags:
        return "acidic"
    resists = poison_res or role in POISON_RESISTANT_ROLES or race == "orcish" or (role == "Monk" and xl >= 3)
    if "p" in flags and not resists:
        return "poisonous"
    cannibal_ok = role == "Caveman" or race == "orcish"
    if not cannibal_ok and CANNIBAL_FLAG.get(race or "human") in flags:
        return "cannibalism"
    if "m" in flags and not cannibal_ok:
        return "a domestic animal (aggravates monsters)"
    if role == "Monk" and MONSTERS.get(name, ("?",))[0] not in VEGAN_CLASSES:
        return "meat (a Monk's alignment suffers)"
    return None


class Food:
    """Mixed into the Pilot: corpse memory, the eating actions, prompts and the hunger escalation. State lives on
    the Pilot: kills [(dlvl, pos, name, turn or None)], poison_res, tin_smell, eating_at, fainting_since."""

    # ------------------------------------------------------------------ what the pack holds
    def food_letters(self):
        """(letter, text) of every pack item the loop would ever eat, in the order it eats them (anything partly
        eaten first)."""
        out = []
        for k, (t, sec) in self.inv.items():
            kind = food_kind(t) if sec in ("Comestibles", "") else None
            if kind:
                out.append((0 if "partly eaten" in t and kind not in RESERVE else 1, FOOD_ORDER.index(kind), t, k))
        return [(k, t) for _, _, t, k in sorted(out)]

    def food_choice(self, hungry, can_pray=False):
        """(letter, text) to eat now, or None. Hungry: the cheapest ordinary food, then the kept rations. Weak or
        worse: the same, then tins and cures, unless a safe prayer will do instead."""
        if not hungry:
            return None
        return next(((k, t) for k, t in self.food_letters()
                     if food_kind(t) not in RESERVE or (hungry != "Hungry" and not can_pray)), None)

    def pack_nutrition(self, skip=None):
        """Rough nutrition of the pack's ordinary food, one item of letter skip left out."""
        return sum(NUTRITION[food_kind(t)] * (quantity(t) - (k == skip)) for k, t in self.food_letters()
                   if food_kind(t) not in RESERVE)

    # ------------------------------------------------------------------ corpses the hero made
    def note_kill(self, dl, pos, name, turn):
        """A kill at pos may have left a corpse there; remember it with its turn (None: it never rots)."""
        self.kills.append((dl, pos, name, turn))
        del self.kills[:-20]

    def corpse_ok(self, name, age):
        """Safe for this hero and fresh: killed fewer than FRESH_TURNS turns ago, or a kind that never rots."""
        if corpse_hazard(name, self.role, self.race, self.last_st.get("xl") or 1, self.poison_res):
            return False
        return name in NEVER_ROTS or (age is not None and 0 <= age < FRESH_TURNS)

    def fresh_corpses(self, v, c):
        """[(pos, name)] of safe, fresh corpses the hero made on this level, nearest first. A kill whose square
        shows no object left no corpse (most small monsters often leave none): it is forgotten."""
        out = []
        for dl, pos, name, turn in list(self.kills):
            if dl != c["dl"]:
                continue
            if pos != c["hero"] and v.ch(*pos) != "%":
                if v.ch(*pos) in ".#":                  # plain floor: nothing there
                    self.forget_corpses(dl, pos)
                continue
            if self.corpse_ok(name, None if turn is None else c["turn"] - turn):
                out.append((cheb(pos, c["hero"]), pos, name))
        return [(pos, name) for _, pos, name in sorted(out)]

    def forget_corpses(self, dl, pos):
        self.kills = [k for k in self.kills if k[:2] != (dl, pos)]

    def floor_food_answer(self, msg):
        """y to "There is a jackal corpse here; eat it?" only while eating a corpse, and only for a safe corpse
        the hero made on this square that is still fresh (or a lichen or lizard). Everything else is n."""
        m = FLOOR_FOOD.search(msg)
        if not (m and self.eating_corpse):
            return "n"
        name = corpse_name(m.group(1))
        if name is None or re.search(r"\bcursed\b", m.group(1)):       # a cursed corpse rots faster
            return "n"
        st = self.last_st
        ages = [st.get("turn", 0) - t for d, p, n, t in self.kills
                if (d, p) == (st.get("dlvl"), self.eating_at) and n == name and t is not None]
        return "y" if self.corpse_ok(name, min(ages) if ages else None) else "n"

    def tin_answer(self, msg):
        """y to a tin's "Eat it?" only for spinach or the meat of a monster that is safe to eat (tins never rot)."""
        m = re.search(r"It smells like (.+?)\.", msg)
        what = m.group(1) if m else self.tin_smell or ""
        if what == "spinach" or "contains spinach" in msg:
            return "y"
        what = re.sub(r"^(?:the |an? )", "", what)
        names = [x for x in (what, what[:-1], what[:-2], re.sub(r"ves$", "f", what)) if x in MONSTERS]
        return "y" if names and not corpse_hazard(names[0], self.role, self.race, self.last_st.get("xl") or 1,
                                                  self.poison_res) else "n"

    def food_message(self, text, v):
        """Messages about food: kills that leave corpses, poison resistance, tins, an empty pack."""
        turn, dl = v.st.get("turn", 0), v.st.get("dlvl", 0)
        a = self.last_act
        m = re.search(r"You kill (?:the |an? )?([A-Za-z][\w' -]*?)!", text)
        if m and "poor " not in m.group(0) and a and a.kind in ("attack", "zap", "fire", "throw") and a.target:
            self.note_kill(dl, a.target, m.group(1), turn)
        m = re.search(r"You see here an? (lichen|lizard) corpse", text)
        if m and v.hero:
            self.note_kill(dl, v.hero, m.group(1), None)
        if re.search(r"You feel (?:especially )?healthy", text):
            self.poison_res = True
        m = re.search(r"It smells like (.+?)\.", text)
        if m or "contains spinach" in text:
            self.tin_smell = m.group(1) if m else "spinach"
        if "You don't have anything to eat" in text:
            self.food_off_until = turn + 300

    # ------------------------------------------------------------------ actions
    def food_actions(self, v, c, acts):
        hero, hungry, th, lv = c["hero"], c["hungry"], c["threats"], c["lv"]
        weak = hungry in ("Weak", "Fainting", "Fainted")
        self.track_hunger(c)
        # a meal takes turns and a rotten one can knock the hero out: eat with no hostile in view unless Weak
        calm = not (c["seen_hostiles"] or c["ranged"]) or (weak and not th)
        # one meal in seven is rotten whatever its age: a hero that is not hungry eats corpses only to save a
        # short pack
        wanted = hungry or self.pack_nutrition() < SPARE_PACK
        corpses = self.fresh_corpses(v, c) if wanted and calm and not ("Satiated" in v.cond or th or lv.shop) \
            else []
        here = next((name for pos, name in corpses if pos == hero), None)
        if here:
            # a fresh corpse is food that will not keep: eat it unless Satiated (no choking from below Satiated)
            acts.append(Act("eat_corpse", "Eat the fresh %s corpse here (%s)" % (here, hungry or "not hungry"),
                            "e", "eat_corpse", 7.5 if weak else 5.5 if hungry else 5.2))
        elif corpses:
            pos, name = corpses[0]
            d = c["dist"].get(pos, 99)
            if d <= (8 if hungry else 2):
                acts.append(Act("goto_corpse", "Step to the fresh %s corpse %d squares away to eat it" % (name, d),
                                travel(hero, pos), "travel", 7 if weak else 5.4 if hungry else 3.9, pos))
        if hungry and c["turn"] >= self.food_off_until:
            pick = self.food_choice(hungry, c["can_pray"])
            if pick or self.inv_turn < 0:
                acts.append(Act("eat", "Eat %s (you are %s)" % (pick[1] if pick else "food", hungry), "e", "eat",
                                (3 if weak else 0) if th else 6.5 if weak else 5.1 if calm else 1))
        if hungry in ("Fainting", "Fainted") and not c["can_pray"] and not self.prayer_broken and \
                not any(a.kind in ("eat", "eat_corpse") for a in acts):
            # fainting leaves the hero helpless, and starvation is certain: a prayer 500+ turns after the last
            # one works about 7 times in 8, and any prayer beats starving
            left = self.starve_eta(c) - c["turn"]
            if left <= 60 or c["turn"] - (self.last_prayer or 0) >= 500:
                acts.append(Act("pray", "Pray: Fainting with no food, starving in about %d turns (a risky prayer "
                                        "beats a sure death)" % max(0, left), "", "pray", 9))

    def prayer_is_food(self, c):
        """Hungry with nothing to eat: the next prayer will be needed for hunger (prayer at Weak fixes HP too)."""
        return c["hungry"] == "Hungry" and self.inv_complete and not self.food_choice("Weak")

    def track_hunger(self, c):
        """The starvation clock starts when Fainting does, and stops once fed."""
        if c["hungry"] not in ("Fainting", "Fainted"):
            self.fainting_since = None
        elif self.fainting_since is None:
            self.fainting_since = c["turn"]

    def starve_eta(self, c):
        """Turn at which the hero starves: Fainting starts at nutrition 0, death comes below -(100 + 10 x Con)."""
        since = c["turn"] if self.fainting_since is None else self.fainting_since
        return since + 100 + 10 * (self.last_st.get("con") or 10)

    # ------------------------------------------------------------------ eating
    def eat_floor(self, c):
        """Eat a corpse underfoot: each floor prompt is answered by its corpse; the pack prompt is cancelled."""
        self.eating_corpse, self.eating_at = True, c["hero"]
        start = self.msg_count
        try:
            self.flow("e", until=8)
        finally:
            self.eating_corpse = False
        said = list(self.msgs)[-(self.msg_count - start):] if self.msg_count > start else []
        if not any("You stop eating" in x for x in said):
            self.forget_corpses(c["dl"], c["hero"])     # eaten, gone, or nothing safe here

    def eat_pack(self, c):
        """Eat from the pack: floor prompts are refused, the pack prompt gets the chosen item."""
        self.send("e")
        for _ in range(8):
            w = self.term.view()
            if w.yn:
                self.answer(w)
            elif w.more:
                self.message(" ".join(r.strip() for r in w.rows[:2]).replace("--More--", "").strip(), w)
                self.send(" ")
            elif w.obj is not None:
                letters = re.sub(r"[^a-zA-Z]", "", w.obj.split(" or ")[0])
                pick = self.food_choice(c["hungry"] or "Hungry", c["can_pray"])
                key = pick[0] if pick and pick[0] in letters else None
                if key is None and self.inv_turn < 0:
                    key = letters[:1] or None          # the pack was never read: the game knows what is food
                self.send(key or "\x1b")
                if key is None:
                    self.food_off_until = c["turn"] + 300
                else:
                    self.inv_turn = -2                 # the pack changed: re-read it
                return
            else:
                return

    # ------------------------------------------------------------------ the outer loop
    def hunger_reason(self, c, acts):
        """(dedupe key, reason) for a hunger escalation, or None. Only when the loop cannot fix it itself: no food
        to eat, no fresh corpse and no prayer that will be safe before Fainting. Once per hunger state and prayer
        (the caller stores the key in hunger_noted)."""
        hungry = c["hungry"]
        if not hungry or any(a.key in ("eat", "eat_corpse", "goto_corpse", "pray") for a in acts):
            return None
        if (hungry == "Hungry" and not self.inv_complete) or self.food_choice(hungry):
            return None              # the pack is unknown, or food is there (a threat only delays eating)
        horizon = {"Hungry": 150, "Weak": 50}.get(hungry, 0)       # turns until Fainting, at the latest
        if self.prayer_safe(c["turn"] + horizon):
            return None              # the loop prays once Weak and the timeout allows
        key = ("Fainting" if hungry == "Fainted" else hungry, self.last_prayer, self.prayer_broken)
        if self.hunger_noted == key:
            return None
        safe = "never (a prayer failed)" if self.prayer_broken else "from T%d" % (
            110 if self.last_prayer is None else self.last_prayer + 900)
        if hungry == "Hungry":
            return key, ("Hungry with no food in the pack, no fresh corpse, and no safe prayer before Fainting "
                         "(safe %s): find food (kill and eat, a shop) or plan a prayer" % safe)
        return key, "%s from hunger, no food, no safe prayer (safe %s)%s" % (
            hungry, safe, "" if self.prayer_broken else "; the loop prays anyway when starvation is near")
