"""Command-line control of a decision-engine-driven game session."""
import argparse
import fcntl
import json
import os
import sys
import time

from . import __version__
from .base import unescape
from .daemon import alive, commit, daemon, spawn
from .decide import Engine
from .settings import Settings
from .store import Store
from .transport import serve_local


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def print_value(value):
    print(value if isinstance(value, str) else json.dumps(value, indent=2))


TRANSITIONS = ("set", "added", "activated", "updated", "suspended", "completed", "removed", "replaced")


def read_text(text):
    return sys.stdin.read() if text == "-" else text


def goal_records(value):
    """Validate caller goal transitions: one object or a list of them."""
    items = value if isinstance(value, list) else [value]
    records = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"transition", "objective", "goal_id", "parent_id", "reason"}:
            raise ValueError("goal records accept transition, objective, goal_id, parent_id and reason")
        if item.get("transition") not in TRANSITIONS:
            raise ValueError("transition must be one of " + ", ".join(TRANSITIONS))
        if not isinstance(item.get("objective"), str) or not item["objective"].strip():
            raise ValueError("goal records need objective text")
        for key in ("goal_id", "parent_id", "reason"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError(key + " must be text")
        records.append({"record": "goal", "transition": item["transition"], "objective": item["objective"],
                        "goal_id": item.get("goal_id"), "parent_id": item.get("parent_id"), "reason": item.get("reason")})
    return records


def tool_list(text):
    return [] if text == "all" else [name for name in text.split(",") if name]


def wait(store, timeout, records_after=None):
    end = time.monotonic() + timeout
    while True:
        status = store.read("status", {})
        state = status.get("state")
        if state in ("paused", "ended"):
            result = {"status": dict(status, alive=alive(status)), "observation": store.read("observation")}
            if records_after is not None:
                result["records"] = list(store.records(records_after))
            print_value(result)
            return 3 if state == "ended" else 0
        if state == "stopped" or (status and not alive(status)):
            print("session is not running; inspect daemon.log")
            return 1
        if status and time.time() - status["heartbeat"] > status["heartbeat_timeout"]:
            print("session heartbeat expired; inspect daemon.log")
            return 1
        if time.monotonic() >= end:
            print_value(status or {"state": "starting"})
            return 2
        time.sleep(0.1)


def send_command(store, name, timeout=15, **values):
    status = store.read("status", {})
    if not alive(status) or status.get("state") == "stopped":
        raise ValueError("no running session")
    sequence = store.enqueue(name, **values)
    end = time.monotonic() + max(timeout, status.get("heartbeat_timeout", 30))
    while time.monotonic() < end:
        reply = store.take_reply(sequence)
        if reply is not None:
            if not reply["ok"]:
                raise ValueError(reply["error"])
            return reply["value"]
        time.sleep(0.05)
    raise ValueError("command has not completed; inspect status before retrying")


def parser():
    p = Parser(prog="nethack_harness.py", description="NetHack observation and execution for a decision engine.")
    p.add_argument("--dir", default=".nethack-harness", help="session directory")
    p.add_argument("--version", action="version", version="%s (commit %s)" % (__version__, commit()))
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    start = sub.add_parser("start", help="start a session in an unused session directory")
    start.add_argument("--socket", required=True)
    start.add_argument("--decide", required=True, help="SystemOne-compatible choice endpoint")
    start.add_argument("--model")
    start.add_argument("--key-env", help="environment variable containing the endpoint bearer key")
    start.add_argument("--objective", default="Play NetHack.")
    start.add_argument("--paused", action="store_true", help="pause before the first decision request")
    start.add_argument("--tools", type=tool_list, default=[], help="comma-separated tools the engine may choose; all removes the limit")
    start.add_argument("--context-file", help="JSON object to pass through as caller context; - reads stdin")
    start.add_argument("--decision-timeout", type=float, default=10)
    start.add_argument("--quiet", type=float, default=0.06)
    start.add_argument("--max-action-steps", type=int, default=8)
    start.add_argument("--review-after-calls", type=int, default=0, help="return to caller after this many endpoint calls; 0 disables")
    start.add_argument("--max-action-attempts", type=int, default=0, help="return to caller after this many action attempts; 0 disables")
    start.add_argument("--timeout", type=float, default=30, help="seconds to wait for a pause")
    resume = sub.add_parser("resume", help="resume after a pause")
    resume.add_argument("--objective")
    resume.add_argument("--context-file", help="replace caller context from a JSON object; - reads stdin")
    resume.add_argument("--max-action-steps", type=int, help="replace the selected action step bound")
    resume.add_argument("--review-after-calls", type=int, help="replace the caller review budget; 0 disables")
    resume.add_argument("--max-action-attempts", type=int, help="replace the action attempt budget; 0 disables")
    resume.add_argument("--tools", type=tool_list, help="replace the tools the engine may choose; all removes the limit")
    resume.add_argument("--records-after", type=int, help="once paused, also print decision records after this ID")
    resume.add_argument("--continue-scope", action="store_true",
                        help="keep the objective scope's attempt history and repetition evidence; budgets still renew")
    resume.add_argument("--timeout", type=float, default=30)
    sub.add_parser("wait", help="wait for a pause or game over").add_argument("--timeout", type=float, default=30)
    for name, description in (("pause", "return control at the next action boundary"),
                              ("stop", "stop this session's daemon"), ("status", "print process and session status"),
                              ("screen", "print the terminal"), ("observe", "print the structured observation"),
                              ("actions", "list the actions available now")):
        sub.add_parser(name, help=description)
    sub.add_parser("act", help="execute an offered action while paused").add_argument("action")
    sub.add_parser("send", help="send caller-supplied keys while paused").add_argument("keys")
    export = sub.add_parser("export", help="export decision records as JSONL")
    export.add_argument("--out", help="output file; defaults to stdout")
    export.add_argument("--after", type=int, default=0, help="export only decision IDs greater than this ID")
    note = sub.add_parser("note", help="record a caller note verbatim, such as a plan or lesson")
    note.add_argument("--kind", required=True, help="caller label, for example plan, lesson, hypothesis or observation")
    note.add_argument("--text", required=True, help="note text; - reads stdin")
    purpose = sub.add_parser("purpose", help="record why this session exists")
    purpose.add_argument("--kind", required=True, help="caller label, for example main, checkpoint, trial or scout")
    purpose.add_argument("--about", help="what this session is for")
    purpose.add_argument("--parent", help="the session or record this one derives from")
    goal = sub.add_parser("goal", help="record caller goal transitions from a JSON object or list")
    goal.add_argument("--file", required=True, help="JSON file; - reads stdin")
    sub.add_parser("help", help="show the command interface")
    local = sub.add_parser("serve-local", help="serve a local NetHack terminal over a Unix socket")
    local.add_argument("--socket", required=True)
    local.add_argument("--nethack", default="nethack")
    local.add_argument("--playground")
    local.add_argument("--options", default="color,time,hilite_pet,!autopickup")
    local.add_argument("args", nargs="*")
    sub.add_parser("_daemon").add_argument("--lock-fd", type=int, required=True, help=argparse.SUPPRESS)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "help":
        p.print_help()
        print("\nExit codes: 0 paused/success, 2 still running, 3 game over, 1 failure, 64 invalid arguments.")
        return 0
    if args.command == "serve-local":
        return serve_local(args)
    if args.command == "_daemon":
        return daemon(args)
    try:
        caller_context = None
        if getattr(args, "context_file", None):
            if args.context_file == "-":
                caller_context = json.load(sys.stdin)
            else:
                with open(args.context_file) as source:
                    caller_context = json.load(source)
            Settings(caller_context=caller_context)
        if args.command == "start":
            settings = Settings(args.objective, args.decision_timeout, args.quiet, args.max_action_steps,
                                args.review_after_calls, args.max_action_attempts, caller_context or {}, args.tools)
            Engine(args.decide)
            if args.key_env and not os.environ.get(args.key_env):
                raise ValueError("the endpoint key environment variable is empty")
        if args.command == "resume" and args.objective is not None:
            Settings(objective=args.objective)
        if args.command == "resume" and args.review_after_calls is not None:
            Settings(review_after_calls=args.review_after_calls)
        if args.command == "resume" and args.max_action_attempts is not None:
            Settings(max_action_attempts=args.max_action_attempts)
        if args.command == "resume" and args.max_action_steps is not None:
            Settings(max_action_steps=args.max_action_steps)
        if args.command == "resume" and args.tools is not None:
            Settings(tools=args.tools)
        if args.command == "resume" and args.records_after is not None and args.records_after < 0:
            raise ValueError("--records-after must be nonnegative")
        intents = None
        if args.command == "note":
            intents = [{"record": "note", "kind": args.kind, "text": read_text(args.text)}]
        elif args.command == "purpose":
            intents = [{"record": "purpose", "kind": args.kind, "about": args.about, "parent": args.parent}]
        elif args.command == "goal":
            intents = goal_records(json.load(sys.stdin) if args.file == "-" else json.load(open(args.file)))
        if intents and not all(isinstance(value, str) and value.strip() for value in
                               (item.get("kind", "goal") for item in intents)):
            raise ValueError("kind must be nonempty text")
        if args.command == "export" and args.after < 0:
            raise ValueError("--after must be nonnegative")
        if args.command == "send":
            args.keys = unescape(args.keys)
            if not args.keys or len(args.keys.encode()) > 256:
                raise ValueError("send requires 1 to 256 bytes")
        if hasattr(args, "timeout") and not 0 <= args.timeout <= 86400:
            raise ValueError("timeout must be between 0 and 86400 seconds")
    except (ValueError, OSError) as e:
        p.error(str(e))
    if intents is not None and not os.path.exists(os.path.join(args.dir, "session.sqlite3")):
        print("no session in this directory", file=sys.stderr)
        return 1
    store = Store(args.dir)
    try:
        if intents is not None:
            print_value({"records": [store.intent(item) for item in intents]})
            return 0
        if args.command == "start":
            with open(store.path("daemon.lock"), "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise ValueError("a session already holds this directory") from None
                if store.read("config") is not None:
                    raise ValueError("this directory contains a session; choose a new --dir")
                store.write("config", {"socket": args.socket, "endpoint": args.decide, "model": args.model,
                                       "key_env": args.key_env, "paused": args.paused, "settings": settings.as_dict()})
                spawn(store, lock)
            for _ in range(150):
                if store.read("status"):
                    break
                time.sleep(0.1)
            else:
                raise ValueError("daemon did not start; inspect daemon.log")
            return wait(store, args.timeout)
        if args.command == "status":
            status = store.read("status", {})
            print_value(dict(status, alive=alive(status)))
        elif args.command == "wait":
            if not store.read("status"):
                raise ValueError("no session in this directory")
            return wait(store, args.timeout)
        elif args.command == "export":
            out = open(args.out, "w") if args.out else sys.stdout
            try:
                for record in store.records(args.after):
                    print(json.dumps(record, allow_nan=False), file=out)
            finally:
                if args.out:
                    out.close()
        elif args.command == "resume":
            send_command(store, "resume", objective=args.objective, review_after_calls=args.review_after_calls,
                         max_action_attempts=args.max_action_attempts, max_action_steps=args.max_action_steps,
                         caller_context=caller_context, tools=args.tools, continue_scope=args.continue_scope)
            return wait(store, args.timeout, args.records_after)
        else:
            values = {"action": args.action} if args.command == "act" else {"keys": args.keys} if args.command == "send" else {}
            print_value(send_command(store, args.command, **values))
        return 0
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        store.close()
