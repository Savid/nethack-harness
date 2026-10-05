"""Background session lifecycle and serialized caller commands."""
import fcntl
import os
import signal
import sys
import time
from dataclasses import replace

from . import __version__
from .actions import Action
from .decide import Engine
from .session import Session
from .settings import Settings
from .store import Store
from .transport import Term


def commit():
    try:
        from ._commit import COMMIT
        return COMMIT
    except ImportError:
        return "unknown"


def alive(status):
    pid = status.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return b"_daemon" in f.read()
    except OSError:
        return True


def spawn(store, lock):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    lock_fd = lock.fileno()
    command = [sys.executable, "-m", "nethack_harness", "--dir", store.dir, "_daemon", "--lock-fd", str(lock_fd)]
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            null = os.open(os.devnull, os.O_RDWR)
            log = os.open(store.path("daemon.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(null, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            os.set_inheritable(lock_fd, True)
            os.closerange(3, lock_fd)
            os.closerange(lock_fd + 1, max(256, lock_fd + 2))
            os.execve(sys.executable, command, env)
        finally:
            os._exit(127)
    os.waitpid(pid, 0)


def daemon(args):
    with os.fdopen(args.lock_fd, "a") as lock:
        os.set_inheritable(lock.fileno(), False)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(args.dir)
        try:
            return run(store)
        finally:
            store.close()


def run(store):
    config = store.read("config")
    settings = Settings(**config["settings"])
    term = Term(config["socket"], quiet=settings.quiet)
    engine = Engine(config["endpoint"], config["model"], os.environ.get(config["key_env"]) if config["key_env"] else None,
                    settings.decision_timeout)
    session = Session(term, engine, store, settings)
    status = {"state": "starting", "pid": os.getpid(), "reason": None, "revision": 0, "version": __version__}
    stopping = False

    def signal_stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, signal_stop)
    signal.signal(signal.SIGINT, signal_stop)

    def publish():
        status.update(heartbeat=time.time(), calls=session.calls, actions=session.actions, turns=session.turns,
                      best_depth=session.best_depth, last=session.last,
                      heartbeat_timeout=max(30, session.settings.decision_timeout + 15))
        store.write("status", status)

    def pause(reason):
        session.pending_tool = None
        status.update(state="ended" if reason == "game_over" else "paused", reason=reason,
                      revision=status["revision"] + 1)

    def synchronize():
        try:
            term.sync(lambda: session.step(protocol=Action("redraw", "Redraw the terminal", "redraw", "\x12")))
            store.write("observation", session.observe())
            status.update(state="running", reason=None)
        except Exception as e:
            pause("terminal synchronization failed (%s)" % type(e).__name__)

    def handle(command):
        nonlocal stopping
        name = command["command"]
        if name == "stop":
            stopping = True
            return "stopping"
        if name == "pause":
            pause("caller_pause")
            return "paused"
        if name == "resume":
            if status["state"] != "paused":
                raise ValueError("resume requires a paused session")
            if command.get("objective") is not None and command["objective"] != session.settings.objective:
                session.settings = replace(session.settings, objective=command["objective"])
                store.intent({"record": "goal", "transition": "set", "objective": command["objective"],
                              "goal_id": None, "parent_id": None, "reason": None})
            if command.get("review_after_calls") is not None:
                session.settings = replace(session.settings, review_after_calls=command["review_after_calls"])
            if command.get("max_action_attempts") is not None:
                session.settings = replace(session.settings, max_action_attempts=command["max_action_attempts"])
            if command.get("max_action_steps") is not None:
                session.settings = replace(session.settings, max_action_steps=command["max_action_steps"])
            if command.get("caller_context") is not None:
                session.settings = replace(session.settings, caller_context=command["caller_context"])
            if command.get("tools") is not None:
                session.settings = replace(session.settings, tools=tuple(command["tools"]))
            session.resume(continue_scope=bool(command.get("continue_scope")))
            synchronize()
            return "resumed"
        if name in ("screen", "observe", "actions"):
            term.poll()
            observation = session.observe()
            store.write("observation", observation)
            if name == "screen":
                return term.view().text_screen()
            if name == "observe":
                return observation
            return [a.as_dict() for a in session.offered()]
        if name in ("act", "send"):
            if status["state"] != "paused":
                raise ValueError("manual input requires a paused session")
            term.poll()
            session.observe()
            if name == "act":
                action = next((a for a in session.offered() if a.id == command["action"]), None)
                if action is None:
                    raise ValueError("action is not available on this observation")
            else:
                action = Action("manual", "Caller-supplied keys", "manual", command["keys"])
            reason = session.step(manual=action)
            if reason:
                pause(reason)
            return {"result": session.last, "observation": session.observe()}
        raise ValueError("unknown command")

    store.intent({"record": "goal", "transition": "set", "objective": settings.objective,
                  "goal_id": None, "parent_id": None, "reason": None})
    publish()
    synchronize()
    if config.get("paused") and status["state"] == "running":
        # Display pages need no decision; the caller receives the first playable observation.
        for _ in range(32):
            if session.observe()["phase"] != "more":
                break
            reason = session.step()
            if reason:
                pause(reason)
                break
        if status["state"] == "running":
            store.write("observation", session.observe())
            pause("caller_pause")
    try:
        while not stopping:
            for sequence, command in store.pending():
                try:
                    result = {"ok": True, "value": handle(command)}
                except Exception as e:
                    result = {"ok": False, "error": str(e) if isinstance(e, ValueError) else type(e).__name__}
                # A caller that waits after the reply must not read the state from before the command.
                publish()
                store.reply(sequence, result)
                store.acknowledge(sequence)
                if stopping:
                    break
            if stopping:
                break
            publish()
            if status["state"] == "running":
                try:
                    reason = session.step(cancelled=lambda: stopping or bool(store.pending()))
                except Exception as e:
                    reason = "session error (%s)" % type(e).__name__
                if reason:
                    pause(reason)
            else:
                time.sleep(0.1)
    finally:
        status.update(state="stopped", reason="stopped")
        publish()
    return 0
