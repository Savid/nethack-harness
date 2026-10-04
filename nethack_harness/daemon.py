"""The background inner loop: start-up, the command handler, pauses and the step loop."""
import base64
import fcntl
import os
import pickle
import random
import re
import signal
import sys
import time

from . import __version__
from .decide import decider
from .escalation import classify
from .hooks import HookError, Hooks
from . import notes as notes_mod
from .policy import GAME_OVER, Pilot
from .report import live_settings, postmortem, status as status_of, summary
from .settings import CFG, DEFAULTS, apply_settings, describe
from .transport import Closed, Held, Term
from .manual import guarded_repeat
from .store import MEMORY, Store, remember_hooks, save_pilot, sync_notes, take_commands, trim_logs


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
    p.journal = open(store.path("keys.jsonl"), "a")
    p.pending, p.progress, p.calm_until = None, p.decisions, p.decisions + CFG["calm"]
    p.start_clock()                       # time while the loop was stopped is not play time
    p.progress_time = p.clock()
    if cfg.get("directive") is not None:
        p.directive = cfg["directive"]
    p.plan.extend(cfg.get("plan") or [])
    p.rng = random.Random(cfg.get("seed") or os.getpid())
    if CFG["tiebreak_seed"] >= 0:
        p.reseed(CFG["tiebreak_seed"])
        CFG["tiebreak_seed"] = -1
    if CFG["time_left"] >= 0:
        p.set_time_left(CFG["time_left"])
        CFG["time_left"] = -1
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
                      settings=live_settings(p), details=status_of(p),
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
    level_notes = {"notes_out": cfg.get("notes_out") or store.path("notes.json"), "notes_in": cfg.get("notes_in"),
                   "mtime": None,
                   "written": None}

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
        p.stop_clock()
        term.seen = ""                           # what follows is the outer loop's play (hand prayers, etc.)
        p.escs += 1
        notes = read_inbox()
        try:
            text = ("".join("INBOX: %s\n" % x for x in notes[-10:])) + summary(p, reason)
        except Exception as e:
            text = "ESCALATION: %s (report failed: %s)" % (reason, e)
        store.text("escalation.txt", text)
        status.update(state="ended" if ended else "paused", reason=reason, code=classify(reason),
                      escalation=status["escalation"] + 1)
        if ended:
            ended_at = time.time()
            sync_notes(store, p, level_notes, ended=True)
            try:
                store.text("postmortem.txt", postmortem(p, reason))
            except Exception as e:      # a report must never stop the game-over bookkeeping
                p.note("command_error", "postmortem: %s" % e)
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
            if CFG["tiebreak_seed"] >= 0:
                p.reseed(CFG["tiebreak_seed"])
                CFG["tiebreak_seed"] = -1
            if CFG["time_left"] >= 0:
                p.set_time_left(CFG["time_left"])
                CFG["time_left"] = -1
            p.plan.extend(c.get("plan") or [])
            for k in ("notes_out", "notes_in"):
                if c.get(k):
                    level_notes[k], level_notes["mtime"] = c[k], None
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
            seen, term.seen = term.seen, ""
            turn = term.view().st.get("turn")
            if "You begin praying" in seen and turn is not None:
                p.last_prayer = turn          # a prayer made by hand while paused
                p.note("prayer", "recorded a prayer made by hand at about T%d" % turn)
            if re.search(r"You feel that .+ is displeased|Thou hast angered me|You feel guilty", seen):
                p.prayer_broken = True
            paused, p.pending = None, None
            p.start_clock()
            p.progress, p.calm_until, p.progress_time = p.decisions, p.decisions + CFG["calm"], p.clock()
            p.move_ban_until = 0                 # a resume is a fresh start for moves
            p.visits.clear()
            p.hp_hist.clear()                    # HP lost while the outer loop played is not news any more
            p.hit_turn = -99
            p.note("resume", "orders=%r %s plan=%s" % (p.directive[:300], describe(), list(p.plan)))
            status.update(state="running", reason=None, code=None)
            term.sync()
        elif cmd == "notes":
            store.text("reply-%d.txt" % c["seq"], notes_mod.lines(p))
        elif cmd == "mark":
            st = p.last_st
            p.marks[c["name"]] = {"i": p.key_index, "turn": st.get("turn"), "dlvl": st.get("dlvl")}
            store.write("marks.json", p.marks)
            store.text("reply-%d.txt" % c["seq"], "marked %s at key %d (T%s, Dlvl %s)\n" % (
                c["name"], p.key_index, st.get("turn"), st.get("dlvl")))
        elif cmd == "postmortem":
            store.text("reply-%d.txt" % c["seq"], postmortem(p, status.get("reason")))
        elif cmd == "repeat":
            if not paused:
                reply = "refused: the inner loop is running; pause it first\n"
            else:
                reply = guarded_repeat(term, p, base64.b64decode(c.get("keys", "")), c,
                                       beat=lambda: publish() if time.time() - beat["at"] > 2 else None)
            store.text("reply-%d.txt" % c["seq"], reply)
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
                    p.record(keys, "hand")
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
    gone = 0
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
                    gone = 0
                except Closed:
                    gone += 1
                    if gone >= 5 and status["state"] != "ended":
                        pause(GAME_OVER, ended=True)      # the game went away while paused
                        publish()
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
            sync_notes(store, p, level_notes, status["state"] == "ended")
            if disk["warned"] and disk["told"] != disk["warned"] and not paused:
                disk["told"] = disk["warned"]
                pause("state dir not writable (%s): free space in %s, then resume" % (disk["warned"], store.dir))
                publish()
    finally:
        save_pilot(store, p)


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
