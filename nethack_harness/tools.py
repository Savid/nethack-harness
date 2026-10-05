"""NetHack command capabilities and their terminal contracts."""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Variant:
    name: str
    prefix: str
    description: str


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    keys: str = ""
    directions: str = ""
    repeat: bool = False
    query: str = ""
    group: str = ""
    variants: tuple = ()


PAUSE = "Return control to the caller"
INVENTORY_ONLY = Variant("inventory", "m", "Select from inventory, skipping floor objects or terrain")
NO_PICKUP = Variant("no_pickup", "m", "Move without automatic pickup")
FORCE_WAIT = Variant("force", "m", "Override the game's safe-wait prevention")


_TOOLS = (
    Tool("move", "Move or bump one adjacent square in a chosen direction", directions="ykuhlbjn",
         variants=(Variant("no_pickup", "m", "Move without automatic pickup or intentionally attacking"),)),
    Tool("attack", "Force an attack at an adjacent square, even if no monster is visible", "F", directions="ykuhlbjn"),
    Tool("open", "Open an adjacent door, or a container here", "o", directions="ykuhlbjn.<>"),
    Tool("close", "Close an adjacent door", "c", directions="ykuhlbjn"),
    Tool("kick", "Kick an adjacent square", "\x04", directions="ykuhlbjn"),
    Tool("wait", "Wait here for a selected bounded number of turns", ".", repeat=True, variants=(FORCE_WAIT,)),
    Tool("search", "Search for traps and secret doors for a selected bounded number of turns", "s", repeat=True,
         variants=(FORCE_WAIT,)),
    Tool("ascend", "Go up here", "<", variants=(NO_PICKUP,)),
    Tool("descend", "Go down here", ">", variants=(NO_PICKUP,)),
    Tool("pickup", "Pick up objects here", ",", variants=(Variant("menu", "m", "Choose objects from a menu"),)),
    Tool("drop", "Choose an object or quantity to drop", "d"),
    Tool("eat", "Eat food from the floor or inventory", "e", variants=(INVENTORY_ONLY,)),
    Tool("quaff", "Drink a potion or from a fountain, sink or surrounding water", "q", variants=(INVENTORY_ONLY,)),
    Tool("read", "Read a scroll, spellbook or other readable object", "r"),
    Tool("apply", "Use a tool or object, including keys, pick-axes, lamps and instruments", "a"),
    Tool("zap", "Choose a wand to zap", "z"),
    Tool("cast", "Choose a spell to cast; then answer its targeting prompts", "Z"),
    Tool("throw", "Choose an object or quantity to throw", "t"),
    Tool("fire", "Fire quivered ammunition", "f"),
    Tool("wield", "Choose an object or quantity to wield, or empty hands", "w"),
    Tool("wear", "Choose armour to wear", "W"),
    Tool("takeoff", "Choose armour to take off", "T"),
    Tool("puton", "Choose an accessory to put on", "P"),
    Tool("remove", "Choose an accessory to remove", "R"),
    Tool("quiver", "Choose ammunition or a quantity for the quiver", "Q"),
    Tool("swap", "Swap primary and alternate weapons", "x"),
    Tool("pray", "Pray to the gods", "#pray\r"),
    Tool("engrave", "Choose how and what to engrave", "E"),
    Tool("inventory", "Inspect the complete inventory", "i", query="inventory"),
    Tool("spells", "Inspect known spells", "+", query="spells"),
    Tool("attributes", "Inspect character attributes", "\x18", query="attributes"),
    Tool("overview", "Inspect the dungeon overview", "\x0f", query="overview"),
    Tool("look_here", "Inspect the square underfoot", ":"),
    Tool("inspect", "Inspect a selected map location"),
    Tool("travel", "Follow a route to a selected known destination without attacking; bounded movement"),
    Tool("explore", "Explore beyond a corridor endpoint until a feature, branch, encounter or step limit"),
    Tool("pause", PAUSE),
)


_COMMANDS = (
    Tool("adjust", "Change inventory letters or split and combine stacks"),
    Tool("annotate", "Name or annotate the current dungeon level"),
    Tool("autopickup", "Toggle automatic pickup"),
    Tool("name", "Name a monster, an individual object or an object type"),
    Tool("chat", "Talk to an adjacent creature"),
    Tool("chronicle", "Show the journal of major events"),
    Tool("conduct", "Show voluntary conducts"),
    Tool("dip", "Dip an object into another object or floor liquid", variants=(INVENTORY_ONLY,)),
    Tool("droptype", "Drop multiple objects or classes of objects"),
    Tool("enhance", "Check or advance weapon and spell skills"),
    Tool("force", "Force a container's lock using a wielded weapon"),
    Tool("genocided", "List genocided and extinct species"),
    Tool("glance", "Select a map location to inspect briefly"),
    Tool("help", "Open game help"),
    Tool("herecmdmenu", "Show the game's menu of commands for this square"),
    Tool("history", "Show the history of the game"),
    Tool("inventtype", "Inspect inventory by object class"),
    Tool("invoke", "Invoke an object's special powers"),
    Tool("jump", "Jump to a selected map location"),
    Tool("known", "Show discovered object types"),
    Tool("knownclass", "Show discovered object types in a selected class"),
    Tool("lookaround", "Describe visible surroundings"),
    Tool("loot", "Loot containers here or interact with an adjacent saddled creature",
         variants=(Variant("saddle", "m", "Interact with an adjacent creature's saddle or inventory"),)),
    Tool("monster", "Use the hero's current monster form ability"),
    Tool("offer", "Offer a sacrifice from the floor or inventory", variants=(INVENTORY_ONLY,)),
    Tool("options", "Inspect game option settings"),
    Tool("optionsfull", "Inspect or change the full game options"),
    Tool("pay", "Pay a shopkeeper for purchases or damage",
         variants=(Variant("menu", "m", "Toggle the game's menu versus itemized payment interaction"),)),
    Tool("prevmsg", "View previous game messages"),
    Tool("quit", "Quit the current game without saving"),
    Tool("redraw", "Redraw the terminal", "\x12"),
    Tool("ride", "Mount or dismount a saddled steed"),
    Tool("rub", "Rub a lamp or stone"),
    Tool("save", "Save the game and exit"),
    Tool("seeall", "Show all equipment in use"),
    Tool("seeamulet", "Show the worn amulet"),
    Tool("seearmor", "Show worn armour"),
    Tool("seerings", "Show worn rings"),
    Tool("seetools", "Show tools in use"),
    Tool("seeweapon", "Show the wielded weapon"),
    Tool("showgold", "Show gold, shop credit and debt"),
    Tool("showspells", "List and reorder known spells"),
    Tool("showtrap", "Describe an adjacent discovered trap"),
    Tool("sit", "Sit here, including on a throne or while laying an egg"),
    Tool("takeoffall", "Select multiple worn items to remove"),
    Tool("teleport", "Use the hero's teleport ability or a teleport trap"),
    Tool("terrain", "Inspect the map without objects and monsters"),
    Tool("therecmdmenu", "Show the game's commands for an adjacent square"),
    Tool("tip", "Empty a floor or inventory container", variants=(INVENTORY_ONLY,)),
    Tool("turn", "Use the turn-undead ability"),
    Tool("twoweapon", "Toggle two-weapon combat"),
    Tool("untrap", "Untrap a door, container, floor trap or trapped creature"),
    Tool("vanquished", "List vanquished monsters"),
    Tool("version", "Show game version and build options"),
    Tool("versionshort", "Show the game version"),
    Tool("whatdoes", "Look up the meaning of a command key"),
    Tool("whatis", "Look up a symbol or map location"),
    Tool("wipe", "Wipe the hero's face"),
)


TOOLS = {tool.name: tool for tool in _TOOLS}
TOOLS.update({tool.name: replace(tool, keys=tool.keys or "#" + tool.name + "\r", group="command") for tool in _COMMANDS})
TOOL_NAMES = frozenset(tool.group or tool.name for tool in TOOLS.values())
COMMAND_DESCRIPTION = "Use another named command: " + ", ".join(tool.name for tool in _COMMANDS)


def tool_description(name, description):
    if name == "command":
        return COMMAND_DESCRIPTION
    return TOOLS[name].description if name in TOOLS else description
