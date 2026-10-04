"""Help text: the capability map, the brief map and per-topic help."""

from . import __version__
from .escalation import help_text as escalation_help
from .settings import CFG, DEFAULTS, EFFORT, MODE_KEYS, MODES, RISK


SETTING_DOCS = [
            ("mode", "", "descend | explore | careful (a mode sets only the keys it owns; others are kept)"),
            ("risk", "", "low | normal | high: HP gates and depth lead (e.g. --set risk=high to dive)"),
            ("effort", "", "decision effort: off | low | medium | high (see help effort)"),
            ("descend_hp", "", "descend only at or above this HP fraction (None = from risk)"),
            ("rest_hp", "", "rest below this HP fraction when nothing is in view"),
            ("hp_escalate", "", "escalate below this HP fraction under attack with no safe remedy"),
            ("elbereth_hp", "", "engrave Elbereth below this HP fraction when threatened"),
            ("lead", "", "max depth = XL + lead (None = by role, adjusted by risk)"),
            ("mines", "", "auto (allow for gnome/dwarf heroes) | allow | avoid | escalate"),
            ("dig", "", "1 = dig down with a pick-axe or mattock instead of hunting stairs"),
            ("avoid", "", "regex of monster names never to melee, e.g. avoid='soldier ant|dwarf'"),
            ("danger_max", "", "escalate when model danger > this and it disagrees with the rules"),
            ("p_min", "", "escalate in risky spots when normalised model confidence < this"),
            ("esc_gap", "", "seconds between model-triggered escalations"),
            ("calm", "", "decisions after a resume before model escalations may fire"),
            ("stall", "", "escalate after this many decisions without new squares or depth"),
            ("stall_turns", "", "...or after this many game turns without new squares or depth"),
            ("decide_timeout", "", "seconds per decision call; slower answers trip the breaker"),
            ("breaker", "", "seconds of rules-only play after a failed or slow decision call"),
            ("slow_ms", "", "trip the breaker when the median of the last 5 calls is slower than this"),
            ("search_budget", "", "search turns per level before the ladder moves on"),
            ("sturdy_hp", "", "fragile: max HP below sturdy_hp + sturdy_hp_per_xl * XL (14 at XL 1)..."),
            ("sturdy_hp_per_xl", "", "...the per-level part of that HP bar"),
            ("sturdy_ac", "", "...or AC above this"),
            ("fragile_lead", "", "pace: descend no deeper than XL + this (+1 at risk=high); the cap is the "
                                 "shallower of this pace and XL + lead"),
            ("pace_xl", "", "from this XL a sturdy hero's pace is XL + fragile_lead + 1"),
            ("gate_patience", "", "turns held by the depth cap on an explored level before one level more is "
                                  "allowed (0 = wait for experience)"),
            ("cap_lift", "", "seconds on a level after which the depth lead stops blocking descent "
                             "(never before XL 3 for a fragile hero)"),
            ("potions", "", "1 = quaff known healing potions in emergencies"),
            ("spells", "", "1 = cast known spells: healing when hurt, force bolt at dangerous foes and blockers"),
            ("elbereth", "", "1 = engrave Elbereth in emergencies"),
            ("trapdoors", "", "1 = use known trap doors and holes as free descents"),
            ("probe", "", "1 = ask the game where the stairs are when none are visible"),
            ("mapping", "", "1 = read a known magic mapping scroll when a level runs out of options; "
                            "2 = read one on arrival at each new level while they last"),
            ("briefing", "", "1 = pause once at start with role, kit and capabilities"),
            ("hp_drop", "", "escalate 'losing fast' when HP falls by this fraction of max within 5 turns"),
            ("hp_drop_min", "", "...and by at least this many points; the same fight re-escalates only after "
                                "another step of loss"),
            ("milestone", "", "off | depth | xl | both: pause once at each new deepest Dlvl and/or new XL while "
                              "healthy (a natural moment to checkpoint or rethink)"),
            ("milestone_hp", "", "milestones wait until HP is at least this fraction and no hostile is in view"),
            ("milestone_from", "", "milestones ignore depths shallower than this"),
            ("fight_handoff", "", "losing fast: ladder (pray, quaff, stairs underfoot, verified Elbereth, retreat, "
                                  "then fight; escalate only if HP keeps falling) | escalate (hand over at once)"),
            ("crisis_turns", "", "turns the crisis ladder runs before a still-falling HP is handed over"),
            ("swarm_count", "", "this many fast, poisonous attackers in view (killer bees, soldier ants) is a swarm: "
                                "the loop leaves the level by the up stairs and says so"),
            ("swarm_xl", "", "...only below this experience level"),
            ("swarm_hold", "", "decisions before going back down to a level left because of a swarm"),
            ("fight_question", "", "1 = in a crisis, a close call between ladder steps (retreat, Elbereth, fight) "
                                   "is one decision-model question; it may pick only a legal step"),
            ("branch_points", "", "1 = pause once at each branch point (Mines, trap door or hole, depth jump, "
                                  "two down staircases)"),
            ("stall_secs", "", "...or after this many seconds of play without new squares or depth"),
            ("pickup_food", "", "1 = pick up known-safe food the hero steps on"),
            ("ranged", "", "1 = fire quivered missiles at hostiles approaching in a line"),
            ("auto", "", "1 = log escalations and play on without pausing (benchmarks only)"),
            ("report", "", "compact (reason, status, nearby monsters, last messages, an 11x21 map crop) | full "
                           "(everything and the whole screen); screen shows the whole screen any time"),
            ("pause_on", "", "which escalation codes pause: all | code,code | all,-code,-code "
                             "(help escalations)"),
            ("quiet", "", "seconds of terminal silence that end a key send"),
            ("multi_quiet", "", "seconds of silence that end a count, travel or run (they redraw on the way)"),
            ("last_prayer", "", "turn of a prayer you made by hand"),
            ("time_left", "", "seconds of play left from now, counted on this process's monotonic clock; set it at "
                              "start and again on any resume (a restart or a copied state dir forgets it)"),
            ("endgame_secs", "", "in the last this-many seconds of time_left the loop lifts depth caps, "
                                 "takes stairs at HP 50% or more and prefers any descent (one 'endgame' pause)"),
            ("tiebreak_seed", "", "reseed the loop's tie-breaking choices, so a copy explores differently while every "
                                  "safety rule stays the same (status shows the seed)")]


HELP = {
    "protocol": """PROTOCOL
You are the outer loop; this program is the inner loop. It plays routine NetHack fast and stops when judgment
is needed. Every blocking command (start, wait, resume) returns at an escalation, at game over or at --timeout:
  exit 0  paused: an ESCALATION (or BRIEFING) report follows; the keyboard is yours until you resume
  exit 2  timeout, still playing: call wait again (the line names decision-model trouble, if any)
  exit 3  game over (or the terminal socket closed)
  exit 1  no loop in --dir, not running, a setup error, or stuck (no heartbeat for 15s: stop, then start);
          start again keeps memory unless --fresh; see daemon.log
  exit 64 a usage error (unknown flag, bad --set value, send without keys): nothing happened
The first stop is a BRIEFING: role, race, kit, capabilities, settings and suggestions. Set your plan, then resume.
WHILE PAUSED (the keyboard is yours until you resume):
  screen                     look; status shows settings, effort, plan, hooks, counters and kit
  send 'keys' | send --hex 1b  play a few keys; a pending --More-- is dismissed first and reported
  repeat 'Fh' --times 6      a guarded batch: stops at HP loss, a new monster, --More-- or a prompt
  log 40                     what the loop did and why (oscillation, bans, failed targets)
  resume [options]           hand it back, with new settings, orders, plan items or hooks
Never send keys while it runs (pause first). Prayers made through the terminal are noticed on resume;
--set last_prayer=T records one explicitly.
Example turn:  resume --set risk=low --directive "avoid melee with the dwarf" --timeout 300""",
    "commands": """COMMANDS (all take --dir DIR; state lives there)
  start --socket S --decide URL [--model M --key-env VAR] [resume options]  launch; blocks until it needs you
  wait [--timeout S]                      block until the next escalation
  resume [options] [--timeout S]          continue after an escalation (options below; all optional)
  pause | stop                            pause at the next step (prints the situation) | stop the loop
  status                                  JSON: state, settings, effort, hooks, plan, counters, kit
  log [N]                                 recent inner-loop events
  screen                                  the screen as the loop sees it        e.g. screen
  send KEYS | send --hex HEX              keys while paused; prints the screen    e.g. send --hex '04 6c' (kick east)
                                          KEYS take escapes: \\r Enter, \\e Escape, \\xHH   e.g. send '#pray\\r'
  notes                                   one line per level: stairs seen or imported, holes, hazards
  --notes-out FILE / --notes-in FILE      (start or resume) keep FILE up to date with this game's level notes /
                                          use another copy's notes: travel toward stairs it saw, avoid its
                                          Mines staircase (only for the same game: the first screen must match)
  mark NAME | keys [--since NAME|T] [--raw]  name a moment | print every key sent since (source-tagged);
                                          --raw lines replay with --plan replay:FILE
  postmortem                              the death (or last crisis) in one block, also saved at game over
                                          as postmortem.txt: killer, HP trail, ladder steps, escalations
  repeat KEYS --times N [--stop-hp F] [--stop-on REGEX] [--allow-hp-loss]
                                          while paused: KEYS up to N times (1-50), stopping on HP loss, HP below
                                          F (0.5), a new monster in view, --More--/prompt, a level change, an
                                          alarming or matching message      e.g. repeat Fh --times 6
  probe --socket S --decide URL           one decision on the current screen (look-ups only)
  serve-local --socket S --nethack BIN    run nethack in a pty behind a terminal socket (testing)
  help [TOPIC]                            this map; topics: protocol commands settings modes effort plan hooks
                                          plugins escalations playbook
  --version                               print the version
Resume options: --directive TEXT  --mode M  --set k=v  --plan ITEM  --questions FILE  --plugin FILE
                --enable KEY  --disable KEY""",
    "settings": "SETTINGS (--set k=v at start or resume; status shows them)\n" + "\n".join(
        "  %-14s %-9s %s" % (k, DEFAULTS[k], doc) for k, _, doc in SETTING_DOCS),
    "modes": "MODES (--mode M resets the mode-owned keys (%s) to defaults, then applies the mode; other settings "
             "such as mines, avoid, dig and effort are kept; --set after --mode wins)\n" % ", ".join(MODE_KEYS) + "\n".join(
        "  %-8s %s" % (m, " ".join("%s=%s" % kv for kv in v.items())) for m, v in MODES.items()) +
        "\nRISK levels: " + "; ".join("%s: %s" % (k, " ".join("%s=%s" % kv for kv in v.items()))
                                      for k, v in RISK.items()),
    "effort": "EFFORT (--set effort=LEVEL; when and how much the decision model is asked)\n" + "\n".join(
        "  %-7s %s" % (k, ", ".join("%s=%s" % kv for kv in v.items())) for k, v in EFFORT.items()) +
        "\n  ask: never | danger (contested steps with an adjacent hostile or HP below half) | risky (contested steps"
        "\n       with a monster within 3, a recent hit or HP below half) | contested (every contested step)."
        "\n  cache: reuse the last answer for this many decisions while the situation is unchanged."
        "\n  Example: --set effort=high in a dangerous spot, --set effort=low while crawling corridors.",
    "plan": """PLAN QUEUE (--plan ITEM, repeatable, runs before normal play, in order)
  keys:TEXT            send literal keys once            e.g. --plan 'keys:Za.'
  hex:HEX              send bytes once                   e.g. --plan 'hex:04 6c'
  goal:stairs          go to known down stairs and descend (probes if none known)
  goal:up              go to the up stairs and climb
  goal:dig             dig down here with the pick-axe or mattock
  goal:rest[:F]        rest until HP fraction F (default 0.95); stops on a hit or a monster in view
  goal:search[:N]      search N turns here (default 15)
  goal:explore[:N]     prefer exploring for N decisions
  goal:travel:R,C      travel to screen row R, column C (1-based)
  goal:pray            pray now
  replay:FILE          send FILE's key lines (keys --raw) one per step; stops on a 15% HP loss
CRISIS ITEMS (one call each instead of hand-typed keys mid-fight)
  goal:elbereth        engrave Elbereth in the dust, read it back, re-engrave once if misspelt
  goal:quaff[:L]       quaff letter L, or the first known healing potion
  goal:retreat         stairs within 8 steps clear of attackers (and take them), else a step next to fewer
  goal:fight:DIR[:N]   attack DIR (hjklyubn) up to N times (default 4, max 20); stops and escalates when HP
                       falls 15% of max or a new hostile comes adjacent; ends when the target is gone""",
    "hooks": """HOOKS: declarative questions (--questions FILE.json at start or resume; --disable/--enable KEY)
  {"questions": [{"key": "shopkeeper",
                  "question": {"type": "noul", "instructions": "Is a shopkeeper visible on the screen?"},
                  "escalate_when": {"noul_gte": 0.8}, "when": {"every": 10}, "cooldown": 200}]}
  escalate_when: noul_gte noul_lte score_gte score_lte choice_in(list) min_confidence (all must hold)
  when: {"every": N} decisions | {"new_level": true}; none = whenever the effort level asks
  A firing hook pauses with reason hook:KEY and the report shows its answer.""",
    "plugins": """PLUGINS (--plugin FILE.py at start or resume; hook API 1; errors pause instead of crashing)
  API = 1
  def extra_questions(facts): return {key: question}           # added to this decision's call
  def on_answers(facts, answers): return None | {"escalate": "why"} | {"action": "keys to send instead"}
  def on_resume(facts, orders): ...                             # orders: directive, mode, set, enable, disable
  def on_escalation(facts, esc): return None | {"continue": true} | {"plan": [items]}
                                  # esc: {"code", "text"}; answer an escalation yourself (help escalations);
                                  # codes marked [always pauses] never reach it
  facts: dlvl hp hpmax hp_percent xl turn conditions new_level hostiles standing_on role race messages
         decisions keys mode risk orders screen state""",
    "playbook": """ESCALATION PLAYBOOK
  low HP, no safe remedy: --plan goal:quaff, goal:elbereth (@ humans and minotaurs ignore it), goal:retreat,
      or finish a weak foe with goal:fight:DIR; then --set risk=low or --mode careful
  danger / uncertain: act yourself, or resume with --directive (the model then decides contested steps)
  stalled / level exhausted: read the map; fire or throw at blockers that must not be meleed; search dead ends
      (send 15s); kick locked doors (send --hex '04 6c'); push boulders; --set dig=1 with a pick-axe; read a
      magic mapping scroll
  Gnomish Mines: gnome or dwarf heroes and strong fighters: --set mines=allow; others mines=avoid
  hunger: the loop eats pack food (cheapest first, rations kept), fresh safe corpses it made, and prays when
      Weak and safe; it pauses only with no food and no safe prayer before Fainting: find food (a shop, kills),
      or pray if the last prayer was 500+ turns ago (about 7 in 8 work; never after a failed prayer)
  unknown prompt or alarming message: answer or react (stoning: pray, or eat a lizard or acidic corpse)
  hook:KEY: a hook you loaded fired; --disable KEY to stop it""",
}


HELP["escalations"] = escalation_help()


BRIEF_SETTINGS = ("risk", "effort", "mines", "dig", "avoid", "fight_handoff", "milestone", "pause_on", "time_left",
                  "report")


def brief():
    """The short map an outer loop reads once: protocol, exit codes, commands, the settings used most."""
    rows = ["nethack-harness %s: a fast NetHack inner loop; you are the outer loop." % __version__,
            "Every blocking command (start, wait, resume) returns at a pause, at game over or at --timeout:",
            "  0 paused: a report follows, the keyboard is yours until resume | 2 still playing: wait again",
            "  3 game over (postmortem tells why) | 1 not running or stuck: stop, then start | 64 usage error",
            "Commands (all take --dir DIR):",
            "  start --socket S --decide URL|none [resume options]   wait [--timeout S]   pause   stop   status",
            "  resume [--set k=v] [--plan ITEM] [--directive TEXT] [--mode M] [--timeout S]",
            "  screen | send KEYS (\\r Enter, \\e Esc) | repeat KEYS --times N (stops at trouble) | postmortem",
            "  notes | mark NAME | keys --since NAME | help TOPIC",
            "Plan items: goal:stairs goal:up goal:rest[:F] goal:search[:N] goal:pray goal:elbereth goal:quaff[:L]",
            "  goal:retreat goal:fight:DIR[:N] goal:travel:R,C goal:explore[:N] goal:dig keys:TEXT replay:FILE",
            "Settings used most (now):"]
    for k in BRIEF_SETTINGS:
        doc = next((d for name, _, d in SETTING_DOCS if name == k), "")
        rows.append("  %-14s %-10s %s" % (k, CFG[k], doc if len(doc) <= 72 else doc[:70].rsplit(" ", 1)[0] + " ..."))
    rows.append("Topics: " + " ".join(HELP) + " | help all: everything")
    return "\n".join(rows)


def help_text(topic=None):
    if topic == "brief" or not topic:
        return brief()
    if topic == "all":
        return ("nethack-harness %s: a fast NetHack inner loop for an outer-loop agent.\n\n" % __version__ +
                "\n\n".join(HELP[t] for t in ("protocol", "commands", "settings", "effort", "modes", "plan",
                                               "hooks", "plugins", "escalations", "playbook")))
    if topic not in HELP:
        return "unknown topic %s; topics: brief all %s" % (topic, " ".join(HELP))
    return HELP[topic]
