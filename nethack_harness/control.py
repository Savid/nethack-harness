"""The background inner loop, its state directory, and the command line."""
import argparse
import base64
import fcntl
import json
import os
import pickle
import random
import re
import signal
import sys
import time

from . import __version__
from .decide import decider
from .hooks import HookError, Hooks
from .policy import GAME_OVER, Pilot
from .report import status as status_of, summary
from .settings import ALIASES, CFG, DEFAULTS, EFFORT, MODES, RISK, apply_settings, describe, validate, MODE_KEYS
from .transport import Closed, Held, Term, serve_local

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
  probe --socket S --decide URL           one decision on the current screen (look-ups only)
  serve-local --socket S --nethack BIN    run nethack in a pty behind a terminal socket (testing)
  help [TOPIC]                            this map; topics: protocol commands settings modes effort plan hooks
                                          plugins playbook
  --version                               print the version
Resume options: --directive TEXT  --mode M  --set k=v  --plan ITEM  --questions FILE  --plugin FILE
                --enable KEY  --disable KEY""",
    "settings": "SETTINGS (--set k=v at start or resume; status shows them)\n" + "\n".join(
        "  %-14s %-9s %s" % (k, DEFAULTS[k], doc) for k, _, doc in [
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
            ("search_budget", "", "search turns per level before the ladder moves on"),
            ("cap_lift", "", "seconds on a level after which the depth lead stops blocking descent"),
            ("potions", "", "1 = quaff known healing potions in emergencies"),
            ("spells", "", "1 = cast healing in emergencies (Healer)"),
            ("elbereth", "", "1 = engrave Elbereth in emergencies"),
            ("trapdoors", "", "1 = use known trap doors and holes as free descents"),
            ("probe", "", "1 = ask the game where the stairs are when none are visible"),
            ("mapping", "", "1 = read a known magic mapping scroll when a level runs out of options"),
            ("briefing", "", "1 = pause once at start with role, kit and capabilities"),
            ("quiet", "", "seconds of terminal silence that end a key send"),
            ("last_prayer", "", "turn of a prayer you made by hand")]),
    "modes": "MODES (--mode M resets the mode-owned keys (%s) to defaults, then applies the mode; other settings "
             "such as mines, avoid, dig and effort are kept; --set after --mode wins)\n" % ", ".join(MODE_KEYS) + "\n".join(
        "  %-8s %s" % (m, " ".join("%s=%s" % kv for kv in v.items())) for m, v in MODES.items()) +
        "\nRISK levels: " + "; ".join("%s: %s" % (k, " ".join("%s=%s" % kv for kv in v.items()))
                                      for k, v in RISK.items()),
    "effort": "EFFORT (--set effort=LEVEL; when and how much the decision model is asked)\n" + "\n".join(
        "  %-7s %s" % (k, ", ".join("%s=%s" % kv for kv in v.items())) for k, v in EFFORT.items()) +
        "\n  ask: never | risky (contested and monsters near or hurt) | contested | most steps."
        "\n  cache: reuse the last answer for this many decisions while the situation is unchanged."
        "\n  Example: --set effort=high in a dangerous spot, --set effort=low while crawling corridors.",
    "plan": """PLAN QUEUE (--plan ITEM, repeatable, runs before normal play, in order)
  keys:TEXT            send literal keys once            e.g. --plan 'keys:Za.'
  hex:HEX              send bytes once                   e.g. --plan 'hex:04 6c'
  goal:stairs          go to known down stairs and descend (probes if none known)
  goal:up              go to the up stairs and climb
  goal:dig             dig down here with the pick-axe or mattock
  goal:rest[:F]        rest until HP fraction F (default 0.95) or a monster appears
  goal:search[:N]      search N turns here (default 15)
  goal:explore[:N]     prefer exploring for N decisions
  goal:travel:R,C      travel to screen row R, column C (1-based)
  goal:pray            pray now""",
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
  facts: dlvl hp hpmax hp_percent xl turn conditions new_level hostiles standing_on role race messages
         decisions keys mode risk orders screen state""",
    "playbook": """ESCALATION PLAYBOOK
  low HP, no safe remedy: quaff a potion, cast healing (Za.), engrave Elbereth (E - Elbereth; @ humans and
      minotaurs ignore it), go upstairs, or finish a weak foe; then --set risk=low or --mode careful
  danger / uncertain: act yourself, or resume with --directive (the model then decides contested steps)
  stalled / level exhausted: read the map; fire or throw at blockers that must not be meleed; search dead ends
      (send 15s); kick locked doors (send --hex '04 6c'); push boulders; --set dig=1 with a pick-axe; read a
      magic mapping scroll
  Gnomish Mines: gnome or dwarf heroes and strong fighters: --set mines=allow; others mines=avoid
  hunger with no food: pray if the last prayer was 900+ turns ago (never after a failed prayer)
  unknown prompt or alarming message: answer or react (stoning: pray, or eat a lizard or acidic corpse)
  hook:KEY: a hook you loaded fired; --disable KEY to stop it""",
}


def help_text(topic=None):
    if topic:
        if topic not in HELP:
            return "unknown topic %s; topics: %s" % (topic, " ".join(HELP))
        return HELP[topic]
    return ("nethack-harness %s: a fast NetHack inner loop for an outer-loop agent.\n\n" % __version__ +
            "\n\n".join(HELP[t] for t in ("protocol", "commands", "settings", "effort", "modes", "plan", "hooks",
                                           "plugins", "playbook")))


class Store:
    def __init__(self, path):
        self.dir = os.path.abspath(path)
        os.makedirs(self.dir, exist_ok=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, obj):
        self.publish(name, json.dumps(obj, default=str))

    def publish(self, name, data):
        """Write a complete file atomically; on failure (disk full) leave no temporary file behind."""
        tmp = self.path("%s.%d.%d.tmp" % (name, os.getpid(), random.randrange(1 << 30)))
        try:
            with open(tmp, "w") as f:
                f.write(data)
            os.replace(tmp, self.path(name))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read(self, name):
        try:
            with open(self.path(name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def text(self, name, value=None):
        if value is None:
            try:
                with open(self.path(name)) as f:
                    return f.read()
            except OSError:
                return ""
        self.publish(name, value)
        return value


def save_pilot(store, p):
    """Returns None, or the error when the state directory cannot be written. A memory that could not be
    updated is removed, so a restart never resumes from stale knowledge."""
    kept = (p.term, p.log, p.decide, p.hooks)
    p.term = p.log = p.decide = p.hooks = None
    try:
        with open(store.path("memory.tmp"), "wb") as f:
            pickle.dump((p, dict(CFG), MEMORY), f)
        os.replace(store.path("memory.tmp"), store.path("memory.pkl"))
        return None
    except OSError as e:
        for name in ("memory.tmp", "memory.pkl"):
            try:
                os.unlink(store.path(name))
            except OSError:
                pass
        return "%s: %s" % (type(e).__name__, e.strerror or e)
    finally:
        p.term, p.log, p.decide, p.hooks = kept


LOG_CAP = 32 << 20


def trim_logs(store, p):
    """Keep log.jsonl and daemon.log bounded: the log rotates once to log.jsonl.1; daemon.log is cut."""
    try:
        if p.log and p.log.tell() > LOG_CAP:
            p.log.close()
            os.replace(store.path("log.jsonl"), store.path("log.jsonl.1"))
            p.log = open(store.path("log.jsonl"), "a")
        if os.fstat(2).st_size > LOG_CAP // 4 and os.path.samefile("/proc/self/fd/2", store.path("daemon.log")):
            os.ftruncate(2, 0)
    except (OSError, ValueError):
        pass


def load_hooks(hooks, questions=(), plugins=(), enable=(), disable=()):
    problems = []
    for path in questions or ():
        try:
            hooks.load_questions(path)
        except Exception as e:
            problems.append("questions %s: %s: %s" % (path, type(e).__name__, e))
    for path in plugins or ():
        try:
            hooks.load_plugin(path)
        except Exception as e:
            problems.append("plugin %s: %s: %s" % (path, type(e).__name__, e))
    hooks.disabled |= set(disable or ())
    hooks.disabled -= set(enable or ())
    return problems


MEMORY = "%s/m2" % __version__      # bump the suffix when the pickled pilot changes shape


def commit():
    """The build's commit: stamped into release zipapps, or a COMMIT file beside the entry script or package."""
    try:
        from ._commit import COMMIT
        return COMMIT
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(here), here):
        try:
            with open(os.path.join(d, "COMMIT")) as f:
                return f.read().strip()[:40] or "unknown"
        except OSError:
            continue
    return "unknown"


def daemon(args):
    store = Store(args.dir)
    lock = open(store.path("daemon.lock"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one inner loop per state directory
    except OSError:
        print("another inner loop holds %s" % store.path("daemon.lock"), file=sys.stderr)
        return 1
    cfg = store.read("config.json")
    status = {"state": "starting", "pid": os.getpid(), "escalation": store.read("status.json").get("escalation", 0),
              "version": __version__, "commit": commit()}
    store.write("status.json", status)
    term = Term(cfg["socket"])
    for _ in range(600):
        try:
            term.sync()
            break
        except Held:
            time.sleep(0.5)
    decide = decider(cfg.get("decide"), cfg.get("model"), os.environ.get(cfg.get("key_env") or "", None))
    p = None
    if not cfg.get("fresh"):
        try:
            with open(store.path("memory.pkl"), "rb") as f:
                p, saved, version = pickle.load(f)
            if version != MEMORY:
                raise ValueError("memory from another version")
            CFG.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError, ImportError, TypeError):
            p = None
    problems = []
    try:
        apply_settings(cfg.get("mode"), cfg.get("set"))
    except ValueError as e:
        problems.append(str(e))
    if p is None:
        p = Pilot(term, decide)
    if CFG["last_prayer"] >= 0:
        p.last_prayer, CFG["last_prayer"] = CFG["last_prayer"], -1
    p.term, p.decide, p.log, p.hooks = term, decide, open(store.path("log.jsonl"), "a"), Hooks()
    p.hooks.log_path = store.path("hooks.log")
    p.pending, p.progress, p.calm_until = None, p.decisions, p.decisions + CFG["calm"]
    if cfg.get("directive") is not None:
        p.directive = cfg["directive"]
    p.plan.extend(cfg.get("plan") or [])
    p.rng = random.Random(cfg.get("seed") or os.getpid())
    kept = store.read("hooks.json") or {}
    problems += load_hooks(p.hooks, (kept.get("questions") or []) + cfg.get("questions", []),
                           (kept.get("plugins") or []) + cfg.get("plugins", []), cfg.get("enable"),
                           (kept.get("disabled") or []) + (cfg.get("disable") or []))
    remember_hooks(store, cfg)
    status["state"] = "running"
    done = store.read("control.json").get("seq", 0)
    paused = None
    ended_at = None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def publish():
        status.update(keys=p.keys, decisions=p.decisions, model_calls=p.calls, reused_answers=p.reused,
                      max_dlvl=p.max_dl, model_ms=int(1000 * p.mtime / max(1, p.calls)), done=done,
                      last=next((h["text"] for h in reversed(p.hist) if h["kind"] == "act"), "")[:120],
                      settings=dict(CFG), details=status_of(p),
                      breaker=max(0, int(p.breaker_until - time.time())), beat=time.time(),
                      home=os.path.realpath(store.dir))
        beat["at"] = time.time()
        try:
            store.write("status.json", status)
        except OSError:
            pass                  # disk full: wait sees the heartbeat go stale
        trim_logs(store, p)

    beat = {"at": 0.0}

    inbox = {"path": cfg.get("inbox"), "offset": 0}

    def read_inbox():
        if not inbox["path"]:
            return []
        try:
            with open(inbox["path"]) as f:
                f.seek(inbox["offset"])
                new = f.read()
                inbox["offset"] = f.tell()
        except OSError:
            return []
        lines = [x.strip() for x in new.splitlines() if x.strip()]
        for x in lines:
            p.note("inbox", x[:200])
        return lines

    def pause(reason, ended=False):
        nonlocal paused, ended_at
        paused = reason
        p.escs += 1
        notes = read_inbox()
        try:
            text = ("".join("INBOX: %s\n" % x for x in notes[-10:])) + summary(p, reason)
        except Exception as e:
            text = "ESCALATION: %s (report failed: %s)" % (reason, e)
        store.text("escalation.txt", text)
        status.update(state="ended" if ended else "paused", reason=reason, escalation=status["escalation"] + 1)
        if ended:
            ended_at = time.time()
        p.note("pause", reason)
        save(p)

    def save(p):
        err = save_pilot(store, p)
        if err and not disk["warned"]:
            disk["warned"] = err
        elif not err:
            disk["warned"] = None

    disk = {"warned": None, "told": None}

    def handle(c):
        nonlocal paused
        cmd = c.get("cmd")
        if cmd == "stop":
            status.update(state="stopped")
            publish()
            raise SystemExit(0)
        if cmd == "pause" and not paused:
            pause("paused on request")
        elif cmd == "resume" and status["state"] == "paused":
            if c.get("answering") not in (None, status["escalation"]):
                return            # a resume for an older report: the outer loop has not read the newest one
            problems = []
            if c.get("directive") is not None:
                p.directive = c["directive"]
            try:
                apply_settings(c.get("mode"), c.get("set"))
            except (ValueError, TypeError) as e:
                problems.append(str(e))
            if CFG["last_prayer"] >= 0:
                p.last_prayer, CFG["last_prayer"] = CFG["last_prayer"], -1
            p.plan.extend(c.get("plan") or [])
            problems += load_hooks(p.hooks, c.get("questions"), c.get("plugins"), c.get("enable"),
                                   c.get("disable"))
            remember_hooks(store, c)
            if problems:
                pause("resume problem: " + "; ".join(problems))
                return
            try:
                p.hooks.resumed({"orders": p.directive, "mode": CFG["mode"], "decisions": p.decisions},
                                {k: c.get(k) for k in ("directive", "mode", "set", "enable", "disable")})
            except HookError as e:
                p.note("hook_error", str(e))
            seen, term.seen = getattr(term, "seen", ""), ""
            turn = term.view().st.get("turn")
            if "You begin praying" in seen and turn is not None and CFG["last_prayer"] < 0:
                p.last_prayer = turn          # a prayer made by hand while paused
                p.note("prayer", "recorded a prayer made by hand at about T%d" % turn)
            if re.search(r"You feel that .+ is displeased|Thou hast angered me|You feel guilty", seen):
                p.prayer_broken = True
            paused, p.pending = None, None
            p.progress, p.calm_until = p.decisions, p.decisions + CFG["calm"]
            p.note("resume", "orders=%r %s plan=%s" % (p.directive[:300], describe(), list(p.plan)))
            status.update(state="running", reason=None)
            term.sync()
        elif cmd in ("send", "screen"):
            if cmd == "send" and not paused:
                reply = "refused: the inner loop is running; pause it first\n"
            else:
                note = ""
                if cmd == "send":
                    keys = base64.b64decode(c.get("keys", ""))
                    term.poll()
                    for _ in range(5):          # a pending --More-- would swallow the keys
                        w = term.view()
                        if not w.more:
                            break
                        note += "[dismissed --More--: %s]\n" % w.msg.replace("--More--", "").strip()[:160]
                        term.send(b" ")
                    term.send(keys)
                    p.note("manual", repr(keys)[:80])
                else:
                    term.poll()
                reply = note + term.view().text_screen() + "\n"
            store.text("reply-%d.txt" % c["seq"], reply)

    publish()
    if problems:
        pause("setup problem: " + "; ".join(problems))
        publish()
    try:
        while True:
            if time.time() - beat["at"] > 2:
                publish()                 # the heartbeat that lets wait tell a stuck loop from a busy one
            for c in take_commands(store, done):
                done = c["seq"]
                try:
                    handle(c)
                except (SystemExit, KeyboardInterrupt):
                    raise
                except Closed as e:
                    store.text("reply-%d.txt" % c["seq"], "game closed: %s\n" % e)
                    if status["state"] != "ended":
                        pause(GAME_OVER, ended=True)
                except Exception as e:   # a bad command never kills the loop
                    store.text("reply-%d.txt" % c["seq"], "error: %s: %s\n" % (type(e).__name__, e))
                    p.note("command_error", "%s: %s: %s" % (c.get("cmd"), type(e).__name__, e))
                publish()
            if status["state"] == "ended" and ended_at and time.time() - ended_at > 600:
                return 0          # game over long ago: free the lock
            if status["state"] == "ended" or paused:
                try:
                    term.poll()   # stay in sync while the outer loop plays by hand
                except Exception:
                    pass
                time.sleep(0.2)
                continue
            try:
                reason = p.step()
            except Held:
                status["held"] = True
                publish()
                time.sleep(0.5)
                continue
            except Closed:
                reason = GAME_OVER
            except Exception as e:
                reason = "inner loop error: %s: %s" % (type(e).__name__, str(e)[:200])
            status["held"] = False
            if reason:
                pause(reason, ended=reason == GAME_OVER)
            if reason or p.decisions % 5 == 0:
                publish()
            if p.decisions and p.decisions % 50 == 0:
                save(p)
            if disk["warned"] and disk["told"] != disk["warned"] and not paused:
                disk["told"] = disk["warned"]
                pause("state dir not writable (%s): free space in %s, then resume" % (disk["warned"], store.dir))
                publish()
    finally:
        save_pilot(store, p)


def remember_hooks(store, c):
    """Keep loaded hook files and enable/disable choices so a restarted loop restores them."""
    h = store.read("hooks.json") or {"questions": [], "plugins": [], "disabled": []}
    h["questions"] += [x for x in c.get("questions") or [] if x not in h["questions"]]
    h["plugins"] += [x for x in c.get("plugins") or [] if x not in h["plugins"]]
    h["disabled"] = sorted((set(h["disabled"]) | set(c.get("disable") or [])) - set(c.get("enable") or []))
    store.write("hooks.json", h)


def spawn_daemon(store):
    """Double-fork so the inner loop leaves the caller's process tree: an agent tool that kills a timed-out
    command and all its descendants must not take the loop with it."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    cmd = [sys.executable, "-m", "nethack_harness", "--dir", store.dir, "_daemon"]
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            null = os.open(os.devnull, os.O_RDWR)
            log = os.open(store.path("daemon.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(null, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            os.closerange(3, 256)
            os.execve(sys.executable, cmd, env)
        finally:
            os._exit(127)
    os.waitpid(pid, 0)


STUCK_SECS = 15


def stuck(st):
    """Seconds since the daemon's last heartbeat, when that is too long; 0 otherwise (older daemons have none)."""
    beat = st.get("beat")
    if not isinstance(beat, (int, float)) or st.get("state") in ("stopped", "ended"):
        return 0
    age = time.time() - beat
    return int(age) if age > STUCK_SECS else 0


def ours(store, st):
    """status.json copied from another directory names a process that serves that directory, not this one."""
    home = st.get("home")
    return not home or home == os.path.realpath(store.dir)


def alive(status):
    try:
        os.kill(int(status["pid"]), 0)
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


def wait(store, timeout, since):
    end = time.time() + timeout
    while time.time() < end:
        st = store.read("status.json")
        if st.get("escalation", 0) > since and st.get("state") in ("paused", "ended"):
            print(store.text("escalation.txt"))
            if st["state"] == "paused":
                print("\n[paused: the keyboard is yours (send, screen). Continue with resume; help for options]")
                return 0
            print("\n[game over: %s]" % st.get("reason"))
            return 3
        if not st:
            print("no inner loop in %s; start it" % store.dir)
            return 1
        if st.get("state") == "stopped" or not alive(st) or not ours(store, st):
            print("inner loop not running (state: %s); see %s" % (st.get("state"), store.path("daemon.log")))
            return 1
        stale = stuck(st)
        if stale:
            print("inner loop stuck: no heartbeat for %ds (state %s, last: %s); stop it (stop ends a stuck loop) "
                  "and start again" % (stale, st.get("state"), st.get("last")))
            return 1
        time.sleep(0.2)
    st = store.read("status.json")
    if st.get("escalation", 0) > since and st.get("state") in ("paused", "ended"):
        return wait(store, 0.5, since)
    if st.get("held"):
        print("game input is held for now (waiting for the game to accept keys); call wait again")
        return 2
    d = st.get("details") or {}
    health = ""
    if st.get("breaker") or d.get("model_errors"):
        health = "; decision model: %s errors%s, last: %s" % (
            d.get("model_errors"), ", rules only for %ss" % st["breaker"] if st.get("breaker") else "",
            d.get("last_model_error") or "-")
    recent = d.get("recent_ms") or []
    if recent and sorted(recent)[len(recent) // 2] > 1000:
        health += "; slow decision calls (recent ms: %s)" % ",".join(str(x) for x in recent)
    print("still running%s, call wait again: %s keys, %s decisions, %s model calls (%s ms), max Dlvl %s, "
          "effort %s, last: %s%s" % (" fine" if not health else "", st.get("keys"), st.get("decisions"),
                                     st.get("model_calls"), st.get("model_ms"), st.get("max_dlvl"),
                                     (st.get("settings") or {}).get("effort"), st.get("last"), health))
    return 2


def control(store, cmd, **kw):
    """Queue one command for the daemon. Commands are appended under a lock so parallel callers never lose
    one; the daemon handles them in order."""
    with open(store.path("control.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        q = store.read("control.json")
        seq = q.get("seq", 0) + 1
        pending = [c for c in q.get("queue", []) if c["seq"] > q.get("done", 0)]
        kw.update(cmd=cmd, seq=seq)
        store.write("control.json", {"seq": seq, "done": q.get("done", 0), "queue": pending + [kw]})
    return seq


def take_commands(store, done):
    q = store.read("control.json")
    return [c for c in q.get("queue", []) if c["seq"] > done]


def parse_sets(items):
    out = {}
    for item in items or []:
        if "=" not in item:
            print("--set wants k=v, got %r" % item, file=sys.stderr)
            raise SystemExit(64)
        k, v = item.split("=", 1)
        k = ALIASES.get(k, k)
        if k not in DEFAULTS:
            print("unknown setting %s (known: %s)" % (k, ", ".join(sorted(DEFAULTS))), file=sys.stderr)
            raise SystemExit(64)
        out[k] = v
    try:
        validate(out)
    except ValueError as e:
        print("--set: %s" % e, file=sys.stderr)
        raise SystemExit(64)
    return out


def add_resume_options(p):
    p.add_argument("--directive", help="orders for the decision model; while set it decides contested steps ('' clears)")
    p.add_argument("--mode", choices=sorted(MODES))
    p.add_argument("--set", action="append", default=[], metavar="K=V", help="a setting, e.g. risk=low (help settings)")
    p.add_argument("--plan", action="append", default=[], metavar="ITEM", help="queue a plan item (help plan)")
    p.add_argument("--questions", action="append", default=[], metavar="FILE", help="hook questions (help hooks)")
    p.add_argument("--plugin", action="append", default=[], metavar="FILE", help="hook plugin (help plugins)")
    p.add_argument("--enable", action="append", default=[], metavar="KEY")
    p.add_argument("--disable", action="append", default=[], metavar="KEY")
    p.add_argument("--timeout", type=float, default=100, help="seconds to block (default 100)")


class Parser(argparse.ArgumentParser):
    def error(self, message):            # usage errors must never look like "2: still running fine"
        self.print_usage(sys.stderr)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        raise SystemExit(64)


def main(argv=None):
    ap = Parser(prog="nethack_harness.py", description="Fast NetHack inner loop for an outer-loop "
                                 "agent. Run `help` for the capability map.")
    ap.add_argument("--dir", default=os.environ.get("NETHACK_HARNESS_DIR", ".nethack-harness"),
                    help="state directory shared by the inner loop and these commands")
    ap.add_argument("--version", action="version", version="%s (commit %s)" % (__version__, commit()))
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    s = sub.add_parser("start", help="launch the inner loop in the background; block until it needs you")
    s.add_argument("--socket", required=True, help="terminal socket: path, unix:///path or http://host:port")
    s.add_argument("--decide", required=True, help="SystemOne-compatible decision endpoint URL, or 'none'")
    s.add_argument("--model", help="model name to send (some endpoints require one)")
    s.add_argument("--key-env", help="environment variable holding a bearer key for the decision endpoint")
    s.add_argument("--fresh", action="store_true", help="ignore memory saved by an earlier inner loop")
    s.add_argument("--seed", help="seed for tie-breaking choices")
    s.add_argument("--inbox", help="a file another process may append notes to; new lines head the next report")
    add_resume_options(s)
    add_resume_options(sub.add_parser("resume", help="continue after an escalation; blocks like wait"))
    w = sub.add_parser("wait", help="block until the next escalation")
    w.add_argument("--timeout", type=float, default=100)
    sub.add_parser("pause", help="pause at the next step (prints the situation)")
    sub.add_parser("stop", help="stop the inner loop (memory is kept)")
    sub.add_parser("status", help="state, settings, effort, hooks and counters as JSON")
    lg = sub.add_parser("log", help="recent inner-loop events")
    lg.add_argument("n", nargs="?", type=int, default=30)
    sub.add_parser("screen", help="the screen as the inner loop sees it")
    sd = sub.add_parser("send", help="send keys while paused; prints the resulting screen")
    sd.add_argument("keys", nargs="?")
    sd.add_argument("--hex", help="bytes as hex, e.g. 1b for Escape, 0d for Enter")
    pr = sub.add_parser("probe", help="one decision on the current screen (look-ups only)")
    pr.add_argument("--socket", required=True)
    pr.add_argument("--decide", required=True)
    pr.add_argument("--model")
    pr.add_argument("--key-env")
    lo = sub.add_parser("serve-local", help="run nethack in a pty behind a terminal socket, for testing")
    lo.add_argument("--socket", required=True)
    lo.add_argument("--nethack", default="nethack")
    lo.add_argument("--playground", help="passed to nethack as -d DIR")
    lo.add_argument("--options", help="NETHACKOPTIONS value, e.g. 'seed:42,color,!autopickup'")
    lo.add_argument("args", nargs="*", help="extra nethack arguments (after --)")
    hp = sub.add_parser("help", help="the capability map; help TOPIC for one section")
    hp.add_argument("topic", nargs="?")
    sub.add_parser("_daemon")
    a = ap.parse_args(argv)

    if a.cmd == "help":
        print(help_text(a.topic))
        print("\nversion %s, commit %s" % (__version__, commit()))
        return 0
    if a.cmd == "serve-local":
        return serve_local(a)
    if a.cmd == "probe":
        term = Term(a.socket)
        term.sync()
        pilot = Pilot(term, decider(a.decide, a.model, os.environ.get(a.key_env or "", None)))
        pilot.options = pilot.briefed = True
        v = pilot.view()
        if not v.normal:
            print("not at a normal command prompt: %s" % v.msg)
            return 1
        c = pilot.context(v)
        acts = pilot.actions(term.view(), c)
        ans, took = pilot.ask(term.view(), c, acts)
        print(json.dumps({"options": {x.key: x.desc for x in acts}, "rule": acts[0].key, "answers": ans,
                          "ms": int(took * 1000)}, indent=1))
        return 0
    sets = parse_sets(getattr(a, "set", None))       # bad settings are usage errors (64) before anything runs
    try:
        store = Store(a.dir)
    except OSError as e:
        print("cannot use --dir %s: %s" % (a.dir, e.strerror or e))
        return 1
    if a.cmd == "_daemon":
        with open(store.path("daemon.log"), "a") as log:
            os.dup2(log.fileno(), 2)
        return daemon(a)
    if a.cmd == "start":
        st = store.read("status.json")
        if st.get("state") in ("running", "paused") and alive(st) and ours(store, st):
            print("already running (%s); use wait, resume or stop" % st["state"])
            return 1
        cfg = {"socket": a.socket, "decide": a.decide, "model": a.model, "key_env": a.key_env, "fresh": a.fresh,
               "seed": a.seed, "inbox": os.path.abspath(a.inbox) if a.inbox else None, "directive": a.directive, "mode": a.mode, "set": sets, "plan": a.plan,
               "questions": [os.path.abspath(x) for x in a.questions],
               "plugins": [os.path.abspath(x) for x in a.plugin], "enable": a.enable, "disable": a.disable}
        store.write("config.json", cfg)
        if os.path.exists(store.path("status.json")):
            os.unlink(store.path("status.json"))
        store.write("control.json", {"seq": 0, "done": 0, "queue": []})
        if os.path.exists(store.path("hooks.json")) and a.fresh:
            os.unlink(store.path("hooks.json"))
        try:
            with open(store.path("daemon.lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            print("an inner loop is already running for %s; use wait, resume or stop" % store.dir)
            return 1
        spawn_daemon(store)
        for _ in range(150):
            if store.read("status.json"):
                break
            time.sleep(0.1)
        else:
            print("the inner loop did not start; last lines of %s:" % store.path("daemon.log"))
            print("".join(open(store.path("daemon.log")).readlines()[-8:]))
            return 1
        return wait(store, a.timeout, 0)
    st = store.read("status.json")
    if a.cmd == "wait":
        return wait(store, a.timeout, st.get("escalation", 0) - (1 if st.get("state") in ("paused", "ended") else 0))
    if a.cmd == "status":
        print(json.dumps(dict(st, alive=alive(st)), default=str))
        return 0
    if a.cmd == "log":
        try:
            with open(store.path("log.jsonl")) as f:
                lines = f.readlines()[-a.n:]
        except OSError:
            return 1
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            print(d.get("step"), d.get("kind"), d.get("text", "")[:120], d.get("p", ""))
        return 0
    if not st:
        print("no inner loop in %s; start it" % store.dir)
        return 1
    if not alive(st) or not ours(store, st):
        print("inner loop not running; use start")
        return 1
    if a.cmd == "pause" and st.get("state") == "paused":
        return wait(store, 1, st.get("escalation", 0) - 1)
    if a.cmd == "stop" and stuck(st):
        pid = int(st["pid"])
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not alive(st):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
        st.update(state="stopped")
        store.write("status.json", st)
        print("stopped a stuck inner loop (pid %d)" % pid)
        return 0
    if a.cmd in ("pause", "stop"):
        control(store, a.cmd)
        if a.cmd == "pause":
            return wait(store, 15, st.get("escalation", 0))
        print("stop requested")
        return 0
    if a.cmd == "resume":
        if st.get("state") != "paused":
            print("not paused (state: %s); use wait" % st.get("state"))
            return 1
        control(store, "resume", answering=st.get("escalation"), directive=a.directive, mode=a.mode,
                set=sets, plan=a.plan,
                questions=[os.path.abspath(x) for x in a.questions], plugins=[os.path.abspath(x) for x in a.plugin],
                enable=a.enable, disable=a.disable)
        return wait(store, a.timeout, st.get("escalation", 0))
    if a.cmd in ("send", "screen"):
        keys = b""
        if a.cmd == "send":
            if (a.keys is None) == (a.hex is None):
                print("send wants keys or --hex")
                return 64
            try:
                keys = a.keys.encode() if a.keys is not None else bytes.fromhex(a.hex)
            except ValueError:
                print("send --hex wants hex bytes, e.g. '04 6c'")
                return 64
            if not 0 < len(keys) <= 4096:
                print("send takes 1 to 4096 bytes")
                return 64
        seq = control(store, a.cmd, keys=base64.b64encode(keys).decode())
        for _ in range(150):
            reply = store.text("reply-%d.txt" % seq)
            if reply:
                os.unlink(store.path("reply-%d.txt" % seq))
                print(reply, end="")
                return 0 if not reply.startswith("error") else 1
            time.sleep(0.1)
        print("no reply from the inner loop")
        return 1
    return 0
