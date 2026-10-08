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
from .play import Play
from .session import Session
from .settings import Settings
from .store import Store
from .transport import Closed, Term


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
                    settings.decision_timeout) if config["endpoint"] else None
    session = Session(term, engine, store, settings)
    play = Play(session)
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
        status.update(state="ended" if reason in ("game_over", "terminal_closed") else "paused", reason=reason,
                      revision=status["revision"] + 1)

    def synchronize():
        try:
            term.sync(lambda: session.step(protocol=Action("redraw", "Redraw the terminal", "redraw", "\x12")))
            store.write("observation", session.observe())
            status.update(state="running", reason=None)
        except Exception as e:
            pause("terminal synchronization failed (%s)" % type(e).__name__)

    def refresh():
        try:
            term.poll()
        except Closed:
            pause("terminal_closed")
        if status["state"] != "ended" and term.view().ended:
            pause("game_over")

    def handle(sequence, command):
        nonlocal stopping
        name = command["command"]
        if name == "stop":
            stopping = True
            return "stopping"
        if name == "pause":
            pause("caller_pause")
            return "paused"
        if name == "resume":
            if engine is None:
                raise ValueError("this session has no decision endpoint; play it with look, send, go and rest")
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
        if name == "observe":
            term.poll()
            observation = session.observe()
            store.write("observation", observation)
            return observation
        if name == "look":
            refresh()
            text = play.view()
            store.write("observation", session.observe())
            return {"view": text, "ended": status["state"] == "ended"}
        if name in ("send", "go", "rest"):
            if status["state"] not in ("paused", "ended"):
                raise ValueError("the session is playing by itself; pause it first")
            refresh()
            closed = status["reason"] == "terminal_closed"
            if status["state"] == "ended" and (name != "send" or closed):
                report = "%s: the game %s; nothing was sent" % (name, "has exited" if closed else "is over")
                return {"view": play.view(report), "ended": True}

            def interrupted():
                return stopping or any(other["command"] in ("pause", "stop")
                                       for number, other in store.pending() if number != sequence)

            session.observe()
            report, reason, refused = getattr(play, name)(command["argument"], interrupted)
            if reason:
                pause(reason)
            return {"view": play.view(report), "ended": status["state"] == "ended", "failed": bool(refused)}
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
                    result = {"ok": True, "value": handle(sequence, command)}
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
