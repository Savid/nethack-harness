"""JSON CLI for an LLM or other caller to manage and execute goals."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from .goals import GoalBoard
from .harness import HarnessClient
from .runner import step
from .storage import Repository


def read_json(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as source:
        return json.load(source)


def parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="separate directory for caller goal state")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="attach to an existing paused harness session")
    init.add_argument("--session", required=True)
    init.add_argument("--harness", default=str(Path(__file__).resolve().parent.parent / "nethack_harness.py"),
                      help="harness Python script or zipapp")
    for name in ("add", "update", "replace"):
        command = commands.add_parser(name)
        if name != "add":
            command.add_argument("id")
        command.add_argument("--file", required=True, help="goal JSON (patch for update); - reads stdin")
    for name in ("activate", "suspend", "remove", "complete"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "complete":
            command.add_argument("--evidence", required=True, help="caller explanation supporting completion")
    commands.add_parser("status", help="goals and pending execution, without contacting the game")
    commands.add_parser("events", help="retained goal lifecycle and execution history")
    commands.add_parser("check", help="assess active goal and report eligibility using a fresh observation")
    commands.add_parser("step", help="assess and execute at most one bounded action")
    run = commands.add_parser("run", help="repeat bounded windows until completion or caller review")
    run.add_argument("--max-windows", type=int, default=8)
    commands.add_parser("recover", help="pause and reconcile an unresolved execution; never retry it")
    return parser


def invoke(repository, args):
    with repository.locked():
        if args.command == "init":
            if (repository.directory / "state.json").exists():
                raise ValueError("this directory already contains goal state")
            config = {"command": [sys.executable, str(Path(args.harness).resolve())],
                      "directory": str(Path(args.session).resolve())}
            if Path(config["directory"]) == repository.directory:
                raise ValueError("goal state needs a directory separate from the harness session")
            HarnessClient(**config).paused()
            document = {"harness": config, "board": GoalBoard().snapshot()}
            repository.save(document)
            return {"reason": "initialized"}
        document = repository.load()
        board = GoalBoard(document["board"])
        client = HarnessClient(**document["harness"])
        name = args.command
        if name == "status":
            return {key: value for key, value in board.snapshot().items() if key != "events"}
        if name == "events":
            return board.state["events"]
        if name in ("step", "run", "recover"):
            return step(document, client, repository.save, recover=name == "recover")
        board.editable()
        if name == "add":
            result = board.add(read_json(args.file))
        elif name in ("update", "replace"):
            result = getattr(board, name)(args.id, read_json(args.file))
        elif name in ("suspend", "remove"):
            result = getattr(board, name)(args.id)
        else:
            client.paused()
            observation = client.observe()
            if name == "activate":
                result = board.activate(args.id, observation)
            elif name == "complete":
                result = board.complete(args.id, observation, args.evidence)
            else:
                assessment = board.assess(observation)
                result = {"active": assessment, "goals": {identifier: board.eligibility(identifier, observation)
                                                           for identifier in board.state["goals"]}}
        document["board"] = board.snapshot()
        repository.save(document)
        return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    if arguments.command == "run" and arguments.max_windows <= 0:
        parser().error("--max-windows must be positive")
    repository = Repository(arguments.dir)
    try:
        windows = arguments.max_windows if arguments.command == "run" else 1
        for _ in range(windows):
            result = invoke(repository, arguments)
            if arguments.command != "run" or result.get("reason") != "ready":
                break
        else:
            result = dict(result, reason="window_limit", explanation="goal remains active; caller may continue or edit it")
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error), "recovery": "inspect status; use recover if an execution is pending"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
