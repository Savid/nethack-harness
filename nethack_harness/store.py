"""The state directory: atomic files, the command queue, the key journal, notes and the saved pilot."""
import fcntl
import json
import os
import pickle
import random
import time

from . import __version__
from . import notes as notes_mod
from .settings import CFG


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


def enqueue(store, cmd, **kw):
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


def remember_hooks(store, c):
    """Keep loaded hook files and enable/disable choices so a restarted loop restores them."""
    h = store.read("hooks.json") or {"questions": [], "plugins": [], "disabled": []}
    h["questions"] += [x for x in c.get("questions") or [] if x not in h["questions"]]
    h["plugins"] += [x for x in c.get("plugins") or [] if x not in h["plugins"]]
    h["disabled"] = sorted((set(h["disabled"]) | set(c.get("disable") or [])) - set(c.get("enable") or []))
    store.write("hooks.json", h)


def print_keys(store, since=None, raw=False):
    """Print the key journal, optionally from a mark or a turn on."""
    rows = []
    for name in ("keys.jsonl.1", "keys.jsonl"):
        for line in store.text(name).splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if since:
        marks = store.read("marks.json")
        if since in marks:
            rows = [r for r in rows if r["i"] > marks[since]["i"]]
        elif since.isdigit():
            rows = [r for r in rows if (r.get("t") or 0) >= int(since)]
        else:
            print("no mark %r (marks: %s)" % (since, ", ".join(sorted(marks)) or "none"))
            return 1
    for r in rows:
        print(r["k"] if raw else "T%-6s Dlvl%-3s %-7s %s" % (r.get("t"), r.get("dl"), r["src"], r["k"]))
    return 0


def sync_notes(store, p, notes, ended=False):
    """--notes-out: rewrite the file on a level change, every 200 turns and at game over. --notes-in: merge the
    other copy's file whenever it changes."""
    if notes.get("notes_out"):
        mark = (p.prev_dl, (p.turn or 0) // 200, ended)
        if mark != notes.get("written"):
            try:
                notes_mod.write(notes["notes_out"], p)
                notes["written"] = mark
            except OSError as e:
                p.note("notes", "cannot write %s: %s" % (notes["notes_out"], e.strerror))
                notes["notes_out"] = None
    if notes.get("notes_in") and p.anchor:       # this game's own fingerprint comes from its first screen
        try:
            mtime = os.stat(notes["notes_in"]).st_mtime
        except OSError:
            return
        if mtime != notes.get("mtime"):
            notes["mtime"] = mtime
            try:
                with open(notes["notes_in"]) as f:
                    got = notes_mod.merge(p, json.load(f))
            except (OSError, ValueError) as e:
                got = "unreadable notes: %s" % e
            p.note("notes", "imported %s" % (", ".join(got) if isinstance(got, list) else got))


MEMORY = "%s/m1" % __version__      # bump the suffix when the pickled pilot changes shape


def save_pilot(store, p):
    """Returns None, or the error when the state directory cannot be written. A memory that could not be
    updated is removed, so a restart never resumes from stale knowledge."""
    kept = (p.term, p.log, p.decide, p.hooks, p.paused_at, p.journal)
    p.term = p.log = p.decide = p.hooks = p.journal = None
    if p.paused_at is None:
        p.paused_at = time.time()       # a restart from this memory does not count the downtime as play
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
        p.term, p.log, p.decide, p.hooks, p.paused_at, p.journal = kept


LOG_CAP = 32 << 20


def trim_logs(store, p):
    """Keep log.jsonl and daemon.log bounded: the log rotates once to log.jsonl.1; daemon.log is cut."""
    try:
        if p.journal and p.journal.tell() > LOG_CAP:
            p.journal.close()
            os.replace(store.path("keys.jsonl"), store.path("keys.jsonl.1"))
            p.journal = open(store.path("keys.jsonl"), "a")
        if p.log and p.log.tell() > LOG_CAP:
            p.log.close()
            os.replace(store.path("log.jsonl"), store.path("log.jsonl.1"))
            p.log = open(store.path("log.jsonl"), "a")
        if os.fstat(2).st_size > LOG_CAP // 4 and os.path.samefile("/proc/self/fd/2", store.path("daemon.log")):
            os.ftruncate(2, 0)
    except (OSError, ValueError):
        pass
