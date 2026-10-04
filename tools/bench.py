#!/usr/bin/env python3
"""A small regression benchmark: seeded local games, a scripted outer loop, and per-game metrics.

Judge behaviour changes on many games, not on one anecdote. Each game runs for a fixed wall time with no
decision model (or a mock endpoint), under one of two scripted outer loops:

  resume       resume every escalation with no help (what the loop does on its own)
  recommended  answer escalations with the plan items and settings the docs recommend

    python3 tools/bench.py run --serve CMD --seeds 1-12 --secs 240 --out head.jsonl
    python3 tools/bench.py compare --base v0.4.1 --head master --serve CMD --seeds 1-12 --secs 240
    python3 tools/bench.py report head.jsonl [base.jsonl]

--serve is a shell command that starts a terminal socket for one game; it is formatted with {seed},
{socket} and {dir} (a fresh empty directory per game). The default uses this harness's serve-local:

    python3 nethack_harness.py serve-local --socket {socket} --nethack nethack --options 'seed:{seed},color'

At most --parallel games (default 2) run at once.
"""
import argparse
import collections
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SERVE = ("%s %s serve-local --socket {socket} --nethack nethack --options 'seed:{seed},color,!autopickup'"
                 % (shlex.quote(sys.executable), shlex.quote(os.path.join(ROOT, "nethack_harness.py"))))

# Escalation codes by reason prefix, so that commits from before the code registry are counted alike.
CODES = [("briefing", r"^briefing"), ("game_over", r"^game_over"), ("milestone", r"^milestone"),
         ("branch_point", r"^branch point"), ("depth_jump", r"^depth jump"), ("depth_gate", r"^depth gate"),
         ("endgame", r"^endgame"), ("crisis", r"^losing fast: (?:the crisis|HP still)"),
         ("losing_fast", r"^losing fast"), ("surrounded", r"^surrounded"), ("low_hp", r"^low HP"),
         ("hunger", r"from hunger|^Hungry"), ("oscillating", r"^oscillating"), ("stalled", r"^stalled"),
         ("level_exhausted", r"^level exhausted"), ("alarm", r"^alarming"), ("prompt", r"prompt"),
         ("danger", r"^danger|^uncertain"), ("other", r"")]
# Rough fragile/sturdy split by role, for the per-role table.
STURDY = ("Valkyrie", "Samurai", "Barbarian", "Knight", "Priest", "Monk", "Caveman")


def code_of(reason):
    return next(c for c, rx in CODES if re.search(rx, reason or ""))


# ------------------------------------------------------------------ the scripted outer loops
def answer(code, reason, level):
    """The 'recommended' outer loop: resume options for an escalation, from the documented playbook."""
    if code in ("losing_fast", "crisis", "low_hp", "surrounded"):
        if "prayer: safe" in reason or "prayer: likely" in reason:
            return ["--plan", "goal:pray"]
        return ["--plan", "goal:retreat"] if level >= 2 else ["--plan", "goal:elbereth"]
    if code == "hunger" and "prayer: safe" in reason:
        return ["--plan", "goal:pray"]
    if code in ("level_exhausted", "stalled"):
        return ["--plan", "goal:search:30", "--plan", "goal:explore:20"]
    if code == "depth_gate":
        return ["--plan", "goal:explore:30"]
    if code == "alarm" and "lycanthropy" in reason and "prayer: safe" in reason:
        return ["--plan", "goal:pray"]
    return []


def harness(harn, state, args, timeout=120):
    try:
        p = subprocess.run([sys.executable, harn, "--dir", state] + args, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return "timeout", ""


def play(harn, sock, state, secs, outer, decide, extra):
    """Run the outer loop for secs of wall time. Returns (role line, escalation reasons, exit code)."""
    end = time.time() + secs
    code, out = harness(harn, state, ["start", "--socket", sock, "--decide", decide, "--timeout", "20"] + extra)
    role, reasons = "", []
    level = 0
    while True:
        first = out.strip().splitlines()[0] if out.strip() else ""
        m = re.search(r"you are an? (?:\S+ )?(\S+) (\S+) \(", first)
        if m and not role:
            role = m.group(2)
        if code == 0 and first.startswith("ESCALATION:"):
            reasons.append(first[len("ESCALATION: "):])
        if code == 3 or code == 1 or time.time() >= end:
            break
        left = max(5, min(60, end - time.time()))
        if code == 0:
            reason = reasons[-1] if reasons else ""
            m = re.search(r"Dlvl (\d+)", out)
            level = int(m.group(1)) if m else level
            opts = answer(code_of(reason), reason, level) if outer == "recommended" else []
            if "unknown" in reason and "prompt" in reason:
                harness(harn, state, ["send", "--hex", "1b"])
            code, out = harness(harn, state, ["resume", "--timeout", str(int(left))] + opts)
        else:
            code, out = harness(harn, state, ["wait", "--timeout", str(int(left))])
    final = code
    if final not in (3,):
        harness(harn, state, ["stop"])
    return role, reasons, final


# ------------------------------------------------------------------ metrics from the loop's own log
def metrics(state, role, reasons, final, secs):
    rows = []
    for name in ("log.jsonl.1", "log.jsonl"):
        try:
            with open(os.path.join(state, name)) as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
    try:
        with open(os.path.join(state, "status.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    deepest, first_at, xl, turn = 1, {1: (0.0, 1)}, 1, 0
    last_new_turn, stuck, gated_turns, linger = 1, [], 0, 0
    gate_since = None
    for r in rows:
        if r.get("turn"):
            turn = max(turn, r["turn"])
        if r["kind"] == "level":
            m = re.search(r"arrived on Dlvl (\d+) \(turn (\d+)\)", r.get("text", ""))
            if m:
                dl, t = int(m.group(1)), int(m.group(2))
                if dl > deepest:
                    if t - last_new_turn >= 500:
                        stuck.append((deepest, last_new_turn, t))
                    deepest, last_new_turn = dl, t
                    first_at[dl] = (r.get("t", 0.0), t)
                if gate_since is not None:
                    gated_turns += t - gate_since
                    gate_since = None
        elif r["kind"] == "msg":
            m = re.search(r"Welcome to experience level (\d+)", r.get("text", ""))
            if m:
                xl = max(xl, int(m.group(1)))
        elif r["kind"] == "escalate" and r.get("text", "").startswith("depth gate") and gate_since is None:
            gate_since = r.get("turn") or turn
        elif r["kind"] == "act" and r.get("text", "").startswith("linger"):
            linger += 1
    if gate_since is not None:
        gated_turns += turn - gate_since
    if turn - last_new_turn >= 500:
        stuck.append((deepest, last_new_turn, turn))
    died = final == 3
    cause = ""
    if died:
        try:
            with open(os.path.join(state, "postmortem.txt")) as f:
                m = re.search(r"killer \(best guess\): (.*)", f.read())
                cause = m.group(1).strip() if m else ""
        except OSError:
            pass
        if not cause:
            msgs = [r.get("text", "") for r in rows if r["kind"] == "msg"][-6:]
            hit = [m.group(1) for t in msgs for m in re.finditer(r"(?:The |the )([a-z][\w' -]*?) "
                                                                 r"(?:hits|bites|stings|kicks|claws|zaps)", t)]
            cause = hit[-1] if hit else "unknown"
    codes = collections.Counter(code_of(x) for x in reasons)
    return {"role": role, "sturdy": role in STURDY, "max_dlvl": deepest, "xl": xl, "turns": turn,
            "died": died, "cause": cause, "secs": secs,
            "dlvl_at": {str(k): {"t": round(v[0], 1), "turn": v[1]} for k, v in sorted(first_at.items())},
            "escalations": dict(codes), "decisions": st.get("decisions"), "keys": st.get("keys"),
            "stuck": [{"dlvl": d, "from": a, "to": b, "turns": b - a} for d, a, b in stuck],
            "stuck_turns": sum(b - a for _, a, b in stuck), "gated_turns": gated_turns, "linger_acts": linger}


# ------------------------------------------------------------------ running games
def one_game(seed, a, harn, label):
    work = tempfile.mkdtemp(prefix="bench-%s-%s-" % (label, seed))
    sock = os.path.join(tempfile.mkdtemp(prefix="nhb"), "t.sock")
    game_dir = os.path.join(work, "game")
    os.makedirs(game_dir)
    cmd = a.serve.format(seed=seed, socket=shlex.quote(sock), dir=shlex.quote(game_dir))
    server = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    try:
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
        time.sleep(1.0)
        state = os.path.join(work, "state")
        extra = []
        for s in a.set:
            extra += ["--set", s]
        role, reasons, final = play(harn, sock, state, a.secs, a.outer, a.decide, extra)
        out = metrics(state, role, reasons, final, a.secs)
        out.update(seed=seed, label=label, outer=a.outer)
        return out
    finally:
        try:
            os.killpg(server.pid, signal.SIGTERM)
        except OSError:
            pass
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(os.path.dirname(sock), ignore_errors=True)


def seeds_of(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out += list(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


def run_games(a, harn, label):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(2, a.parallel))) as pool:
        futures = [pool.submit(one_game, seed, a, harn, label) for seed in seeds_of(a.seeds)]
        for f in futures:
            r = f.result()
            results.append(r)
            print("  %s seed %-4s %-11s Dlvl %-2s XL %-2s T%-6s %s" % (
                label, r["seed"], r["role"] or "?", r["max_dlvl"], r["xl"], r["turns"],
                "died (%s)" % r["cause"] if r["died"] else "alive"), file=sys.stderr)
    return results


def worktree(ref):
    path = tempfile.mkdtemp(prefix="bench-ref-")
    subprocess.run(["git", "-C", ROOT, "worktree", "add", "--detach", "-q", path, ref], check=True)
    return path


# ------------------------------------------------------------------ reports
def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def summary(results):
    by_dl = collections.defaultdict(list)
    for r in results:
        for dl, v in r["dlvl_at"].items():
            by_dl[int(dl)].append(v["turn"])
    esc = collections.Counter()
    for r in results:
        esc.update(r["escalations"])
    n = len(results) or 1
    return {
        "games": len(results),
        "median max Dlvl": med([r["max_dlvl"] for r in results]),
        "median XL": med([r["xl"] for r in results]),
        "deaths": sum(r["died"] for r in results),
        "median turns": med([r["turns"] for r in results]),
        "median turn reaching Dlvl": {dl: (med(v), len(v)) for dl, v in sorted(by_dl.items())},
        "median stuck turns (>=500 without a new deepest level)": med([r["stuck_turns"] for r in results]),
        "median gated turns": med([r["gated_turns"] for r in results]),
        "escalations per game": {k: round(v / n, 1) for k, v in esc.most_common()},
        "median decisions": med([r["decisions"] for r in results]),
    }


def per_role(results):
    rows = collections.defaultdict(list)
    for r in results:
        rows[r["role"] or "?"].append(r)
    out = []
    for role, rs in sorted(rows.items()):
        out.append("  %-12s %-7s n=%d  Dlvl %s  XL %s  deaths %d  stuck %s  gated %s" % (
            role, "sturdy" if rs[0]["sturdy"] else "fragile", len(rs), med([r["max_dlvl"] for r in rs]),
            med([r["xl"] for r in rs]), sum(r["died"] for r in rs), med([r["stuck_turns"] for r in rs]),
            med([r["gated_turns"] for r in rs])))
    return "\n".join(out)


def print_report(sets):
    names = list(sets)
    sums = {k: summary(v) for k, v in sets.items()}
    keys = list(next(iter(sums.values())))
    print("%-58s %s" % ("", "  ".join("%-24s" % n for n in names)))
    for k in keys:
        vals = [sums[n][k] for n in names]
        if isinstance(vals[0], dict):
            print(k)
            sub = sorted(set().union(*[v.keys() for v in vals]), key=lambda x: (isinstance(x, str), x))
            for s in sub:
                print("  %-56s %s" % (s, "  ".join("%-24s" % (v.get(s, "-"),) for v in vals)))
        else:
            print("%-58s %s" % (k, "  ".join("%-24s" % (v,) for v in vals)))
    for n in names:
        print("\nper role, %s" % n)
        print(per_role(sets[n]))
        deaths = collections.Counter(r["cause"] for r in sets[n] if r["died"])
        if deaths:
            print("  deaths by cause: " + ", ".join("%s %d" % kv for kv in deaths.most_common()))


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def save(path, results):
    with open(path, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--serve", default=DEFAULT_SERVE, help="command that starts one game's terminal socket")
        p.add_argument("--seeds", default="1-12")
        p.add_argument("--secs", type=float, default=240, help="wall time per game")
        p.add_argument("--outer", choices=("resume", "recommended"), default="resume")
        p.add_argument("--decide", default="none", help="decision endpoint URL, or none (rules only)")
        p.add_argument("--set", action="append", default=[], metavar="K=V", help="extra settings for every game")
        p.add_argument("--parallel", type=int, default=2, help="games at once (at most 2)")
        p.add_argument("--keep", action="store_true", help="keep each game's state directory")
        if name == "run":
            p.add_argument("--harness", default=os.path.join(ROOT, "nethack_harness.py"))
            p.add_argument("--label", default="run")
            p.add_argument("--out", required=True)
        else:
            p.add_argument("--base", required=True)
            p.add_argument("--head", required=True)
            p.add_argument("--out-dir", default=".")
    r = sub.add_parser("report")
    r.add_argument("files", nargs="+")
    a = ap.parse_args()
    if a.cmd == "run":
        results = run_games(a, a.harness, a.label)
        save(a.out, results)
        print_report({a.label: results})
    elif a.cmd == "compare":
        sets = {}
        for ref in (a.base, a.head):
            path = worktree(ref)
            try:
                sets[ref] = run_games(a, os.path.join(path, "nethack_harness.py"), ref)
                save(os.path.join(a.out_dir, "bench-%s.jsonl" % re.sub(r"\W", "_", ref)), sets[ref])
            finally:
                subprocess.run(["git", "-C", ROOT, "worktree", "remove", "--force", path])
        print_report(sets)
    else:
        print_report({os.path.basename(f): load(f) for f in a.files})


if __name__ == "__main__":
    main()
