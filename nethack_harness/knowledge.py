"""Game knowledge: symbols, colours, monster tables, prompt answers, roles and items.

Rules here are hard: the decision model never overrides them, it may only add caution.
"""
import re

DIRS = {"h": (0, -1), "j": (1, 0), "k": (-1, 0), "l": (0, 1), "y": (-1, -1), "u": (-1, 1), "b": (1, -1), "n": (1, 1)}
DN = {"h": "west", "j": "south", "k": "north", "l": "east", "y": "northwest", "u": "northeast", "b": "southwest",
      "n": "southeast"}
ITEMS = set(")[%?/=!(*\"$")
FLOOR = set(".#<>{_^\\") | ITEMS
BOULDERS = set("`0")
WARNING = set("12345")              # warning glyphs: an unseen monster of that difficulty
MON = set("abcdefghijklmnopqrstuvwxyzABCDEFGHJKLMNOPQRSTUVWXYZ@&';:~")   # 'I' = remembered unseen monster

# Status conditions: canonical name -> every form NetHack 3.7 prints (long, and short when the line is crowded).
CONDITION_FORMS = {
    "Hungry": ("Hungry", "Hngry"), "Weak": ("Weak", "Wk"), "Fainting": ("Fainting", "Fnt"), "Fainted": ("Fainted",),
    "Satiated": ("Satiated", "Sat"), "Burdened": ("Burdened", "Brd"), "Stressed": ("Stressed", "Ssd"),
    "Strained": ("Strained", "Snd"), "Overtaxed": ("Overtaxed", "Otd"), "Overloaded": ("Overloaded", "Old"),
    "Blind": ("Blind", "Blnd"), "Conf": ("Conf", "Cnf"), "Stun": ("Stun", "Stn"), "Hallu": ("Hallu", "Hal"),
    "FoodPois": ("FoodPois", "Fpois"), "TermIll": ("TermIll", "Ill"), "Stone": ("Stone", "Ston"),
    "Slime": ("Slime", "Slim"), "Strngl": ("Strngl", "Stngl"), "Lev": ("Lev",), "Fly": ("Fly",),
    "Ride": ("Ride",), "Deaf": ("Deaf",), "Held": ("Held", "Hld", "Grab"), "Trapped": ("Trapped", "Trap"),
    "Parlyz": ("Parlyz",), "Zzz": ("Zzz",), "InLava": ("InLava",), "Sliding": ("Sliding",), "Unconsc": ("Unconsc",),
}
CONDITIONS = tuple(CONDITION_FORMS)
CONDITION_RE = {name: re.compile(r"\b(?:%s)\b" % "|".join(forms)) for name, forms in CONDITION_FORMS.items()}
MAJOR = ("Weak", "Fainting", "Fainted", "FoodPois", "TermIll", "Stone", "Slime", "Strngl", "InLava")

HIT = re.compile(r"\b(hits|bites|stings|kicks|butts|touches|claws|misses|stabs|thrusts|swings|grabs|engulfs)!")
ALARM = re.compile(r"You are slowing down|limbs are stiffening|deathly sick|can't breathe|You turn into|feverish|"
                   r"You are slimed|turning into green slime|closed for inventory|You stole|strangled|"
                   r"You can't move|How dare you|break my door|You owe|You feel like a hypocrite")
STONING = re.compile(r"You are slowing down|limbs are stiffening")
SHOP = re.compile(r"Welcome to [A-Z][\w' -]*'s|cash register|shoplifters|[Cc]losed for inventory|"
                  r"leave your (?:pick-axe|dwarvish mattock) outside")
PRAY_OK = re.compile(r"You feel that .+ is (?:well-pleased|pleased|satisfied)\.|You feel much better\.|"
                     r"Your stomach feels content\.|You feel a hopeful feeling|reconciliation")
PRAY_BAD = re.compile(r"You feel that .+ is displeased\.|Thou hast angered me|Thou must relearn|You feel guilty|"
                      r"Thou hast strayed|smitten|Thou durst")
BOULDER_FAIL = re.compile(r"You try to move the boulder, but in vain|Perhaps that's why you cannot move|"
                          r"There is a boulder in your way|cannot move (?:it|past it)|won't roll")
BOULDER_BUSY = re.compile(r"You hear a monster behind the boulder")
FELL = re.compile(r"trap door opens up under you|gaping hole under you|You fall through|You fly down")
SWAP_REFUSED = re.compile(r"You stop\.\s+(.+?) doesn't want to swap places|You stop\.\s+(.+?) is in your way")
ELBERETH_IGNORERS = ("@", "A")

# Prompt answers, first match wins. Answers: keys to send, or one of the tokens below.
#   ESC, ESCALATE, GAME_OVER, PRAY (y only when the loop chose to pray), CORPSE (y only when eating a corpse)
PROMPTS = [
    (r"Really attack ", "ESC"),                                        # peacefuls: never
    (r"Really \w+ (?:onto|into) that (?:trap door|hole)\?", "y"),      # a free descent
    (r"Really \w+ (?:onto|into) that ", "n"),                           # other known traps
    (r"(?:into|Step into) that (?:poison gas|vapor|gas) cloud\?", "n"),
    (r"no return!.*Still climb\?|Still climb\?", "n"),                  # leaving the dungeon ends the game
    (r"Are you sure you want to pray\?", "PRAY"),
    (r"here; eat (?:it|one)\?", "CORPSE"),
    (r"Continue eating\?", "n"),
    (r"Stop eating\?", "y"),
    (r"Do you want to add to the current engraving\?", "n"),
    (r"Do you want to (?:add to the|erase the) .*writing", "n"),
    (r"(?:Unlock|Kick) it(?: with [^?]*)?\?", "y"),
    (r"Lock it(?: with [^?]*)?\?", "n"),
    (r"trouble lifting .*Continue\?", "n"),
    (r"Try to squeeze (?:down|through)\?", "y"),
    (r"Really quit|Are you sure you want to (?:quit|leave|enter)", "n"),
    (r"Hello stranger, who are you\?", "ESCALATE"),
    (r"Pay\?|Itemized billing\?", "ESCALATE"),
    (r"Do you want your possessions identified\?", "GAME_OVER"),
    (r"Do you want to keep the save file\?", "n"),
    (r"(?:Shall I|Do you want to) (?:remove|take off)", "n"),
    (r"Do you want to dig downward\?", "y"),
    (r"pick (?:it|them) up\?|Pick up ", "n"),
]
PROMPTS = [(re.compile(rx), ans) for rx, ans in PROMPTS]


def prompt_answer(msg):
    for rx, ans in PROMPTS:
        if rx.search(msg):
            return ans
    return None


def colour(fg, bold):
    """(base colour, bright) from what the terminal emulator stored: bold+3X or a bright 9X name."""
    if fg.startswith("bright"):
        return fg[6:], True
    if fg in ("default", "white"):
        return ("white" if bold or fg == "white" else "gray"), bold or fg == "white"
    return fg, bold


# Never melee: route around, kill only at range. Keys: symbol, or (symbol, base colour, bright).
NEVER_MELEE = {
    "e": "e: floating eye paralyses; gas spore and spheres explode",
    "j": "j: jellies have passive cold or acid",
    "y": "y: lights explode (blindness, hallucination)",
    ("F", "green", False): "green mold: passive acid",
    ("F", "brown", False): "brown mold: passive cold",
    ("F", "brown", True): "yellow mold: passive stun and poison",
    ("F", "red", False): "red mold: passive fire",
    ("c", "brown", True): "cockatrice: touching it petrifies",
    ("c", "brown", False): "chickatrice: touching it petrifies",
    ("b", "green", False): "acid blob: passive acid",
}
SAFE_F = {("F", "green", True), ("F", "magenta", False), ("F", "magenta", True)}  # lichen, violet fungus, shrieker
NEVER_NAMES = ("floating eye", "gas spore", " mold", "cockatrice", "chickatrice", "acid blob", "jelly",
               "yellow light", "black light", "sphere")
# Do not start melee below this experience level: escape, Elbereth or ranged attacks instead.
THREAT = {
    "a": 99, ("h", "red", False): 5, ("h", "magenta", True): 99, ("q", "brown", False): 4,
    ("s", "magenta", False): 8, ("d", "red", False): 8, "C": 99, "D": 99, "H": 99, "T": 99, "L": 99, "V": 99,
    "&": 99, "U": 99, ";": 99, "N": 10, "O": 7, "Y": 6,
}
THREAT_NAMES = {"soldier ant": 99, "fire ant": 99, "killer bee": 99, "dwarf": 5, "rothe": 4, "mind flayer": 99,
                "werewolf": 6, "werejackal": 3, "wererat": 3, "giant spider": 8, "owlbear": 8, "leocrotta": 8,
                "chameleon": 10, "minotaur": 99, "troll": 99, "soldier": 8, "watchman": 99, "shopkeeper": 99,
                "priest": 99, "Oracle": 99}
AMBIGUOUS = set("@hGkoqdf")          # farlook these when near: peaceful/pet/threat varies by species


def lookup(table, sym, base, bright):
    return table.get((sym, base, bright), table.get(sym))


def never_melee(sym, base, bright, name=""):
    if name and not name.startswith("unseen"):
        return next((n for n in NEVER_NAMES if n in name), None)
    if (sym, base, bright) in SAFE_F:
        return None
    return lookup(NEVER_MELEE, sym, base, bright)


def threat_xl(sym, base, bright, name=""):
    for n, xl in THREAT_NAMES.items():
        if name and n in name:
            return xl
    return lookup(THREAT, sym, base, bright) or 0


# Food and corpses.
FOODS = ("food ration", "cram ration", "lembas wafer", "fortune cookie", "apple", "carrot", "orange", "pear",
         "melon", "banana", "cream pie", "candy bar", "pancake", "egg", "kelp", "slime mold", "meatball",
         "C-ration", "K-ration", "tortilla", "tin", "eucalyptus leaf", "sprig of wolfsbane", "clove of garlic")
SAFE_CORPSES = ("newt", "jackal", "coyote", "fox", "sewer rat", "giant rat", "iguana", "lichen", "gnome lord",
                "gnome", "hill orc", "hobbit", "gecko", "giant ant", "rothe", "pony", "goblin", "wolf", "lizard",
                "dingo", "jaguar", "hill giant", "Mordor orc", "Uruk-hai", "dwarf")
UNSAFE_CORPSE = re.compile(r"kobold|were|cockatrice|chickatrice|Medusa|chameleon|doppelganger|disenchanter|"
                           r"violet fungus|yellow mold|bat\b|dog|cat\b|kitten|mimic|zombie|mummy|green slime|"
                           r"acid|lichen corpse \(rotten\)")

# Roles.
ROLE_TITLES = {"Digger": "Archeologist", "Plunderer": "Barbarian", "Plunderess": "Barbarian",
               "Troglodyte": "Caveman", "Troglodytess": "Caveman", "Rhizotomist": "Healer", "Gallant": "Knight",
               "Candidate": "Monk", "Aspirant": "Priest", "Footpad": "Rogue", "Tenderfoot": "Ranger",
               "Hatamoto": "Samurai", "Rambler": "Tourist", "Stripling": "Valkyrie", "Evoker": "Wizard"}
ROLE_LEAD = {"Valkyrie": 4, "Samurai": 4, "Barbarian": 4, "Priest": 3, "Monk": 3, "Knight": 3, "Caveman": 3,
             "Ranger": 3, "Healer": 2, "Tourist": 2, "Wizard": 2, "Rogue": 2, "Archeologist": 2}
RACES = ("human", "elven", "dwarvish", "gnomish", "orcish")
WELCOME = re.compile(r"You are an? (?:(lawful|neutral|chaotic) )?(?:(male|female) )?"
                     r"(human|elven|dwarvish|gnomish|orcish) (\w+)\.")
ATTRIBUTES = re.compile(r"a level \d+ (?:(male|female) )?(human|elven|dwarvish|gnomish|orcish|elf|dwarf|gnome|orc) "
                        r"(\w+)\.")
ALIGNMENT = re.compile(r"You are (lawful|neutral|chaotic)")

DIG_TOOLS = ("pick-axe", "dwarvish mattock")
HEALING = re.compile(r"potions? of (?:full |extra )?healing")
MAPPING = re.compile(r"scrolls? of magic mapping")
