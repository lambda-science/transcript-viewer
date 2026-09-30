"""The library holds what you decided about a transcript. Losing it silently is
the worst failure here, so these lean on durability and on never destroying an
annotation by accident."""

import json

import pytest

from transcript_viewer import db, library


@pytest.fixture
def store(tmp_path):
    """The library's own database, closed afterwards so the next test reopens."""
    path = tmp_path / "library.db"
    yield path
    db.close(path)


def test_absent_library_reads_as_empty(store):
    assert library.load(store) == {}
    assert library.get("nothing", store)["title"] == ""


def test_a_damaged_field_costs_its_own_value_and_no_more(store):
    """A damaged byte should not stop the viewer from starting.

    A document could be corrupt as a whole; a row can only be corrupt in a
    column, so the blast radius is one field of one record rather than every
    annotation anyone ever made.
    """
    library.update("k1", store, title="Kept", starred_steps=["7"])
    db.connect(store).execute(
        "UPDATE marks SET starred_steps = '{ not json' WHERE content_hash = 'k1'"
    )

    record = library.get("k1", store)
    assert record["starred_steps"] == []
    assert record["title"] == "Kept"


def test_update_then_load_round_trips(store):
    library.update("k1", store, title="SOC2 web app")
    record = library.get("k1", store)
    assert record["title"] == "SOC2 web app"
    assert record["tags"] == []


def test_update_merges_rather_than_replaces(store):
    library.update("k1", store, title="First", tags=["security"])
    record = library.get("k1", store)
    assert record["title"] == "First"  # untouched by the second write
    assert record["tags"] == ["security"]


def test_unknown_fields_are_ignored(store):
    library.update("k1", store, title="Keep", nonsense="drop me")
    assert "nonsense" not in library.get("k1", store)


def test_tags_are_normalised_and_deduped(store):
    library.update("k1", store, tags=["Security", " security ", "", "Needs-Review", 7])
    assert library.get("k1", store)["tags"] == ["security", "needs-review"]


def test_clearing_every_field_forgets_the_entry(store):
    library.update("k1", store, title="Temporary")
    library.update("k1", store, title="")
    assert library.load(store) == {}


def test_a_field_this_version_no_longer_knows_cannot_keep_an_entry_alive(store):
    """A retired field used to be enough to stop a record ever being forgotten:
    it was written into everyone's file, and a record with one more key than the
    defaults could never compare equal to them again. Columns cannot do that —
    what is not a column is not stored."""
    library.update("k1", store, title="Temporary", retired=True)
    library.update("k1", store, title="")

    assert library.load(store) == {}


def test_clearing_one_kind_of_annotation_leaves_the_others(store):
    """Pruning is per table now. Clearing a note should not cost you a star."""
    library.update("k1", store, note="why this matters", starred=True)
    library.update("k1", store, note="")

    record = library.get("k1", store)
    assert record["note"] == ""
    assert record["starred"] is True


def test_remove_reports_whether_there_was_anything(store):
    library.update("k1", store, title="Here")
    assert library.remove("k1", store) is True
    assert library.remove("k1", store) is False


def test_update_requires_a_key(store):
    with pytest.raises(ValueError):
        library.update("", store, title="orphan")


def test_tags_are_counted_most_used_first(store):
    library.update("k1", store, tags=["security", "shipped"])
    library.update("k2", store, tags=["security"])
    assert library.tags(store) == [("security", 2), ("shipped", 1)]


def test_a_closed_library_is_one_file(store):
    """Write-ahead logging keeps a `-wal` and a `-shm` alongside while the
    library is open; closing it folds them back in. Anything else left behind
    would be a temporary file that outlived its write."""
    library.update("k1", store, title="One")
    library.update("k2", store, title="Two")
    db.close(store)

    assert [p.name for p in store.parent.iterdir()] == [store.name]


def test_a_write_that_fails_part_way_leaves_the_record_as_it_was(store, monkeypatch):
    """One annotation is several statements — a label, its tags, its marks —
    and a failure between them must not leave half of an edit behind."""
    library.update("k1", store, title="Original", tags=["keep"])

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(library.json, "dumps", boom)
    with pytest.raises(OSError):
        library.update(
            "k1", store, title="Doomed", tags=["gone"], starred_steps=["1"]
        )

    record = library.get("k1", store)
    assert record["title"] == "Original"
    assert record["tags"] == ["keep"]


def test_decorate_folds_annotations_into_index_rows(store):
    library.update("k1", store, title="Named", tags=["security"])
    rows = [{"key": "k1", "path": "/a"}, {"key": "k2", "path": "/b"}]
    decorated = library.decorate(rows, store)
    assert decorated[0]["title"] == "Named"
    assert decorated[0]["tags"] == ["security"]
    # An unannotated row still gets defaults, so the client branches on nothing.
    assert decorated[1]["title"] == "" and decorated[1]["starred"] is False


def test_the_real_library_is_never_touched_by_a_redirected_call(tmp_path, monkeypatch):
    """A frozen default argument once let tests write to the user's own store.
    Redirecting the module path must actually redirect."""
    redirected = tmp_path / "elsewhere.db"
    monkeypatch.setattr(db, "DB_PATH", redirected)

    library.update("k1", title="Only here")

    assert redirected.is_file()
    assert library.get("k1")["title"] == "Only here"


def test_starred_steps_round_trip(store):
    library.update("k1", store, starred_steps=["3", "12", "sub-1"])
    assert library.get("k1", store)["starred_steps"] == ["3", "12", "sub-1"]


def test_starred_steps_are_deduped_and_stringified(store):
    library.update("k1", store, starred_steps=[3, "3", 12, "", "  ", 12])
    assert library.get("k1", store)["starred_steps"] == ["3", "12"]


def test_starring_only_steps_is_enough_to_keep_the_entry(store):
    """A transcript with a starred step but no name must not be forgotten."""
    library.update("k1", store, starred_steps=["7"])
    assert library.load(store)["k1"]["starred_steps"] == ["7"]


def test_unstarring_every_step_forgets_an_otherwise_empty_entry(store):
    library.update("k1", store, starred_steps=["7"])
    library.update("k1", store, starred_steps=[])
    assert library.load(store) == {}
