"""Transactional session state, command queue and decision records."""
from datetime import datetime, timezone
import fcntl
import json
import os
import sqlite3
import time
import zlib

INTENT_SCHEMA = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replies (id INTEGER PRIMARY KEY, created REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, type TEXT NOT NULL, source TEXT NOT NULL,
    request BLOB, response BLOB, choice TEXT, latency REAL, outcome BLOB, after_state BLOB, intent TEXT,
    CHECK ((type = 'decision' AND request IS NOT NULL) OR (type = 'intent' AND intent IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES records(id),
    keys TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inputs_decision ON inputs(decision_id);
"""


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def pack(value):
    """Decision payloads are compressed: each holds full observations, and sessions often live on small tmpfs."""
    return zlib.compress(encode(value).encode())


def unpack(blob):
    return json.loads(zlib.decompress(blob))


class Store:
    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        path = self.path("session.sqlite3")
        # SQLite's journal-mode change can fail immediately when two clients
        # create the database at once, despite the connection busy timeout.
        with open(self.path("schema.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.db = sqlite3.connect(path, timeout=5)
            try:
                os.chmod(path, 0o600)
                self.db.execute("PRAGMA foreign_keys=ON")
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.executescript(SCHEMA)
            except Exception:
                self.db.close()
                raise

    def path(self, name):
        return os.path.join(self.dir, name)

    def close(self):
        self.db.close()

    def read(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def write(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, encode(value)))

    def enqueue(self, command, **values):
        with self.db:
            return self.db.execute("INSERT INTO commands(body) VALUES (?)", (encode(dict(values, command=command)),)).lastrowid

    def pending(self):
        return [(row[0], json.loads(row[1])) for row in self.db.execute("SELECT id,body FROM commands ORDER BY id")]

    def acknowledge(self, sequence):
        with self.db:
            self.db.execute("DELETE FROM commands WHERE id=?", (sequence,))

    def reply(self, sequence, result):
        with self.db:
            self.db.execute("DELETE FROM replies WHERE created<?", (time.time() - 300,))
            self.db.execute("INSERT INTO replies VALUES (?,?,?)", (sequence, time.time(), encode(result)))

    def take_reply(self, sequence):
        with self.db:
            row = self.db.execute("SELECT body FROM replies WHERE id=?", (sequence,)).fetchone()
            if row:
                self.db.execute("DELETE FROM replies WHERE id=?", (sequence,))
        return json.loads(row[0]) if row else None

    def begin(self, source, request):
        with self.db:
            return self.db.execute("INSERT INTO records(created,type,source,request) VALUES (?,'decision',?,?)",
                                   (time.time(), source, pack(request))).lastrowid

    def intent(self, record):
        """Append a caller intent record; the harness stores it verbatim and never interprets it."""
        now = time.time()
        body = dict(record, schema=INTENT_SCHEMA,
                    recorded_at=datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"))
        with self.db:
            return self.db.execute("INSERT INTO records(created,type,source,intent) VALUES (?,'intent','caller',?)",
                                   (now, encode(body))).lastrowid

    def choice(self, number, choice, response, latency):
        with self.db:
            self.db.execute("UPDATE records SET choice=?,response=?,latency=? WHERE id=?",
                            (choice, pack(response), latency, number))

    def input(self, number, keys, source):
        with self.db:
            return self.db.execute("INSERT INTO inputs(decision_id,keys,source,status) VALUES (?,?,?,'pending')",
                                   (number, keys, source)).lastrowid

    def input_status(self, number, status):
        with self.db:
            self.db.execute("UPDATE inputs SET status=? WHERE id=?", (status, number))

    def finish(self, number, outcome, after):
        with self.db:
            self.db.execute("UPDATE records SET outcome=?,after_state=? WHERE id=?",
                            (pack(outcome), pack(after), number))

    def records(self, after=0):
        """Decision and caller intent records in their shared sequence."""
        fields = ("id", "created", "type", "source", "request", "response", "choice", "latency", "outcome", "after",
                  "intent")
        for row in self.db.execute("SELECT * FROM records WHERE id>? ORDER BY id", (after,)):
            record = dict(zip(fields, row))
            if record["type"] == "intent":
                yield dict({key: record[key] for key in ("id", "created", "type", "source")},
                           intent=json.loads(record["intent"]))
                continue
            del record["intent"]
            for key in ("request", "response", "outcome", "after"):
                if record[key] is not None:
                    record[key] = unpack(record[key])
            record["inputs"] = [dict(zip(("keys", "source", "status"), item)) for item in self.db.execute(
                "SELECT keys,source,status FROM inputs WHERE decision_id=? ORDER BY id", (record["id"],))]
            yield record
