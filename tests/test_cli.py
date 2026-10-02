"""The command line is the entry point, so its argument handling is worth
testing directly rather than only through the server."""

import pytest
from pathlib import Path
from subprocess import CompletedProcess
from transcript_viewer import corpus

from transcript_viewer import cli

LOG = """{"timestamp": "2026-05-01T12:00:00Z", "type": "session_meta", "payload": {"session_id": "c", "cli_version": "1", "cwd": "/w"}}
{"timestamp": "2026-05-01T12:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}}
"""


@pytest.fixture
def served(monkeypatch):
    """Capture what would be served instead of starting a server."""
    calls = {}

    def fake_serve(entries, port=7433, open_browser=True, explicit_port=False):
        calls.update(
            entries=entries,
            port=port,
            open_browser=open_browser,
            explicit_port=explicit_port,
        )

    monkeypatch.setattr(cli, "serve", fake_serve)
    return calls


def test_a_missing_path_is_reported_not_traced(served, capsys, tmp_path):
    assert cli.main([str(tmp_path / "nope.jsonl")]) == 2
    assert "no such path" in capsys.readouterr().err
    assert not served


def test_a_single_file_is_served_without_indexing(served, tmp_path):
    log = tmp_path / "one.jsonl"
    log.write_text(LOG)
    assert cli.main([str(log)]) == 0
    assert [e.path for e in served["entries"]] == [str(log)]


def test_a_directory_is_scanned(served, tmp_path):
    (tmp_path / "a.jsonl").write_text(LOG)
    (tmp_path / "b.jsonl").write_text(
        LOG.replace('"session_id": "c"', '"session_id": "d"')
    )
    assert cli.main([str(tmp_path)]) == 0
    assert len(served["entries"]) == 2


def test_an_archive_is_looked_inside(served, tmp_path):
    import zipfile

    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("inner.jsonl", LOG)
    assert cli.main([str(bundle)]) == 0
    assert len(served["entries"]) == 1


def test_a_path_with_nothing_convertible_reports_rather_than_serving(
    served, capsys, tmp_path
):
    (tmp_path / "notes.txt").write_text("nothing here")
    assert cli.main([str(tmp_path)]) == 1
    said = capsys.readouterr().err
    assert "no transcript" in said
    # It must also say what it looked at: a bare refusal reads as the tool
    # failing rather than the path not holding what was hoped for.
    assert "1 file" in said and ".txt" in said
    assert not served


def test_an_empty_library_opens_empty_rather_than_scanning(served, tmp_path, monkeypatch):
    """Scanning because the library is empty indexes a whole machine unasked."""
    from transcript_viewer import corpus

    monkeypatch.setattr(corpus, "INDEX_PATH", tmp_path / "absent.json")
    def refuse(*a, **k):
        raise AssertionError("a first run scanned the machine")
    monkeypatch.setattr(corpus, "scan", refuse)

    assert cli.main([]) == 0
    assert served["entries"] == []


def test_no_argument_falls_back_to_the_index(served, tmp_path, monkeypatch):
    log = tmp_path / "indexed.jsonl"
    log.write_text(LOG)
    index = tmp_path / "index.json"
    monkeypatch.setattr(corpus, "INDEX_PATH", index)
    corpus.save(corpus.scan([log]), index)

    assert cli.main([]) == 0
    assert [e.path for e in served["entries"]] == [str(log)]


def test_port_is_only_explicit_when_asked_for(served, tmp_path):
    """An unasked-for port may move when busy; a chosen one may not."""
    log = tmp_path / "a.jsonl"
    log.write_text(LOG)

    cli.main([str(log)])
    assert served["explicit_port"] is False and served["port"] == 7433

    cli.main([str(log), "--port", "8080"])
    assert served["explicit_port"] is True and served["port"] == 8080


def test_no_open_is_passed_through(served, tmp_path):
    log = tmp_path / "a.jsonl"
    log.write_text(LOG)
    cli.main([str(log), "--no-open"])
    assert served["open_browser"] is False


@pytest.mark.parametrize("profile", ["dashboard-production", None])
def test_s3_object_is_downloaded_and_served_until_shutdown(monkeypatch, profile):
    from transcript_viewer import config, fetch

    url = "s3://phagos-production-scientific-ai/relay/trajectory-example.trajectory.json"
    commands = []
    paths = []
    monkeypatch.setattr(config, "tokens", lambda: {"aws": "saved-profile"})
    monkeypatch.setattr(fetch.shutil, "which", lambda name: "/usr/local/bin/aws")

    def run(command, **kwargs):
        commands.append(command)
        Path(command[4]).write_text(LOG)
        return CompletedProcess(command, 0, "", "")

    def serve(entries, **kwargs):
        paths.extend(Path(entry.path) for entry in entries)
        assert len(paths) == 1 and paths[0].read_text() == LOG
        assert kwargs == {"port": 8080, "open_browser": False, "explicit_port": True}

    monkeypatch.setattr(fetch.subprocess, "run", run)
    monkeypatch.setattr(cli, "serve", serve)
    argv = [url, "--port", "8080", "--no-open"]
    if profile is not None:
        argv += ["--profile", profile]
    assert cli.main(argv) == 0
    assert commands == [[
        "aws", "s3", "cp", url, str(paths[0]), "--only-show-errors",
        "--profile", profile or "saved-profile",
    ]]
    assert not paths[0].exists()
    assert config.tokens()["aws"] == "saved-profile"


def test_s3_download_failure_is_reported(served, monkeypatch, capsys):
    from transcript_viewer import fetch

    def fail(*args, **kwargs):
        raise fetch.FetchError("No usable AWS session")

    monkeypatch.setattr(fetch, "_s3_download", fail)
    assert cli.main(["s3://bucket/log.json", "--profile", "dashboard-production"]) == 1
    assert "No usable AWS session" in capsys.readouterr().err
    assert not served


@pytest.mark.parametrize("url", ["s3://bucket", "s3://bucket/folder/", "s3://bad_bucket/a.json", "s3://bucket/.."])
def test_s3_requires_an_object_path(served, capsys, url):
    assert cli.main([url]) == 2
    assert "expected s3://bucket/path/to/file" in capsys.readouterr().err
    assert not served
