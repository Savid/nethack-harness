"""Terminal symbols and observable game vocabulary."""
import re

DIRS = {"h": (0, -1), "j": (1, 0), "k": (-1, 0), "l": (0, 1),
        "y": (-1, -1), "u": (-1, 1), "b": (1, -1), "n": (1, 1)}
DIRECTION_NAMES = {"h": "west", "j": "south", "k": "north", "l": "east", "y": "northwest",
                   "u": "northeast", "b": "southwest", "n": "southeast"}
ITEMS = set(")[%?/=!(*\"$")
MON = set("abcdefghijklmnopqrstuvwxyzABCDEFGHJKLMNOPQRSTUVWXYZ@&';:~")
WARNING = set("12345")

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
CONDITION_RE = {name: re.compile(r"\b(?:%s)\b" % "|".join(forms)) for name, forms in CONDITION_FORMS.items()}


def colour(fg, bold):
    """(base colour, bright) from what the terminal emulator stored: bold+3X or a bright 9X name."""
    if fg.startswith("bright"):
        return fg[6:], True
    if fg in ("default", "white"):
        return ("white" if bold or fg == "white" else "gray"), bold or fg == "white"
    return fg, bold

# Messages that report a trap at the hero's square: the game's own description of a square, and the
# announcements of traps that leave the hero where they triggered.
TRAP_HERE = re.compile(r"There is an? ((?:[\w-]+ )*?(?:trap|trap door|hole|web|pit|squeaky board|magic portal)) here")
TRAP_EVENTS = ((r"\bdart shoots out at you", "dart trap"), (r"\barrow shoots out at you", "arrow trap"),
               (r"\bbear trap closes on", "bear trap"), (r"\byou (?:fall|land) (?:into|in) a (?:spiked )?pit", "pit"),
               (r"\bsharp iron spikes", "spiked pit"), (r"\bboard beneath you squeaks", "squeaky board"),
               (r"\brock falls on your head", "falling rock trap"), (r"\bcloud of gas", "sleeping gas trap"),
               (r"\bgush of water hits", "rust trap"), (r"\b(?:stumble into|caught in) a (?:spider )?web", "web"),
               (r"\bKAABLAMM", "land mine"))


def trap_kind(message):
    here = TRAP_HERE.search(message)
    if here:
        return here[1]
    return next((kind for pattern, kind in TRAP_EVENTS if re.search(pattern, message, re.I)), None)
