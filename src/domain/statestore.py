"""
Shared state store (platform persistence, issue #23 phase 1): one small key-value/document API
with two interchangeable backends, so NF state survives restarts WITHOUT changing default behavior.

Contract:
  open_store(nf_name) -> StateStore; store.collection(name) -> Collection with get/put/delete/
  keys/values/items over JSON-serializable values, listed in insertion order (an update keeps a
  key's original position). Namespaced collections keep one NF's state kinds apart.

Backends (chosen by environment, per NF process, at open_store time):
  TELCO_STATE_DIR unset (THE DEFAULT)   in-memory dicts: exactly today's behavior, clean slate
                                        per run, no files written, restarts forget everything.
                                        Every pre-existing acceptance spec runs this path.
  TELCO_STATE_DIR=/some/dir             sqlite3 (stdlib, in-charter) at <dir>/<nf_name>.db.
                                        Each NF owns its OWN db file - one writer process per
                                        file, never shared across processes. WAL journal mode +
                                        check_same_thread=False + one process-wide lock per store
                                        covers the NF's ThreadingHTTPServer handler threads and
                                        background loops.

Phase 1 state owners (this module's only users today): UDM/UDR provisioned subscribers, BSS
product orders + identity counter, OSS service orders + ordered services/workbenches, billing
usage + charges + counter baselines, both agents' proposals + audit events, NSMF slice
templates + instances and the SMF's dynamic UPF-selection rules (epic #11).
PHASE 2 (issue #23): inventory, catalog, sessions where meaningful - intentionally NOT persisted
yet (live-derived views and ephemeral session state stay in memory).
"""

import json
import os
import sqlite3
import threading
from pathlib import Path

ENV_VAR = "TELCO_STATE_DIR"


class Collection:
    """One namespaced key -> JSON-document map inside a StateStore."""

    def __init__(self, backend, name):
        self._backend = backend
        self._name = name

    def get(self, key, default=None):
        value = self._backend.get(self._name, key)
        return default if value is None else value

    def put(self, key, value):
        self._backend.put(self._name, key, value)

    def delete(self, key):
        self._backend.delete(self._name, key)

    def keys(self):
        return self._backend.keys(self._name)

    def values(self):
        return [v for _, v in self._backend.items(self._name)]

    def items(self):
        return self._backend.items(self._name)

    def __contains__(self, key):
        return self._backend.get(self._name, key) is not None

    def __len__(self):
        return len(self._backend.keys(self._name))


class _MemoryBackend:
    """Default backend: plain dicts, exactly the pre-persistence in-memory behavior."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def _col(self, collection):
        return self._data.setdefault(collection, {})

    def get(self, collection, key):
        with self._lock:
            return self._col(collection).get(key)

    def put(self, collection, key, value):
        # round-trip through JSON so both backends store detached, JSON-clean copies
        with self._lock:
            self._col(collection)[key] = json.loads(json.dumps(value))

    def delete(self, collection, key):
        with self._lock:
            self._col(collection).pop(key, None)

    def keys(self, collection):
        with self._lock:
            return list(self._col(collection).keys())

    def items(self, collection):
        with self._lock:
            return [(k, json.loads(json.dumps(v))) for k, v in self._col(collection).items()]


class _SqliteBackend:
    """sqlite3 backend: one db file per NF, WAL mode, one lock over the shared connection."""

    def __init__(self, db_path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "  seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  collection TEXT NOT NULL,"
            "  key TEXT NOT NULL,"
            "  value TEXT NOT NULL,"
            "  UNIQUE (collection, key))")
        self._conn.commit()

    def get(self, collection, key):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE collection = ? AND key = ?",
                (collection, key)).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, collection, key, value):
        with self._lock:
            # ON CONFLICT UPDATE keeps the original seq, so an updated key keeps its
            # insertion-order position (matching dict semantics in the memory backend)
            self._conn.execute(
                "INSERT INTO kv (collection, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (collection, key) DO UPDATE SET value = excluded.value",
                (collection, key, json.dumps(value)))
            self._conn.commit()

    def delete(self, collection, key):
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE collection = ? AND key = ?",
                               (collection, key))
            self._conn.commit()

    def keys(self, collection):
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM kv WHERE collection = ? ORDER BY seq", (collection,)).fetchall()
        return [r[0] for r in rows]

    def items(self, collection):
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM kv WHERE collection = ? ORDER BY seq",
                (collection,)).fetchall()
        return [(k, json.loads(v)) for k, v in rows]


class StateStore:
    def __init__(self, backend, persistent, location):
        self._backend = backend
        self.persistent = persistent   # True only when sqlite3-backed (TELCO_STATE_DIR set)
        self.location = location       # db file path, or None in memory mode

    def collection(self, name):
        return Collection(self._backend, name)


def open_store(nf_name):
    """The one entry point: env decides the backend, the store API is identical either way."""
    state_dir = os.environ.get(ENV_VAR)
    if not state_dir:
        return StateStore(_MemoryBackend(), persistent=False, location=None)
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / f"{nf_name}.db"
    return StateStore(_SqliteBackend(db_path), persistent=True, location=str(db_path))
