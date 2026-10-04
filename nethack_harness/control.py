"""The command line: start, wait, resume and the other commands, talking to the daemon through the state directory."""
import argparse
import base64
import fcntl
import json
import os
import re
import signal
import sys
import time

from . import __version__
from .decide import decider
from .base import unescape
from .policy import Pilot
from .settings import DEFAULTS, MODES, validate
from .transport import Term, serve_local
from .daemon import commit, daemon, spawn_daemon
from .helptext import help_text
from .store import Store, enqueue, print_keys


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
    """The recorded daemon process still exists and is a harness daemon (a reused pid is not)."""
    try:
        pid = int(status["pid"])
        os.kill(pid, 0)
    except (OSError, KeyError, TypeError, ValueError):
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return b"_daemon" in f.read()
    except OSError:
        return True                  # no /proc here: trust the signal check


def lock_free(store):
    """True when no process holds the state dir's daemon lock (so no loop is running there)."""
    try:
        with open(store.path("daemon.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def wait(store, timeout, since):
    end = time.time() + timeout
    while time.time() < end:
        st = store.read("status.json")
        live = alive(st) and ours(store, st)
        if st.get("escalation", 0) > since and (st.get("state") == "ended" or st.get("state") == "paused" and live):
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
    print("running: max Dlvl %s, %s decisions%s; wait again" % (st.get("max_dlvl"), st.get("decisions"), health))
    return 2


def parse_sets(items):
    out = {}
    for item in items or []:
        if "=" not in item:
            print("--set wants k=v, got %r" % item, file=sys.stderr)
            raise SystemExit(64)
        k, v = item.split("=", 1)
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
    p.add_argument("--notes-out", metavar="FILE", help="keep FILE up to date with this game's level notes")
    p.add_argument("--notes-in", metavar="FILE", help="use another copy's level notes (re-read when FILE changes)")
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
    sub.add_parser("notes", help="one line per level: stairs seen or imported, up stairs, holes, hazards")
    mk = sub.add_parser("mark", help="name this moment in the key journal (for keys --since NAME)")
    mk.add_argument("name")
    ks = sub.add_parser("keys", help="the key journal: every key sent (loop, plan, plugin, hand, replay)")
    ks.add_argument("--since", help="a mark NAME, or a turn number")
    ks.add_argument("--raw", action="store_true", help="key lines only, ready for --plan replay:FILE")
    sub.add_parser("postmortem", help="the death or last crisis in one block: killer, HP trail, ladder steps, "
                                      "escalations, prayer, settings, last keys and messages")
    rp = sub.add_parser("repeat", help="while paused: send keys up to N times, stopping at the first sign of "
                                       "trouble (HP loss, new monster, --More--, prompt, matching message)")
    rp.add_argument("keys", nargs="?")
    rp.add_argument("--hex", help="bytes as hex instead of KEYS")
    rp.add_argument("--times", type=int, default=5, help="repetitions, 1-50 (default 5)")
    rp.add_argument("--stop-hp", type=float, default=0.5, help="stop below this HP fraction (default 0.5)")
    rp.add_argument("--stop-on", default="", metavar="REGEX", help="also stop when a message matches")
    rp.add_argument("--allow-hp-loss", action="store_true", help="do not stop just because HP fell")
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
    if getattr(a, "plan", None):
        a.plan = ["replay:" + os.path.abspath(x[7:]) if x.startswith("replay:") else x for x in a.plan]
    for item in getattr(a, "plan", None) or []:
        try:
            Pilot.check_plan(item)
        except ValueError as e:
            print("--plan: %s" % e, file=sys.stderr)
            return 64
    if a.cmd == "repeat" and a.keys is not None and re.search(r"[0-9]$", a.keys):
        print("repeat wants whole commands: KEYS ends in a count (digits) with no command after it")
        return 64
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
               "notes_out": os.path.abspath(a.notes_out) if a.notes_out else None,
               "notes_in": os.path.abspath(a.notes_in) if a.notes_in else None,
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
        print(json.dumps(dict(st, alive=alive(st) and ours(store, st)), default=str))
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
    if a.cmd == "keys":
        return print_keys(store, a.since, a.raw)
    if a.cmd == "postmortem" and not (st and alive(st) and ours(store, st)):
        text = store.text("postmortem.txt")
        print(text or "no postmortem in %s (the game has not ended, and no loop is running)\n" % store.dir, end="")
        return 0 if text else 1
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
        if lock_free(store) or not alive(st):
            # nobody holds this dir's lock: the recorded pid is gone or belongs to someone else; signal no one
            st.update(state="stopped")
            store.write("status.json", st)
            print("no inner loop holds %s (pid %d is not ours); marked stopped" % (store.dir, pid))
            return 0
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
        enqueue(store, a.cmd)
        if a.cmd == "pause":
            return wait(store, 15, st.get("escalation", 0))
        print("stop requested")
        return 0
    if a.cmd == "resume":
        if st.get("state") != "paused":
            print("not paused (state: %s); use wait" % st.get("state"))
            return 1
        enqueue(store, "resume", answering=st.get("escalation"), directive=a.directive, mode=a.mode,
                set=sets, plan=a.plan,
                questions=[os.path.abspath(x) for x in a.questions], plugins=[os.path.abspath(x) for x in a.plugin],
                enable=a.enable, disable=a.disable,
                notes_out=os.path.abspath(a.notes_out) if a.notes_out else None,
                notes_in=os.path.abspath(a.notes_in) if a.notes_in else None)
        return wait(store, a.timeout, st.get("escalation", 0))
    if a.cmd in ("postmortem", "mark", "notes"):
        seq = enqueue(store, a.cmd, **({"name": a.name} if a.cmd == "mark" else {}))
        for _ in range(150):
            reply = store.text("reply-%d.txt" % seq)
            if reply:
                os.unlink(store.path("reply-%d.txt" % seq))
                print(reply, end="")
                return 0
            time.sleep(0.1)
        print("no reply from the inner loop")
        return 1
    if a.cmd in ("send", "screen", "repeat"):
        keys, extra = b"", {}
        if a.cmd == "repeat":
            if not 1 <= a.times <= 50 or not 0 <= a.stop_hp <= 1:
                print("repeat wants --times 1-50 and --stop-hp 0-1")
                return 64
            try:
                re.compile(a.stop_on)
            except re.error as e:
                print("repeat --stop-on is not a valid regex: %s" % e)
                return 64
            extra = {"times": a.times, "stop_hp": a.stop_hp, "stop_on": a.stop_on, "allow_loss": a.allow_hp_loss}
        if a.cmd in ("send", "repeat"):
            if (a.keys is None) == (a.hex is None):
                print("%s wants keys or --hex" % a.cmd)
                return 64
            try:
                keys = unescape(a.keys).encode("latin-1") if a.keys is not None else bytes.fromhex(a.hex)
            except ValueError:
                print("%s --hex wants hex bytes, e.g. '04 6c'" % a.cmd)
                return 64
            if not 0 < len(keys) <= 4096:
                print("%s takes 1 to 4096 bytes" % a.cmd)
                return 64
        seq = enqueue(store, a.cmd, keys=base64.b64encode(keys).decode(), **extra)
        for _ in range(600 if a.cmd == "repeat" else 150):
            reply = store.text("reply-%d.txt" % seq)
            if reply:
                os.unlink(store.path("reply-%d.txt" % seq))
                print(reply, end="")
                return 0 if not reply.startswith(("error", "refused")) else 1
            time.sleep(0.1)
        print("no reply from the inner loop")
        return 1
    return 0
