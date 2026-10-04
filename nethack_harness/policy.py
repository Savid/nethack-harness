"""The inner loop. Rules own safety and mechanics; the decision model judges contested steps; anything tricky is
escalated to the outer loop. step() plays one decision and returns an escalation reason or None."""
import collections
import json
import random
import time

from .escalation import BY_CODE, classify, parse_pause_on
from .hooks import HOOK_API, HookError, Hooks
from .settings import CFG
from .perceive import Perception
from .messages import Messages
from .candidates import Candidates
from .food import Food
from .crisis import Crisis
from .execute import Execution
from .modelview import ModelView
from .stepper import Stepper
from .arbiter import Arbiter
from .base import GAME_OVER, RACE_MONSTER, Act, Dead, Hard, escape  # noqa: F401  (re-exported)


class Pilot(Perception, Messages, Food, Candidates, Crisis, Execution, ModelView, Stepper, Arbiter):
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
        self.kills = []                  # (dlvl, pos, monster, turn or None if it never rots): corpses we made
        self.poison_res = False          # "You feel healthy": poisonous corpses become safe
        self.tin_smell = None            # what the last opened tin smelled like
        self.eating_at = None            # the square of the corpse being eaten
        self.fainting_since = None       # turn the current Fainting began: the starvation clock
        self.hist, self.msgs = collections.deque(maxlen=80), collections.deque(maxlen=12)
        self.msg_log = collections.deque(maxlen=20)      # longer message memory, for the postmortem
        self.sent = collections.deque(maxlen=30)         # the last keys sent, for the postmortem
        self.hp_trail = collections.deque(maxlen=40)     # (turn, hp, hpmax), for the postmortem
        self.last_crisis = None                          # the most recent crisis ladder, kept after it ends
        self.inv_complete = False                        # a whole inventory menu has been read at least once
        self.spells = {}                                 # name -> (letter, level, failure %), from the + menu
        self.last_code = None
        self.msg_count = 0                               # messages seen so far (no-op detection)
        self.farlook_retries = {}                        # square -> looks that named no monster there                            # the code of the latest escalation (escalation.py)
        self.hunger_noted = None
        self.last_st = {}                                # the last status line read on a normal screen
        self.keys = self.decisions = self.calls = self.escs = 0
        self.mtime, self.t0, self.mark, self.progress, self.progress_turn = 0.0, time.time(), None, 0, 0
        self.directive, self.max_dl, self.prev_dl = "", 0, None
        self.obst, self.hostiles, self.pending, self.last_act, self.last_try = [], [], None, None, None
        self.low_noted, self.turn, self.bad_screens, self.hit_turn = None, None, 0, -99
        self.calm_until, self.hook_answers, self.seen_levels = 0, {}, set()
        self.visits, self.outcomes = collections.deque(maxlen=10), collections.deque(maxlen=10)
        self.move_ban_until = self.boost_until = self.frozen = self.engulf_sends = 0
        self.unknown_prompts, self.elbereth_at, self.blind_since = {}, None, None
        self.last_model_esc = -1e9
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
        self.progress_time = self.clock()
        self.rng = random.Random(0)
        self.lookup = None                 # optional p -> farlook text, instead of asking the game
        self.tiebreak = None               # the tie-break seed set with --set tiebreak_seed
        self.journal, self.key_index, self.key_source = None, 0, "loop"   # the key journal (record)
        self.marks = {}                    # name -> {"i", "turn", "dlvl"} (H mark)
        self.replay_hp = None              # HP when a plan replay began
        self.plan_tries = 0                # travel attempts for the current goal:stairs / goal:up
        self.fled_from = {}                # dlvl -> [(monster, pos)] that drove the hero up its stairs
        self.anchor = None                 # fingerprint of this game's first screen (level notes)
        self.endgame_noted = False         # the endgame escalation was given
        self.time_budget = None            # (seconds left, monotonic reference, process identity): time_left
        self.last_seen = None              # (dlvl, turn) at the last look, for turns spent per level
        self.log = None
        self.hooks = Hooks()

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

    def record(self, keys, source):
        """The key journal: every send, with its source (loop, plan, plugin, hand), turn and level."""
        text = keys if isinstance(keys, str) else keys.decode("latin-1")
        self.sent.append(text)
        self.key_index += 1
        if self.journal is not None:
            st = self.last_st
            try:
                self.journal.write(json.dumps({"i": self.key_index, "t": st.get("turn"), "dl": st.get("dlvl"),
                                               "src": source, "k": escape(text)}) + "\n")
                self.journal.flush()
            except (OSError, ValueError):
                pass

    def reseed(self, seed):
        self.rng, self.tiebreak = random.Random(seed), seed
        self.note("reseed", "tie-break seed %d" % seed)

    def send(self, keys):
        if self.term.view().dead:
            raise Dead()                 # never type into "You die..." or the end-of-game questions
        self.record(keys, self.key_source)
        self.term.send(keys)
        self.keys += 1

    def esc(self, reason, **kw):
        """Every escalation passes here. Returns the reason to pause on, or None to play on: when its code is
        silenced by pause_on, when an on_escalation plugin answers it, or in auto mode (benchmarks)."""
        code = classify(reason)
        self.last_code = code
        self.note("escalate", reason, turn=self.turn, code=code, **kw)
        if CFG["auto"]:
            return None
        if code not in parse_pause_on(CFG["pause_on"]):
            self.note("silenced", "%s (pause_on): %s" % (code, reason[:120]))
            return None
        if self.hooks is not None and BY_CODE.get(code) is not None and BY_CODE[code].silenceable:
            try:
                answer = self.hooks.escalation(self.esc_facts(), {"code": code, "text": reason})
            except HookError as e:
                self.note("hook_error", str(e))
                answer = None
            if answer:
                self.note("hook_answer", "%s: %s" % (code, answer))
                for item in answer.get("plan") or []:
                    try:
                        self.check_plan(str(item))
                        self.plan.append(str(item))
                    except ValueError as e:
                        self.note("hook_error", "on_escalation plan item %r: %s" % (item, e))
                return None
        return reason

    @staticmethod
    def map_sig(v):
        """A fingerprint of the map rows, to tell "nothing happened" from "something changed"."""
        return hash(tuple(v.rows[1:22]))

    def esc_facts(self):
        v = self.term.view()
        st = v.st if v.st.get("dlvl") is not None else self.last_st
        return {"api": HOOK_API, "dlvl": st.get("dlvl"), "hp": st.get("hp"), "hpmax": st.get("hpmax"),
                "xl": st.get("xl"), "turn": st.get("turn"), "decisions": self.decisions, "conditions": list(v.cond),
                "orders": self.directive, "plan": list(self.plan)}
