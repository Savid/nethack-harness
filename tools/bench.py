#!/usr/bin/env python3
"""Measure decision-engine sessions against local games."""
import argparse
import json
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from nethack_harness.store import Store  # noqa: E402


def cli(directory, *args):
    return subprocess.run([sys.executable, str(ROOT / "nethack_harness.py"), "--dir", str(directory)] + list(args),
                          capture_output=True, text=True, timeout=60)


def seeds(text):
    result = []
    for part in text.split(","):
        if "-" in part:
            start, end = map(int, part.split("-"))
            result.extend(range(start, end + 1))
        else:
            result.append(int(part))
    if not result:
        raise argparse.ArgumentTypeError("at least one seed is required")
    return result


def run_game(args, seed):
    with tempfile.TemporaryDirectory(prefix="nethack-bench-") as directory:
        root = Path(directory)
        socket, state = root / "terminal.sock", root / "session"
        substitutions = {"seed": str(seed), "socket": str(socket), "dir": str(root)}
        command = [part.format(**substitutions) for part in shlex.split(args.serve)]
        with open(root / "game.log", "w") as log:
            game = subprocess.Popen(command, stdout=log, stderr=log)
            started = time.monotonic()
            try:
                for _ in range(100):
                    if socket.exists():
                        break
                    if game.poll() is not None:
                        raise RuntimeError("game server exited before opening its socket")
                    time.sleep(0.1)
                else:
                    raise RuntimeError("game server did not open its socket")
                extra = []
                for key in ("model", "key_env"):
                    if getattr(args, key):
                        extra += ["--" + key.replace("_", "-"), getattr(args, key)]
                result = cli(state, "start", "--socket", str(socket), "--decide", args.decide,
                             "--objective", args.objective, "--max-action-steps", str(args.max_action_steps),
                             "--timeout", str(min(5, args.seconds)), *extra)
                while result.returncode == 2 and time.monotonic() - started < args.seconds:
                    result = cli(state, "wait", "--timeout", str(min(5, max(0, args.seconds - (time.monotonic() - started)))))
                store = Store(state)
                try:
                    status = store.read("status", {})
                    records = [record for record in store.records() if record["type"] == "decision"]
                finally:
                    store.close()
                latencies = [r["latency"] for r in records if r["source"] == "engine" and r["latency"] is not None]
                failed = sum((r["outcome"] or {}).get("reason") == "decision_error" for r in records)
                return {"seed": seed, "seconds": time.monotonic() - started, "exit": result.returncode,
                        "state": status.get("state"), "reason": status.get("reason"),
                        "best_depth": status.get("best_depth", 0), "turns": status.get("turns", 0),
                        "actions": status.get("actions", 0), "calls": status.get("calls", 0),
                        "decision_errors": failed, "latency_median": statistics.median(latencies) if latencies else None,
                        "truncated": result.returncode == 2, "model": args.model, "objective": args.objective}
            finally:
                cli(state, "stop")
                game.terminate()
                try:
                    game.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    game.kill()
                    game.wait()


def report(paths):
    for path in paths:
        with open(path) as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        if not rows:
            continue
        print(json.dumps({"file": path, "games": len(rows),
                          "median_depth": statistics.median(r["best_depth"] for r in rows),
                          "median_turns": statistics.median(r["turns"] for r in rows),
                          "calls": sum(r["calls"] for r in rows),
                          "decision_errors": sum(r["decision_errors"] for r in rows),
                          "ended": sum(r["state"] == "ended" for r in rows),
                          "truncated": sum(r["truncated"] for r in rows)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--serve", required=True, help="server command with {seed}, {socket} and {dir} placeholders; no shell")
    run.add_argument("--decide", required=True)
    run.add_argument("--model")
    run.add_argument("--key-env")
    run.add_argument("--objective", default="Play NetHack.")
    run.add_argument("--seeds", type=seeds, default=[1, 2, 3])
    run.add_argument("--seconds", type=float, default=120)
    run.add_argument("--max-action-steps", type=int, default=8)
    run.add_argument("--out", required=True)
    sub.add_parser("report").add_argument("files", nargs="+")
    args = parser.parse_args()
    if args.command == "report":
        report(args.files)
        return
    if args.seconds <= 0:
        parser.error("seconds must be positive")
    with open(args.out, "w") as stream:
        for seed in args.seeds:
            result = run_game(args, seed)
            stream.write(json.dumps(result) + "\n")
            stream.flush()
            print(json.dumps(result))


if __name__ == "__main__":
    main()
