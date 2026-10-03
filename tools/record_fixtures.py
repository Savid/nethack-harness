#!/usr/bin/env python3
"""Record screen fixtures from a running game, or refresh their expectations.

    python3 tools/record_fixtures.py record SOCK OUTDIR --name seed7 [--every 25] [--decisions 400]
    python3 tools/record_fixtures.py regen DIR      # rewrite each fixture's "expect" line from the current rules

record plays the game with the rules alone (no decision model, escalations logged and ignored) and saves the
screen, with the farlook answers the loop needed, every N decisions. regen ranks each saved screen with a fresh
pilot; review the diff before committing: it is the change in behaviour.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
import nethack_harness as nh  # noqa: E402
import fixturefmt  # noqa: E402


def record(a):
    term = nh.Term(a.socket)
    term.sync()
    p = nh.Pilot(term, None)
    nh.CFG["auto"] = 1
    looks, saved = {}, 0
    plain_farlook, plain_actions = p.farlook, p.actions

    def farlook(v, pos, cache=True):
        name = plain_farlook(v, pos, cache)
        looks[pos] = name
        return name

    def actions(v, c):
        nonlocal saved
        acts = plain_actions(v, c)
        if p.decisions % a.every == 0 and v.normal:
            path = os.path.join(a.outdir, "%s-%04d.screen" % (a.name, p.decisions))
            with open(path, "w") as f:
                f.write(fixturefmt.dump(v, looks.items()))
            saved += 1
        looks.clear()
        return acts

    p.farlook, p.actions = farlook, actions
    os.makedirs(a.outdir, exist_ok=True)
    while p.decisions < a.decisions:
        try:
            if p.step() == nh.policy.GAME_OVER or term.view().dead:
                break
        except nh.policy.Hard:
            continue
        except nh.Closed:
            break
    print("saved %d fixtures" % saved)


def regen(a):
    for name in sorted(os.listdir(a.dir)):
        if not name.endswith(".screen"):
            continue
        path = os.path.join(a.dir, name)
        args, lookups, _ = fixturefmt.load(open(path).read())
        keys, _ = fixturefmt.fresh_ranking(nh, args, lookups)
        view = nh.View(*args)
        with open(path, "w") as f:
            f.write(fixturefmt.dump(view, lookups.items(), keys))
        print(name, " ".join(keys))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("socket")
    r.add_argument("outdir")
    r.add_argument("--name", required=True)
    r.add_argument("--every", type=int, default=25)
    r.add_argument("--decisions", type=int, default=400)
    g = sub.add_parser("regen")
    g.add_argument("dir")
    a = ap.parse_args()
    record(a) if a.cmd == "record" else regen(a)


if __name__ == "__main__":
    main()
