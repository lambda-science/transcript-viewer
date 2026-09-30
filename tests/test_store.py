"""The small JSON files the viewer keeps for itself."""

import json
from pathlib import Path

from transcript_viewer import store



def test_adopts_a_library_left_under_the_old_name(tmp_path, monkeypatch):
    """A rename must not read as data loss: the old directory is taken over."""
    old, new = tmp_path / ".atif", tmp_path / ".transcript-viewer"
    (old / "opened" / "abc").mkdir(parents=True)
    (old / "opened" / "abc" / "s.jsonl").write_text("{}\n")
    (old / "index.json").write_text(json.dumps([{"path": str(old / "opened/abc/s.jsonl")}]))
    (old / "config.json").write_text('{"api_key": "k"}')

    monkeypatch.setattr(store, "LEGACY_ROOT", old)
    monkeypatch.setattr(store, "ROOT", new)

    assert store.adopt_legacy() is True
    assert not old.exists()
    assert json.loads((new / "config.json").read_text()) == {"api_key": "k"}

    # Paths recorded inside the index pointed at the old directory.
    moved = json.loads((new / "index.json").read_text())[0]["path"]
    assert moved == str(new / "opened/abc/s.jsonl")
    assert Path(moved).exists()


def test_adopting_is_a_one_time_move(tmp_path, monkeypatch):
    """An existing library is never overwritten by a stale one beside it."""
    old, new = tmp_path / ".atif", tmp_path / ".transcript-viewer"
    old.mkdir()
    (old / "config.json").write_text('{"api_key": "stale"}')
    new.mkdir()
    (new / "config.json").write_text('{"api_key": "current"}')

    monkeypatch.setattr(store, "LEGACY_ROOT", old)
    monkeypatch.setattr(store, "ROOT", new)

    assert store.adopt_legacy() is False
    assert json.loads((new / "config.json").read_text()) == {"api_key": "current"}


def test_adopting_nothing_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LEGACY_ROOT", tmp_path / "absent")
    monkeypatch.setattr(store, "ROOT", tmp_path / "also-absent")
    assert store.adopt_legacy() is False
