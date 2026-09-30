"""What you decide about a transcript, as opposed to what it is.

Titles, tags and stars live here; paths, sizes and formats stay in
atif-make's index. Keeping them apart means re-indexing can never destroy an
annotation, and an annotation survives its file being re-scanned from somewhere
new — records are keyed by content, so a file that moves keeps everything.

The records are rows now rather than one JSON document, and callers cannot tell:
these functions take and return the shapes they always did, which is what let
the storage change without touching the server's twenty-seven call sites. What
changed is the cost. Setting one star used to rewrite every record in the
library — 2.3 MB, in the one measured here, to store one bit.

The layout is in `db.py`. It keeps where a file came from apart from what anyone
decided about it, which shows up here mostly as pruning: an annotation taken
back to its default leaves no row behind, but the provenance of a download
survives, because a refresh needs it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import db

VERSION = 1

# Two viewers can be open at once. SQLite settles writes between processes; this
# keeps one process from interleaving its own read-modify-write.
_lock = threading.Lock()

# The local single user. Identity arrives with the shared deployment; until then
# every row belongs to the one person at the keyboard, and '' is that person —
# not NULL, which would never match itself in a key.
ME = ""

_DEFAULTS: dict[str, Any] = {
    "title": "",
    # Where a fetched session came from. This is what places it in the tree, what
    # the tree links back to, and what a refresh asks again.
    "source": "",
    # The folder that fetch was told to download into, so a refresh puts new
    # files beside the old ones rather than somewhere else.
    "into": "",
    "tags": [],
    "starred": False,
    "note": "",
    # Individual steps starred inside a transcript. Held as the viewer's step
    # keys — "12" at the top level, "<trajectory>-3" inside a subagent, whose
    # ids restart at 1 — so a star cannot land on the wrong step.
    "starred_steps": [],
    # Summaries the model has already produced, by tool-call id. Cached here so
    # a call is paid for once, and so they survive a restart like any other note.
    "summaries": {},
}

# One row of the join every read starts from. Written once because `load` and
# `get` ask the same question of one row and of all of them, and two spellings
# of it would drift.
_READ = (
    "SELECT t.content_hash AS k, t.source, t.into_dir, t.added, "
    "       l.title, l.note, l.summaries, "
    "       m.starred, m.starred_steps "
    "FROM transcripts t "
    "LEFT JOIN labels l ON l.content_hash = t.content_hash "
    "LEFT JOIN marks  m ON m.content_hash = t.content_hash AND m.user_id = ?"
)


def _record(row: Any, tags_: list[str]) -> dict:
    return {
        **_DEFAULTS,
        "source": row["source"] or "",
        "into": row["into_dir"] or "",
        "title": row["title"] or "",
        "note": row["note"] or "",
        "summaries": db.loads(row["summaries"], {}),
        "starred": bool(row["starred"]),
        "starred_steps": db.loads(row["starred_steps"], []),
        "tags": tags_,
        "added": row["added"],
    }


def _clean_tags(value: Any) -> list[str]:
    """Tags are short, unique, lower-case labels — order preserved."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tag in value:
        if not isinstance(tag, str):
            continue
        tag = tag.strip().lower()[:40]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out[:20]


def _clean_steps(value: Any) -> list[str]:
    """Step keys, unique and order-preserved. Kept as strings because a
    subagent's key is not a number."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        key = str(item).strip()[:120]
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out[:2000]


def _clean(name: str, value: Any) -> Any:
    if name == "tags":
        return _clean_tags(value)
    if name == "starred_steps":
        return _clean_steps(value)
    if name == "summaries":
        if not isinstance(value, dict):
            return {}
        return {
            str(k)[:120]: str(v)[:4000]
            for k, v in value.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    if name == "starred":
        return bool(value)
    return str(value).strip()[:500] if value is not None else ""


def load(path: Path | str | None = None) -> dict[str, dict]:
    """Every annotation, by key.

    Two queries and one pass, because this answers a whole page load. Asking per
    session was the difference between a page appearing and a page taking four
    seconds when the library was a parsed document; as rows it would be a query
    per session, which is worse.
    """
    conn = db.connect(path)
    by_key = {row["k"]: _record(row, []) for row in conn.execute(_READ, (ME,))}
    for row in conn.execute("SELECT content_hash, tag FROM tags ORDER BY content_hash, position"):
        record = by_key.get(row["content_hash"])
        if record is not None:
            record["tags"].append(row["tag"])
    return by_key


def get(key: str, path: Path | str | None = None) -> dict:
    """One record, with defaults filled in, whether or not it is annotated."""
    conn = db.connect(path)
    row = conn.execute(f"{_READ} WHERE t.content_hash = ?", (ME, key)).fetchone()
    if row is None:
        return {**_DEFAULTS}
    return _record(
        row,
        [
            r["tag"]
            for r in conn.execute(
                "SELECT tag FROM tags WHERE content_hash = ? ORDER BY position", (key,)
            )
        ],
    )


def update(key: str, path: Path | str | None = None, **fields: Any) -> dict:
    """Merge fields into one record and persist. Returns the merged record."""
    if not key:
        raise ValueError("a key is required")
    conn = db.connect(path)
    with _lock, conn:
        conn.begin()
        record = get(key, path)
        for name, value in fields.items():
            if name in _DEFAULTS:
                record[name] = _clean(name, value)
        stamp = record.get("added") or db.now()

        conn.execute(
            "INSERT INTO transcripts(content_hash, source, into_dir, added) "
            "VALUES (?,?,?,?) ON CONFLICT(content_hash) DO UPDATE SET "
            "source = excluded.source, into_dir = excluded.into_dir",
            (key, record["source"], record["into"], stamp),
        )

        # Pruning is per table now, which is the behaviour anyone would expect:
        # clearing a note should not cost you your stars.
        if record["title"] or record["note"] or record["summaries"]:
            conn.execute(
                "INSERT INTO labels(content_hash, title, note, summaries, by_user, at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(content_hash) DO UPDATE SET "
                "title = excluded.title, note = excluded.note, "
                "summaries = excluded.summaries, by_user = excluded.by_user, "
                "at = excluded.at",
                (
                    key,
                    record["title"],
                    record["note"],
                    json.dumps(record["summaries"]),
                    ME,
                    db.now(),
                ),
            )
        else:
            conn.execute("DELETE FROM labels WHERE content_hash = ?", (key,))

        conn.execute("DELETE FROM tags WHERE content_hash = ?", (key,))
        conn.executemany(
            "INSERT INTO tags(content_hash, tag, position) VALUES (?,?,?)",
            [(key, tag, at) for at, tag in enumerate(record["tags"])],
        )

        if record["starred"] or record["starred_steps"]:
            conn.execute(
                "INSERT INTO marks(content_hash, user_id, starred, starred_steps, at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(content_hash, user_id) DO UPDATE SET "
                "starred = excluded.starred, "
                "starred_steps = excluded.starred_steps, at = excluded.at",
                (
                    key,
                    ME,
                    int(record["starred"]),
                    json.dumps(record["starred_steps"]),
                    db.now(),
                ),
            )
        else:
            conn.execute(
                "DELETE FROM marks WHERE content_hash = ? AND user_id = ?", (key, ME)
            )

        _forget_if_bare(conn, key)
        record["added"] = stamp
    return record


def _forget_if_bare(conn: Any, key: str) -> None:
    """Drop a transcript nobody said anything about and nothing came from.

    Provenance is worth keeping on its own: a row recording where a file was
    downloaded from is what a refresh asks again. So this only fires when the
    record has neither a source nor an opinion attached.
    """
    row = conn.execute(
        "SELECT source, into_dir FROM transcripts WHERE content_hash = ?", (key,)
    ).fetchone()
    if row is None or row["source"] or row["into_dir"]:
        return
    # Positional, and the key given three times. Named placeholders are spelled
    # differently by every driver, and one style throughout is what keeps the
    # translation to a single substitution.
    # Every column is named and read by name. SQLite's rows answer to a number
    # as well; PostgreSQL's, as this reads them, do not — so a positional read
    # works until the day it is asked of the other engine.
    held = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM labels WHERE content_hash = ?) "
        "    OR EXISTS(SELECT 1 FROM tags   WHERE content_hash = ?) "
        "    OR EXISTS(SELECT 1 FROM marks  WHERE content_hash = ?) AS held",
        (key, key, key),
    ).fetchone()["held"]
    if not held:
        conn.execute("DELETE FROM transcripts WHERE content_hash = ?", (key,))


def remove(key: str, path: Path | str | None = None) -> bool:
    """Forget one record. True when there was something to forget."""
    conn = db.connect(path)
    with _lock, conn:
        conn.begin()
        existed = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM transcripts WHERE content_hash = ?) AS there",
            (key,),
        ).fetchone()["there"]
        for table in ("labels", "tags", "marks", "transcripts"):
            conn.execute(f"DELETE FROM {table} WHERE content_hash = ?", (key,))
    return bool(existed)


def clear(path: Path | str | None = None) -> None:
    """Forget every record. What the library's own clear-all is made of."""
    conn = db.connect(path)
    with _lock, conn:
        conn.begin()
        for table in ("labels", "tags", "marks", "transcripts"):
            conn.execute(f"DELETE FROM {table}")


def tags(path: Path | str | None = None) -> list[tuple[str, int]]:
    """Every tag with a count, most used first."""
    return [
        (row["tag"], row["n"])
        for row in db.connect(path).execute(
            "SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag ORDER BY n DESC, tag"
        )
    ]


def decorate(rows: list[dict], path: Path | str | None = None) -> list[dict]:
    """Fold annotations into index rows so a client gets one object per session."""
    entries = load(path)
    for row in rows:
        row.update({**_DEFAULTS, **entries.get(row.get("key", ""), {})})
    return rows
