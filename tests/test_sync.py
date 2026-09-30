"""Keeping a place in step, as a command rather than a thread in the server.

A schedule can run it, a person can run it, and a fetch that wedges takes
nothing down with it. What these check is the part a schedule depends on: that
a second run of the same source is cheap, and that trouble with one source does
not stop the next.
"""

from __future__ import annotations

import pytest

from transcript_viewer import cli, deploy, fetch


@pytest.fixture(autouse=True)
def somewhere(tmp_path, monkeypatch):
    from transcript_viewer import corpus, db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "library.db")
    monkeypatch.setattr(corpus, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(fetch, "destination", lambda into: tmp_path / "downloads")
    yield
    db.close(tmp_path / "library.db")


def test_a_run_with_nothing_to_follow_says_where_to_name_one(capsys):
    try:
        deploy.use(deploy.Deployment())
        assert cli.main(["sync"]) == 2
    finally:
        deploy.use(None)

    assert "nothing to sync" in capsys.readouterr().err


def test_the_sources_a_deployment_names_are_what_gets_followed(monkeypatch, capsys):
    """A deployment fed from a bucket names it once, so whatever runs the
    schedule cannot disagree with the viewer about what this holds."""
    asked = []
    monkeypatch.setattr(
        fetch, "plan", lambda url, tokens: asked.append(url) or _plan(url)
    )
    monkeypatch.setattr(cli.ingest, "bring", lambda *a, **k: iter(
        [{"t": "added", "added": 2, "seen": 5, "into": "/x"}]
    ))

    try:
        deploy.use(deploy.Deployment(sources=("s3://one/x", "s3://two/y")))
        assert cli.main(["sync"]) == 0
    finally:
        deploy.use(None)

    assert asked == ["s3://one/x", "s3://two/y"]
    assert "2 new of 5" in capsys.readouterr().err


def test_one_source_failing_does_not_stop_the_next(monkeypatch, capsys):
    """A schedule that gives up on the first unreachable bucket stops keeping
    the others in step, and says nothing about it until someone looks."""
    def plan(url, tokens):
        if "broken" in url:
            raise fetch.FetchError("no such bucket")
        return _plan(url)

    monkeypatch.setattr(fetch, "plan", plan)
    monkeypatch.setattr(cli.ingest, "bring", lambda *a, **k: iter(
        [{"t": "added", "added": 1, "seen": 1, "into": "/x"}]
    ))

    assert cli.main(["sync", "s3://broken/x", "s3://fine/y"]) == 1

    said = capsys.readouterr().err
    assert "no such bucket" in said
    assert "1 new of 1" in said, "the second source was still followed"


def test_a_named_url_beats_the_configured_ones(monkeypatch):
    asked = []
    monkeypatch.setattr(
        fetch, "plan", lambda url, tokens: asked.append(url) or _plan(url)
    )
    monkeypatch.setattr(cli.ingest, "bring", lambda *a, **k: iter([]))

    try:
        deploy.use(deploy.Deployment(sources=("s3://configured/x",)))
        cli.main(["sync", "s3://asked-for/y"])
    finally:
        deploy.use(None)

    assert asked == ["s3://asked-for/y"]


def _plan(url):
    return fetch.Plan(service="s3", label="x", files=[], web=url)
