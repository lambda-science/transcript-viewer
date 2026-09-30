"""Where annotations are kept, and how they are read back.

The library used to be one JSON document rewritten in full on every change. A
real library here holds 5,199 records of which **six** carry an annotation: the
rest exist only to record where a file was downloaded from. Starring one
transcript rewrote 2.3 MB, and two processes doing it at once lost one of the
two — the atomic replace kept the file intact and threw half the work away.

So provenance and opinion are separated. `transcripts` is what a file is and
where it came from; `labels` is what a team decided about it; `marks` is what
one person did with it. That split is what makes a shared instance possible:
labels are one set everyone sees, marks belong to whoever made them.

Two stores, one schema. SQLite is the default and is in the standard library,
which matters — this package's only runtime dependency is the converter, and it
stays that way for anyone running it on a laptop. PostgreSQL is an extra, and is
not a nicety once more than one copy is serving: two copies with two SQLite
files means one person's stars exist on one of them and not the other, which is
not a slower answer but a wrong one.

The SQL is written once, in SQLite's dialect, and the placeholders are
translated. That is the whole of the portability layer, and it is deliberately
this thin: a query builder would be a second language to learn for the sake of
four tables.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import deploy, store

DB_PATH = store.ROOT / "library.db"

# Bumped when the shape changes in a way an existing store has to be told about.
SCHEMA = 2

_SETUP = """
CREATE TABLE IF NOT EXISTS transcripts (
  content_hash TEXT PRIMARY KEY,
  source       TEXT NOT NULL DEFAULT '',
  into_dir     TEXT NOT NULL DEFAULT '',
  added        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
  content_hash TEXT PRIMARY KEY,
  title        TEXT NOT NULL DEFAULT '',
  note         TEXT NOT NULL DEFAULT '',
  summaries    TEXT NOT NULL DEFAULT '{}',
  by_user      TEXT NOT NULL DEFAULT '',
  at           TEXT NOT NULL
);

-- Tags get a table of their own because they are the one annotation asked
-- about across every transcript at once: the sidebar counts them. As JSON in a
-- column that question is a full scan and a parse per row; here it is a GROUP
-- BY. Notes and starred steps stay as documents because they are only ever
-- read for the transcript in hand.
--
-- `position` because the order someone typed their tags in is theirs to keep,
-- and insertion order is not a thing a table has. SQLite would answer it with
-- `rowid`, which is not a column anywhere else.
CREATE TABLE IF NOT EXISTS tags (
  content_hash TEXT NOT NULL,
  tag          TEXT NOT NULL,
  position     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_hash, tag)
);
CREATE INDEX IF NOT EXISTS tags_by_tag ON tags(tag);

-- `user` is '' for the local single user rather than NULL: NULL never equals
-- NULL, so a nullable column in a primary key would let one person accumulate
-- a new row per star.
CREATE TABLE IF NOT EXISTS marks (
  content_hash  TEXT NOT NULL,
  user_id       TEXT NOT NULL DEFAULT '',
  starred       INTEGER NOT NULL DEFAULT 0,
  starred_steps TEXT NOT NULL DEFAULT '[]',
  at            TEXT NOT NULL,
  PRIMARY KEY (content_hash, user_id)
);
CREATE INDEX IF NOT EXISTS marks_by_user ON marks(user_id);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""

# One connection per thread. The server is threaded, a sqlite3 connection may
# not be shared across threads, and a psycopg one is not safe to use from two at
# once either.
_local = threading.local()

_PLACEHOLDER = re.compile(r"\?")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _where(value: Path | str) -> str:
    """A store's address as written, whether it arrived as a path or a URL."""
    return value if isinstance(value, str) else str(value)


def _postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


class Store:
    """A connection, and the two dialect differences worth hiding.

    Everything else the four tables need is spelled the same in both: upserts,
    `EXISTS`, indexes, `IF NOT EXISTS`. Only placeholders and the handful of
    setup statements actually differ, so only those are translated.
    """

    def __init__(self, conn: Any, postgres: bool):
        self._conn = conn
        self.postgres = postgres

    def _sql(self, sql: str) -> str:
        return _PLACEHOLDER.sub("%s", sql) if self.postgres else sql

    def execute(self, sql: str, params: Any = ()) -> Any:
        cursor = self._conn.cursor() if self.postgres else self._conn
        return cursor.execute(self._sql(sql), params)

    def executemany(self, sql: str, rows: Any) -> Any:
        rows = list(rows)
        if not rows:
            return None
        cursor = self._conn.cursor() if self.postgres else self._conn
        return cursor.executemany(self._sql(sql), rows)

    def script(self, sql: str) -> None:
        """Run several statements at once.

        Not by splitting on semicolons — a comment or a string holding one
        turns a statement into two, and the halves are not SQL. Each driver
        already knows how to read a script; this asks it to.
        """
        if self.postgres:
            self._conn.cursor().execute(sql)
        else:
            self._conn.executescript(sql)

    def columns(self, table: str) -> set[str]:
        """What a table actually has, which is what a migration should read.

        A version number says what a store was last told it is. This says what
        it is. The two came apart the first time it mattered: a store stamped
        by an older copy recorded its version somewhere the newer copy did not
        look, so it was taken for new, stamped as current, and never migrated —
        leaving a store that claimed to be something it was not.
        """
        if self.postgres:
            rows = self.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_name = ?",
                (table,),
            ).fetchall()
            return {r["name"] for r in rows}
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def begin(self) -> None:
        """Take the write lock now rather than when the first write lands.

        `update` reads a record and writes it back; without this two of them can
        both read, both write, and the later silently undo the earlier.
        """
        if not self.postgres:
            self._conn.execute("BEGIN IMMEDIATE")

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        if exc[0] is None:
            self._conn.commit()
        else:
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _open(where: str) -> Store:
    if _postgres(where):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "this deployment is configured for PostgreSQL; install "
                "transcript-viewer[postgres]"
            ) from exc
        conn = psycopg.connect(where, row_factory=dict_row, autocommit=False)
        return Store(conn, postgres=True)

    path = Path(where)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(where, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # A write waits for a reader rather than failing outright; the default is to
    # raise `database is locked` immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    return Store(conn, postgres=False)


def connect(where: Path | str | None = None) -> Store:
    """The store for this thread, opened and set up on first use.

    A path or a URL. Both, because a caller that has one should not have to
    know which the other would be — and wrapping a URL in `Path` to satisfy a
    narrower type silently destroys it: `Path` collapses the `//` in
    `postgresql://` to one slash, which stops looking like a URL and starts
    looking like a relative filename. That produced a SQLite file named after
    the database it was supposed to be, and a test comparing the two engines
    that was quietly comparing SQLite with itself.
    """
    where = (
        _where(where) if where is not None
        else (deploy.current().database or str(DB_PATH))
    )
    have = getattr(_local, "conns", None)
    if have is None:
        have = _local.conns = {}
    if where not in have:
        conn = _open(where)
        _prepare(conn)
        have[where] = conn
    return have[where]


def _migrate(conn: Store) -> None:
    """Bring a store up to date, by looking at it rather than asking it.

    Every step is written to be safe to run against a store that has already
    had it. That is what lets this ignore the recorded version, which is the
    one thing about a store that can be wrong — and was.

    Only additive or renaming, never dropping: someone's annotations are on the
    other side of this, and a migration that rebuilt would be correct and
    unforgivable.
    """
    # A table with no columns is a table that is not there yet: this runs before
    # the setup script, which builds the missing ones already in their current
    # shape. Nothing to bring forward, and asking anyway is an error.
    marks = conn.columns("marks")
    if marks and "user" in marks and "user_id" not in marks:
        # `user` is a reserved word in PostgreSQL, so the column was renamed
        # when the second engine arrived.
        conn.execute("ALTER TABLE marks RENAME COLUMN user TO user_id")

    tags = conn.columns("tags")
    if tags and "position" not in tags:
        # Insertion order used to be read off SQLite's `rowid`, which nothing
        # else has.
        conn.execute(
            "ALTER TABLE tags ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
        )



def _prepare(conn: Store) -> None:
    # Before the setup script, not after: that script creates an index on a
    # column the migration is about to produce, so against an older store it is
    # the setup that fails rather than anything the migration does.
    _migrate(conn)
    conn.script(_SETUP)
    at = conn.execute("SELECT version FROM schema_version").fetchone()
    if at is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA,))
    conn._conn.commit()


def close(where: Path | str | None = None) -> None:
    """Drop this thread's connection. Tests reopen against a new store."""
    where = (
        _where(where) if where is not None
        else (deploy.current().database or str(DB_PATH))
    )
    conn = (getattr(_local, "conns", None) or {}).pop(where, None)
    if conn is not None:
        conn.close()


def loads(raw: Any, fallback: Any) -> Any:
    """A JSON column, or the fallback if it is missing or damaged.

    Same reasoning as `store.read_json`: one bad byte should cost the field it
    is in, not the page.
    """
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return fallback
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def adopt_json(db: Path | None = None, source: Path | None = None) -> int:
    """Import a `library.json` left by an earlier version. Returns rows moved.

    Runs once: the old file is renamed beside itself rather than deleted, and
    its absence is what stops this happening twice. A format change should be
    even less able to look like data loss than a rename was, and the file is
    two megabytes — keeping it costs nothing next to explaining where somebody's
    notes went.

    The split happens here, which is why this is worth doing at all. Of 5,199
    records in a real library, 5,193 exist only because a file was downloaded
    from somewhere; six carry an annotation. Provenance goes to `transcripts`
    and the six become rows in `labels`, `tags` and `marks`.
    """
    source = source or (store.ROOT / "library.json")
    if not source.exists():
        return 0

    entries = store.read_json(source).get("entries")
    if not isinstance(entries, dict):
        entries = {}

    conn = connect(db)
    moved = 0
    with conn:
        conn.begin()
        for key, record in entries.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                continue
            moved += 1
            stamp = str(record.get("added") or now())
            conn.execute(
                "INSERT INTO transcripts(content_hash, source, into_dir, added) "
                "VALUES (?,?,?,?) ON CONFLICT(content_hash) DO NOTHING",
                (
                    key,
                    str(record.get("source") or ""),
                    str(record.get("into") or ""),
                    stamp,
                ),
            )

            title = str(record.get("title") or "")
            note = str(record.get("note") or "")
            summaries = record.get("summaries") or {}
            if title or note or summaries:
                conn.execute(
                    "INSERT INTO labels(content_hash, title, note, summaries, at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(content_hash) DO NOTHING",
                    (key, title, note, json.dumps(summaries), stamp),
                )

            for position, tag in enumerate(record.get("tags") or []):
                if isinstance(tag, str) and tag.strip():
                    conn.execute(
                        "INSERT INTO tags(content_hash, tag, position) VALUES (?,?,?) "
                        "ON CONFLICT(content_hash, tag) DO NOTHING",
                        (key, tag.strip().lower()[:40], position),
                    )

            starred = bool(record.get("starred"))
            steps = record.get("starred_steps") or []
            if starred or steps:
                conn.execute(
                    "INSERT INTO marks(content_hash, user_id, starred, starred_steps,"
                    " at) VALUES (?,'',?,?,?) "
                    "ON CONFLICT(content_hash, user_id) DO NOTHING",
                    (key, int(starred), json.dumps([str(s) for s in steps]), stamp),
                )

    source.rename(source.with_suffix(".json.imported"))
    return moved
