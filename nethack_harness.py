#!/usr/bin/env python3
"""Stable entry point: runs the nethack_harness package that sits beside this file.

    python3 nethack_harness.py help      # the capability map
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nethack_harness.control import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main() or 0)
