#!/usr/bin/env python3
"""Run the nethack_harness command-line interface."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nethack_harness.control import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main() or 0)
