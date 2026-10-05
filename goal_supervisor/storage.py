"""Atomic caller-side state; edits serialize at execution-window boundaries."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile


class Repository:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self

    def load(self):
        path = self.directory / "state.json"
        if not path.exists():
            raise ValueError("initialize this supervisor directory first")
        with path.open() as source:
            return json.load(source)

    def save(self, value):
        encoded = json.dumps(value, indent=2, allow_nan=False)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as output:
                temporary = output.name
                output.write(encoded + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.directory / "state.json")
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
