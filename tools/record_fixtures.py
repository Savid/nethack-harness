#!/usr/bin/env python3
"""Capture one terminal observation as a JSON fixture."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nethack_harness.transport import Term  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket")
    parser.add_argument("out")
    args = parser.parse_args()
    term = Term(args.socket)
    term.sync()
    view = term.view()
    with open(args.out, "w") as stream:
        json.dump({"rows": view.rows, "fg": view.fgs, "bold": view.bolds, "rev": view.revs,
                   "cursor": view.cursor}, stream, separators=(",", ":"))
        stream.write("\n")


if __name__ == "__main__":
    main()
