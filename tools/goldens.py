"""Render every screen fixture through Play.view and write tests/goldens/<fixture>.txt.

Run from the repository root with `python3 -I tools/goldens.py`. Each fixture is shown to a fresh Play over a
scripted terminal that holds only that screen, so the view is the first look at a new game: no farlooks, no
underfoot look, nothing remembered from before. A fixture the view refuses is written as `error: <message>`.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nethack_harness.play import Play  # noqa: E402
from nethack_harness.screen import View  # noqa: E402
from nethack_harness.session import Session  # noqa: E402
from nethack_harness.settings import Settings  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "screens"
GOLDENS = ROOT / "tests" / "goldens"


class ScriptedTerm:
    """A terminal that always shows one screen and accepts no input."""

    def __init__(self, view):
        self.screen = view

    def poll(self):
        return False

    def send(self, keys, before_send=None, separately=False):
        raise RuntimeError("goldens send no keys")

    def view(self):
        return self.screen

    def message_written(self, rows):
        return True


def render(path):
    data = json.loads(path.read_text())
    play = Play(Session(ScriptedTerm(View(**data)), None, None, Settings(max_action_steps=64)))
    try:
        return play.view()
    except ValueError as error:
        return "error: %s" % error


def main():
    GOLDENS.mkdir(exist_ok=True)
    fixtures = sorted(FIXTURES.glob("*.json"))
    for path in fixtures:
        (GOLDENS / (path.stem + ".txt")).write_text(render(path) + "\n")
    print("%d goldens written to %s" % (len(fixtures), os.path.relpath(GOLDENS, ROOT)))


if __name__ == "__main__":
    main()
