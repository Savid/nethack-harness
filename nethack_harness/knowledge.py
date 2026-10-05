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
