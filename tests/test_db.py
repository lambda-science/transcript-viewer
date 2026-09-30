"""The store the library sits on, and the import that fills it.

The thing worth testing here is not SQL. It is the split: a library where
almost every record exists only to say where a file came from should import as
almost no annotations, and one person's star should not be a rewrite of
everyone else's provenance.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from transcript_viewer import db


@pytest.fixture
def room(tmp_path):
    """A database of its own, closed afterwards so the next test reopens."""
    path = tmp_path / "library.db"
    yield path
    db.close(path)


def test_a_fresh_file_is_a_usable_library(room):
    conn = db.connect(room)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"transcripts", "labels", "tags", "marks"} <= tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == (
        db.SCHEMA
    )


def test_readers_are_not_blocked_by_a_writer(room):
    """A page load asks about every session; starring one must not stop it."""
    assert db.connect(room).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_one_person_starring_leaves_another_persons_star_alone(room):
    """The whole point of a row per person rather than a document per file."""
    conn = db.connect(room)
    for who in ("ann@example.com", "bo@example.com"):
        conn.execute(
            "INSERT INTO marks(content_hash, user_id, starred, at) VALUES ('h',?,1,?)",
            (who, db.now()),
        )
    conn.execute("UPDATE marks SET starred=0 WHERE user_id='bo@example.com'")

    kept = dict(conn.execute("SELECT user_id, starred FROM marks").fetchall())
    assert kept == {"ann@example.com": 1, "bo@example.com": 0}


def test_the_local_user_gets_one_row_however_often_they_star(room):
    """`user` is '' rather than NULL because NULL never equals NULL, which
    would quietly give the single local user a new row every time."""
    conn = db.connect(room)
    for _ in range(3):
        conn.execute(
            "INSERT INTO marks(content_hash, user_id, starred, at) VALUES ('h','',1,?) "
            "ON CONFLICT(content_hash, user_id) DO UPDATE SET starred=1",
            (db.now(),),
        )
    assert conn.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 1


def _library(path, entries):
    path.write_text(json.dumps({"version": 1, "entries": entries}))


def test_an_import_keeps_provenance_apart_from_opinion(room, tmp_path):
    """A real library holds 5,199 records of which six are annotations; the
    rest say only where a download came from. Importing them as annotations is
    what made starring cost a two-megabyte rewrite."""
    source = tmp_path / "library.json"
    _library(
        source,
        {
            f"h{i}": {
                "source": "s3://b/x",
                "into": "/d",
                "added": "2026-01-01T00:00:00Z",
            }
            for i in range(50)
        }
        | {
            "kept": {
                "title": "worth a name",
                "tags": ["Alpha", "alpha", "beta"],
                "starred": True,
                "starred_steps": ["3"],
                "note": "why",
                "source": "s3://b/y",
            },
        },
    )

    moved = db.adopt_json(db=room, source=source)
    conn = db.connect(room)
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("transcripts", "labels", "tags", "marks")
    }

    assert moved == 51
    assert counts == {"transcripts": 51, "labels": 1, "tags": 2, "marks": 1}


def test_an_import_survives_the_process_that_did_it(room, tmp_path):
    """Counting rows on the connection that wrote them proves nothing."""
    source = tmp_path / "library.json"
    _library(source, {"h": {"title": "kept", "source": "s3://b/x"}})
    db.adopt_json(db=room, source=source)
    db.close(room)

    fresh = sqlite3.connect(room)
    assert fresh.execute("SELECT title FROM labels").fetchone()[0] == "kept"
    fresh.close()


def test_the_file_that_was_imported_is_kept(room, tmp_path):
    """A format change should look even less like data loss than a rename did,
    and its absence is what stops the import running twice."""
    source = tmp_path / "library.json"
    _library(source, {"h": {"title": "kept"}})

    assert db.adopt_json(db=room, source=source) == 1
    assert not source.exists()
    assert source.with_suffix(".json.imported").exists()
    assert db.adopt_json(db=room, source=source) == 0


def test_a_damaged_library_imports_as_empty_rather_than_refusing(room, tmp_path):
    """Refusing to start because one byte is wrong is worse than starting
    empty — the same bargain `store.read_json` already makes."""
    source = tmp_path / "library.json"
    source.write_text("{not json")
    assert db.adopt_json(db=room, source=source) == 0


def test_nothing_is_imported_from_a_library_that_was_never_there(room, tmp_path):
    assert db.adopt_json(db=room, source=tmp_path / "absent.json") == 0


@pytest.mark.parametrize(
    ("raw", "fallback", "want"),
    [
        ('["a"]', [], ["a"]),
        ('{"k": "v"}', {}, {"k": "v"}),
        ("not json", [], []),
        ('"a string"', [], []),  # right JSON, wrong shape
        (None, {}, {}),
    ],
)
def test_a_damaged_column_costs_its_own_field_and_no_more(raw, fallback, want):
    assert db.loads(raw, fallback) == want


def test_the_sql_does_not_depend_on_implicit_row_order():
    import re
    from pathlib import Path

    # Only lines that are actually SQL. Scanning the whole file matches the
    # prose explaining why these two are avoided, which is a test that fails
    # for saying the right thing.
    speaks_sql = re.compile(
        r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|FROM|JOIN|WHERE|ON CONFLICT)\b"
    )
    root = Path(__file__).resolve().parent.parent / "src/transcript_viewer"
    for name in ("db.py", "library.py"):
        for line in (root / name).read_text().splitlines():
            bare = line.strip()
            if bare.startswith(("#", "--")) or not speaks_sql.search(line):
                continue
            assert "rowid" not in line.lower(), (
                f"{name} orders by `rowid`, which only SQLite has: {bare}"
            )
            if "RENAME COLUMN user TO user_id" in line:
                continue
            assert not re.search(r"\buser\b(?!_id)", line), (
                f"{name} still uses the old `user` column: {bare}"
            )


def test_a_store_is_migrated_on_what_it_is_not_on_what_it_claims(room):
    """The fault this actually had. Version 1 recorded itself in SQLite's
    `user_version`; version 2 looks in a table of its own. So a real v1 store
    was read as brand new, stamped as current, and never migrated — leaving a
    store that claimed to be something it was not, and a column the queries no
    longer knew the name of.

    Reproduced exactly: a v1 store, already mislabelled, still comes right.
    """
    made = sqlite3.connect(room)
    made.executescript(
        "CREATE TABLE marks (content_hash TEXT NOT NULL, user TEXT NOT NULL"
        " DEFAULT '', starred INTEGER NOT NULL DEFAULT 0,"
        " starred_steps TEXT NOT NULL DEFAULT '[]', at TEXT NOT NULL,"
        " PRIMARY KEY (content_hash, user));"
        "INSERT INTO marks VALUES ('h', '', 1, '[\"7\"]', 'then');"
        # Every read starts from `transcripts`, and nothing ever writes a mark
        # without one, so a store missing it would be testing a shape that
        # cannot occur.
        "CREATE TABLE transcripts (content_hash TEXT PRIMARY KEY,"
        " source TEXT NOT NULL DEFAULT '', into_dir TEXT NOT NULL DEFAULT '',"
        " added TEXT NOT NULL);"
        "INSERT INTO transcripts VALUES ('h', '', '', 'then');"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version VALUES (2);"  # already lying about itself
    )
    made.commit()
    made.close()

    from transcript_viewer import library

    record = library.get("h", room)

    assert record["starred"] is True, "a star written by the older store"
    assert record["starred_steps"] == ["7"]


def test_migrating_twice_is_the_same_as_migrating_once(room):
    """Every step has to be safe against a store that has already had it —
    that is what lets the recorded version be ignored."""
    db.connect(room)
    before = db.connect(room).columns("marks")
    db.close(room)
    after = db.connect(room).columns("marks")

    assert before == after == {
        "content_hash", "user_id", "starred", "starred_steps", "at",
    }


def test_an_older_store_is_brought_forward_rather_than_rebuilt(room):
    """Someone's annotations are on the other side of this. A migration that
    dropped and recreated would be correct and unforgivable."""
    made = sqlite3.connect(room)
    made.executescript(
        "CREATE TABLE tags (content_hash TEXT NOT NULL, tag TEXT NOT NULL,"
        " PRIMARY KEY (content_hash, tag));"
        "INSERT INTO tags VALUES ('h', 'security');"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version VALUES (1);"
    )
    made.commit()
    made.close()

    conn = db.connect(room)

    assert conn.execute("SELECT tag FROM tags").fetchone()["tag"] == "security"
    assert conn.execute("SELECT position FROM tags").fetchone()["position"] == 0
    assert conn.execute("SELECT version FROM schema_version").fetchone()[
        "version"
    ] == db.SCHEMA


class _Fake:
    """A connection that records what it was asked, and answers nothing."""

    def __init__(self):
        self.asked = []
        self.scripts = []

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.asked.append((sql, params))
        return self

    def executemany(self, sql, rows):
        self.asked.append((sql, list(rows)))
        return self

    def executescript(self, sql):
        self.scripts.append(sql)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_sql_reaches_sqlite_unchanged():
    sql = "INSERT INTO marks(content_hash, user_id) VALUES (?,?)"

    lite = _Fake()
    db.Store(lite).execute(sql, ("h", ""))
    assert lite.asked[0][0] == sql


def test_a_script_goes_through_the_engine_rather_than_a_split():
    """Splitting on semicolons turned the comments in the schema into
    statements, and the halves were not SQL."""
    lite = _Fake()
    db.Store(lite).script(db._SETUP)
    assert lite.scripts == [db._SETUP] and not lite.asked


def test_taking_the_write_lock_early_is_asked_of_sqlite():
    """`update` reads a record and writes it back, so two cannot read first."""
    lite = _Fake()
    db.Store(lite).begin()
    assert lite.asked[0][0] == "BEGIN IMMEDIATE"


def test_an_empty_executemany_asks_nothing():
    """`update` rewrites a record's tags by deleting and re-inserting them, and
    a record with no tags would otherwise send an INSERT with no rows."""
    fake = _Fake()
    db.Store(fake).executemany("INSERT INTO tags VALUES (?,?,?)", [])
    assert fake.asked == []


def _exercise(where) -> dict:
    """Put a store through its paces and report only what a caller can see.

    Returned so the complete sequence can be asserted without coupling each
    operation to storage details.
    """
    from transcript_viewer import library

    seen = {}
    library.update("h", where, title="Named", tags=["b", "a"], starred=True,
                   note="why", starred_steps=["7", "3"])
    seen["written"] = library.get("h", where)
    seen["tags"] = library.tags(where)

    library.update("h", where, note="")
    seen["note cleared, star kept"] = library.get("h", where)["starred"]

    library.update("h", where, source="s3://b/x")
    library.update("h", where, title="", tags=[], starred=False, starred_steps=[])
    # Provenance is not an opinion: a row saying where a file came from is what
    # a refresh asks again, so it survives the annotations being cleared.
    seen["after clearing"] = library.load(where)

    library.update("h", where, source="")
    seen["and once nothing is left"] = library.load(where)
    return seen


def test_sqlite_handles_the_full_library_sequence(tmp_path):
    lite = tmp_path / "library.db"
    on_sqlite = _exercise(lite)
    db.close(lite)

    assert on_sqlite["written"]["tags"] == ["b", "a"], "insertion order, not sorted"
    assert on_sqlite["and once nothing is left"] == {}


def test_a_database_path_is_normalised_to_text(tmp_path):
    assert db._where(tmp_path / "library.db") == str(tmp_path / "library.db")


def test_only_sqlite_placeholders_are_used():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src/transcript_viewer"
    for name in ("db.py", "library.py"):
        for line in (root / name).read_text().splitlines():
            if "SELECT" not in line and "INSERT" not in line and "DELETE" not in line:
                continue
            assert not re.search(r"= :\w", line), (
                f"{name} uses a named placeholder: {line.strip()}"
            )
