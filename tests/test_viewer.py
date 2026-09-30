"""The viewer is a local HTTP server over trajectories atif-make produces."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from transcript_viewer import corpus

from transcript_viewer.viewer import serve

LOG = """{"timestamp": "2026-05-01T12:00:00Z", "type": "session_meta", "payload": {"session_id": "c", "cli_version": "1", "cwd": "/w"}}
{"timestamp": "2026-05-01T12:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}}
{"timestamp": "2026-05-01T12:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi back"}]}}
"""

# Built with json.dumps rather than written out: a hand-escaped argument string
# inside a triple-quoted literal loses a level of escaping and stops being JSON.
LOG += '{"timestamp": "2026-05-01T12:00:03Z", "type": "response_item", "payload": {"type": "function_call", "call_id": "call_a", "name": "shell", "arguments": "{\\"command\\": \\"ls\\"}"}}' + "\n" + '{"timestamp": "2026-05-01T12:00:04Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_a", "output": "README.md"}}' + "\n"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Never let a test touch the real library, index, settings or file store."""
    from transcript_viewer import corpus
    from transcript_viewer import config, db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "library.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(corpus, "OPENED_ROOT", tmp_path / "opened")
    monkeypatch.setattr(corpus, "INDEX_PATH", tmp_path / "index.json")
    # Connections are cached per path and per thread; a test that leaves one
    # open holds a handle on a directory pytest is about to delete.
    yield
    db.close(tmp_path / "library.db")


@pytest.fixture
def server(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text(LOG)
    entries = corpus.scan([log])
    assert entries, "fixture log was not recognised"

    port = _free_port()
    thread = threading.Thread(
        target=serve, kwargs={"entries": entries, "port": port, "open_browser": False},
        daemon=True,
    )
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):  # wait for the socket to accept
        try:
            _get(base + "/api/index")
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.05)
    else:
        pytest.fail("viewer did not start")
    return base


def test_serves_the_page(server):
    status, body = _get(server + "/")
    assert status == 200
    assert b"<title>Transcript Viewer</title>" in body


def test_index_lists_the_session(server):
    rows = _sessions(server)
    assert len(rows) == 1
    assert rows[0]["agent"] == "codex"
    # Annotations arrive folded in, so the client never has to join.
    assert rows[0]["title"] == "" and rows[0]["tags"] == []


def _index(server: str) -> dict:
    return json.loads(_get(server + "/api/index")[1])


def _sessions(server: str) -> list:
    return _index(server)["sessions"]


def _first_key(server: str) -> str:
    return _sessions(server)[0]["key"]


def test_converts_on_demand(server):
    _, body = _get(server + f"/api/trajectory?id={_first_key(server)}")
    trajectory = json.loads(body)
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert [s["source"] for s in trajectory["steps"]] == ["user", "agent", "agent"]


def test_unknown_session_is_reported_not_crashed(server):
    _, body = _get(server + "/api/trajectory?id=nosuchkey")
    assert json.loads(body)["error"]


def test_reveal_rejects_a_missing_path(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(server + "/api/reveal?path=%2Fnope%2Fnot%2Fhere")
    assert caught.value.code == 404


def test_reveal_rejects_an_empty_path(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(server + "/api/reveal?path=")
    assert caught.value.code == 400


def _lan_address() -> str | None:
    """This host's outbound interface address, or None if there is no network.

    Connecting a UDP socket sends nothing; it just selects a route so the local
    address can be read back.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 80))  # TEST-NET-1, never routed
            address = s.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def test_binds_loopback_only(server):
    """Session logs carry source code and tool output; never expose them."""
    address = _lan_address()
    if address is None:
        pytest.skip("no non-loopback interface to test against")
    port = int(server.rsplit(":", 1)[1])
    with socket.socket() as s:
        s.settimeout(2)
        # Reaching the server over a routable address would mean it is exposed.
        assert s.connect_ex((address, port)) != 0


def test_falls_back_when_the_default_port_is_busy(tmp_path):
    """Viewing a second session while a first is open is normal, not an error."""
    log = tmp_path / "s.jsonl"
    log.write_text(LOG)
    entries = corpus.scan([log])

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    busy = taken.getsockname()[1]
    try:
        thread = threading.Thread(
            target=serve,
            kwargs={"entries": entries, "port": busy, "open_browser": False,
                    "explicit_port": False},
            daemon=True,
        )
        thread.start()
        # It must come up on some nearby port rather than crashing. Poll the
        # whole range until a deadline: one attempt per port races a slow start.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for candidate in range(busy + 1, busy + 20):
                try:
                    status, _ = _get(f"http://127.0.0.1:{candidate}/api/index")
                except (urllib.error.URLError, ConnectionError, OSError):
                    continue
                assert status == 200
                return
            time.sleep(0.1)
        pytest.fail("viewer did not fall back to a free port")
    finally:
        taken.close()


def test_explicit_port_in_use_is_reported_not_traced(tmp_path):
    """An explicitly requested port is honoured or reported, never moved."""
    log = tmp_path / "s.jsonl"
    log.write_text(LOG)
    entries = corpus.scan([log])

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    busy = taken.getsockname()[1]
    try:
        with pytest.raises(SystemExit) as caught:
            serve(entries, port=busy, open_browser=False, explicit_port=True)
        assert "already in use" in str(caught.value)
        assert "--port" in str(caught.value)
    finally:
        taken.close()


def test_raw_endpoint_returns_the_source(server):
    _, body = _get(server + f"/api/raw?id={_first_key(server)}")
    raw = json.loads(body)
    assert raw["path"].endswith("session.jsonl")
    assert not raw["truncated"]
    # The head of the actual log, not the converted trajectory.
    assert '"session_meta"' in raw["text"]


def test_raw_endpoint_truncates_a_large_file(tmp_path):
    """A rollout can be hundreds of MB; the panel must not ship the lot."""
    from transcript_viewer.viewer import RAW_LIMIT, _Handler

    big = tmp_path / "big.jsonl"
    big.write_text(LOG + ("x" * (RAW_LIMIT + 1000)))
    assert big.stat().st_size > RAW_LIMIT


def test_files_endpoint_lists_subagents(tmp_path, server):
    _, body = _get(server + f"/api/files?id={_first_key(server)}")
    files = json.loads(body)
    roles = {f["role"] for f in files}
    assert "source" in roles
    assert all(f["size"] >= 0 for f in files)


def test_files_endpoint_finds_sibling_subagents(tmp_path):
    from transcript_viewer.viewer import _associated_files

    source = tmp_path / "sess.jsonl"
    source.write_text(LOG)
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-abc.jsonl").write_text(LOG)
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.png").write_bytes(b"\x89PNG")

    found = _associated_files(source)
    roles = {f["name"]: f["role"] for f in found}
    assert roles["sess.jsonl"] == "source"
    assert roles["agent-abc.jsonl"] == "subagent"
    assert roles["a.png"] == "image"


def test_bad_index_on_new_endpoints(server):
    for path in ("/api/raw?id=nope", "/api/files?id=nope"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(server + path)
        assert caught.value.code == 404


def _post_json(url: str, body: dict):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def _post(url: str, data: bytes, filename: str):
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"X-Filename": filename}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def test_open_adds_an_uploaded_log_to_the_index(server):
    """The Open button must add exactly what the CLI would."""
    before = len(_sessions(server))
    other = LOG.replace('"session_id": "c"', '"session_id": "dropped"')
    status, result = _post(server + "/api/open", other.encode(), "dropped.jsonl")
    assert status == 200
    assert result["added"] == 1
    after = _sessions(server)
    assert len(after) == before + 1
    # And it is immediately viewable, not merely listed.
    _, body = _get(server + f"/api/trajectory?id={result['keys'][0]}")
    assert json.loads(body)["schema_version"] == "ATIF-v1.7"


def test_open_rejects_a_file_that_is_not_a_log(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(server + "/api/open", b'{"grade":1}\n', "grades.jsonl")
    assert caught.value.code == 415


def test_open_rejects_an_empty_upload(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(server + "/api/open", b"", "nothing.jsonl")
    assert caught.value.code == 400


def test_open_neutralises_a_traversing_filename(server, tmp_path):
    """A client-supplied name must not decide where bytes land."""
    status, result = _post(
        server + "/api/open", LOG.encode(), "../../../../tmp/escaped.jsonl"
    )
    assert status == 200
    rows = {r["key"]: r for r in _sessions(server)}
    landed = Path(rows[result["keys"][0]]["path"])
    assert landed.name == "escaped.jsonl"
    assert "/tmp/escaped.jsonl" != str(landed)


def test_safe_name_reduces_to_a_leaf():
    from transcript_viewer.viewer import _safe_name

    assert _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("/absolute/path.jsonl") == "path.jsonl"
    assert _safe_name("") == "upload"
    assert _safe_name("%2e%2e%2fetc%2fshadow") == "shadow"


def test_open_is_the_only_post_route(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(server + "/api/anything", b"x", "x.jsonl")
    assert caught.value.code == 404


def test_reordering_the_index_does_not_change_what_a_link_opens(server):
    """The whole point of content keys: a link survives the list moving."""
    rows = _sessions(server)
    key = rows[0]["key"]
    _, before = _get(server + f"/api/trajectory?id={key}")

    from transcript_viewer.viewer import _Handler

    _Handler.entries = list(reversed(_Handler.entries)) + list(_Handler.entries)
    _, after = _get(server + f"/api/trajectory?id={key}")
    assert json.loads(before)["session_id"] == json.loads(after)["session_id"]


def test_opening_the_same_file_twice_updates_rather_than_duplicates(server):
    """Content keys mean a file re-opened from a new place is still one entry."""
    payload = LOG.replace('"session_id": "c"', '"session_id": "twice"').encode()
    _post(server + "/api/open", payload, "first-name.jsonl")
    before = len(_sessions(server))
    _post(server + "/api/open", payload, "renamed.jsonl")
    assert len(_sessions(server)) == before


def test_opened_files_are_marked_and_stored_outside_temp(server):
    payload = LOG.replace('"session_id": "c"', '"session_id": "kept"').encode()
    _, result = _post(server + "/api/open", payload, "kept.jsonl")
    row = {r["key"]: r for r in _sessions(server)}[result["keys"][0]]
    assert row["origin"] == "opened"
    assert Path(row["path"]).is_file()
    # Under the opened store, keyed by content — not a temp directory.
    assert result["keys"][0] in row["path"]


def test_annotating_a_session_persists_and_comes_back_in_the_index(server):
    key = _first_key(server)
    status, record = _post_json(server + "/api/library", {
        "key": key, "title": "SOC2 web app",
        "tags": ["security", "needs-review"], "starred": True,
    })
    assert status == 200
    assert record["title"] == "SOC2 web app"

    index = _index(server)
    row = {r["key"]: r for r in index["sessions"]}[key]
    assert row["title"] == "SOC2 web app"
    assert row["tags"] == ["security", "needs-review"]
    assert {t["name"]: t["count"] for t in index["tags"]} == {
        "security": 1, "needs-review": 1,
    }


def test_annotating_without_a_key_is_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post_json(server + "/api/library", {"title": "orphan"})
    assert caught.value.code == 400


def test_deleting_an_opened_session_removes_its_copy(server):
    payload = LOG.replace('"session_id": "c"', '"session_id": "removable"').encode()
    _, result = _post(server + "/api/open", payload, "removable.jsonl")
    key = result["keys"][0]
    stored = Path({r["key"]: r for r in _sessions(server)}[key]["path"])
    assert stored.is_file()

    request = urllib.request.Request(
        server + f"/api/library?id={key}", method="DELETE"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["removed_copy"] is True

    assert not stored.exists()
    assert key not in {r["key"] for r in _sessions(server)}


def test_an_opened_archive_is_unpacked_rather_than_stored_whole(server, tmp_path):
    """Storing the container would mean re-extracting to a temp directory on
    every start, and a directory scan does not look inside archives."""
    import zipfile

    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(
            "inner.jsonl", LOG.replace('"session_id": "c"', '"session_id": "inner"')
        )
        archive.writestr("manifest.json", '{"note": "travels with it"}')

    _, result = _post(server + "/api/open", bundle.read_bytes(), "bundle.zip")
    assert result["added"] == 1

    from transcript_viewer import corpus

    stored = sorted(p.name for p in corpus.OPENED_ROOT.rglob("*") if p.is_file())
    assert "inner.jsonl" in stored
    assert "manifest.json" in stored          # siblings travel with it
    assert not any(n.endswith(".zip") for n in stored)


def test_an_opened_file_is_classified_from_its_location(server, tmp_path):
    from transcript_viewer import corpus

    payload = LOG.replace('"session_id": "c"', '"session_id": "persist"').encode()
    _post(server + "/api/open", payload, "persist.jsonl")

    kept = [e for e in corpus.load() if e.origin == "opened"]
    assert kept, "the opened file was not written to the index"
    # Its location alone is enough to classify it on a fresh look.
    assert corpus.describe(Path(kept[0].path)).origin == "opened"


def test_starring_steps_persists_on_the_session(server):
    key = _first_key(server)
    _post_json(server + "/api/library", {"key": key, "starred_steps": ["2", "5"]})
    row = {r["key"]: r for r in _sessions(server)}[key]
    assert row["starred_steps"] == ["2", "5"]


def test_a_starred_step_alone_is_worth_keeping(server):
    """A transcript with a starred step but no name must not be forgotten."""
    key = _first_key(server)
    _post_json(server + "/api/library", {"key": key, "starred_steps": ["7"]})
    row = {r["key"]: r for r in _sessions(server)}[key]
    assert row["title"] == "" and row["starred_steps"] == ["7"]


def test_renaming_from_the_transcript_uses_the_same_endpoint(server):
    """The heading and the table row must not disagree about a title."""
    key = _first_key(server)
    _post_json(server + "/api/library", {"key": key, "title": "Renamed here"})
    assert {r["key"]: r for r in _sessions(server)}[key]["title"] == "Renamed here"


def test_deleting_one_file_from_an_archive_spares_its_siblings(server, tmp_path):
    """An unpacked archive shares a directory; removing one entry must not
    delete the other's file and leave its row pointing at nothing."""
    import zipfile

    bundle = tmp_path / "pair.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("one.jsonl", LOG.replace('"session_id": "c"', '"session_id": "one"'))
        archive.writestr("two.jsonl", LOG.replace('"session_id": "c"', '"session_id": "two"'))

    _, result = _post(server + "/api/open", bundle.read_bytes(), "pair.zip")
    assert result["added"] == 2
    first, second = result["keys"]

    rows = {r["key"]: Path(r["path"]) for r in _sessions(server)}
    request = urllib.request.Request(server + f"/api/library?id={first}", method="DELETE")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["removed_copy"] is True

    assert not rows[first].exists()
    assert rows[second].exists(), "the sibling's file was deleted with it"
    # And it is still openable, not an orphaned row.
    assert _get(server + f"/api/raw?id={second}")[0] == 200


def test_deleting_the_last_file_clears_the_whole_store_directory(server, tmp_path):
    payload = LOG.replace('"session_id": "c"', '"session_id": "solo"').encode()
    _, result = _post(server + "/api/open", payload, "solo.jsonl")
    key = result["keys"][0]
    stored = {r["key"]: Path(r["path"]) for r in _sessions(server)}[key]

    request = urllib.request.Request(server + f"/api/library?id={key}", method="DELETE")
    urllib.request.urlopen(request, timeout=5).read()
    assert not stored.exists()
    assert not stored.parent.exists(), "the empty store directory was left behind"


# ---- settings -----------------------------------------------------------------


def _try_post(url: str, body: dict) -> tuple[int, dict]:
    """POST that reports a refusal instead of raising, so it can be asserted on."""
    try:
        return _post_json(url, body)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_a_saved_key_is_never_returned_to_the_page(server):
    secret = "g" * 40
    status, payload = _try_post(
        server + "/api/settings", {"name": "github", "token": secret}
    )
    assert status == 200
    assert secret not in json.dumps(payload)
    assert payload["secrets"]["github"]["source"] == "settings"
    assert payload["secrets"]["github"]["hint"] == "\u2026gggg"
    assert secret not in json.dumps(_index(server))


def test_a_key_that_is_not_one_is_refused(server):
    status, payload = _try_post(
        server + "/api/settings", {"name": "github", "token": "nope"}
    )
    assert status == 400
    assert "does not look like" in payload["error"]


def test_a_saved_key_can_be_removed(server):
    _try_post(
        server + "/api/settings", {"name": "github", "token": "g" * 40}
    )
    status, payload = _try_post(
        server + "/api/settings", {"name": "github", "clear": True}
    )
    assert status == 200
    assert payload["secrets"]["github"]["hint"] == ""


def test_a_session_whose_file_is_gone_is_not_listed(server, tmp_path):
    """A downloaded folder the reader deleted should not leave a dead row."""
    before = _sessions(server)
    assert before, "fixture session missing"

    gone = tmp_path / "vanished.jsonl"
    gone.write_text(LOG)
    status, _ = _post(server + "/api/open", LOG.encode(), "vanished.jsonl")
    assert status == 200
    added = [s for s in _sessions(server) if "vanished" in s["path"]]
    assert added, "the upload was not listed"

    Path(added[0]["path"]).unlink()
    assert not [s for s in _sessions(server) if "vanished" in s["path"]]


def test_a_missing_file_is_hidden_rather_than_forgotten(server, tmp_path, monkeypatch):
    """Purging on sight would delete entries whenever a drive is unmounted."""
    from transcript_viewer import corpus

    status, _ = _post(server + "/api/open", LOG.encode(), "temporary.jsonl")
    assert status == 200
    stored = [e for e in corpus.load() if "temporary" in e.path]
    assert stored, "the upload was not persisted"

    Path(stored[0].path).unlink()
    assert not [s for s in _sessions(server) if "temporary" in s["path"]]
    assert [e for e in corpus.load() if "temporary" in e.path], "the entry was purged"


# ---- deleting ----------------------------------------------------------------------


def _delete(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_deleting_a_session_takes_its_annotations_with_it(server):
    """Everything kept about a transcript lives in one record, and goes at once."""
    from transcript_viewer import library

    key = _first_key(server)
    _try_post(server + "/api/library", {
        "key": key, "title": "kept things", "tags": ["one"],
        "starred": True, "starred_steps": ["1", "2"],
    })
    stored = library.get(key)
    assert stored["starred_steps"] == ["1", "2"]

    status, payload = _delete(server + f"/api/library?id={key}")
    assert status == 200 and payload["removed"]

    assert key not in library.load(), "the record survived the session"
    fresh = library.get(key)
    assert fresh["starred_steps"] == []
    assert fresh["title"] == "" and fresh["tags"] == []


def test_a_scanned_session_is_forgotten_but_its_file_is_left_alone(server):
    """Found on the machine means it is not ours to delete."""
    key = _first_key(server)
    path = Path(_sessions(server)[0]["path"])
    status, payload = _delete(server + f"/api/library?id={key}")
    assert status == 200
    assert payload["removed_copy"] is False
    assert path.exists(), "a file we merely found was deleted"
    assert not _sessions(server)


def test_an_opened_copy_is_deleted_because_the_viewer_made_it(server):
    status, _ = _post(server + "/api/open", LOG.encode(), "brought-in.jsonl")
    assert status == 200
    row = next(s for s in _sessions(server) if "brought-in" in s["path"])
    assert row["origin"] == "opened"

    path = Path(row["path"])
    status, payload = _delete(server + f"/api/library?id={row['key']}")
    assert status == 200 and payload["removed_copy"] is True
    assert not path.exists(), "the viewer's own copy was left behind"




def test_the_tree_is_derived_from_where_a_session_came_from(server):
    """Local mirrors Remote: what produced it, then the unit of work."""
    rows = _sessions(server)
    assert rows[0]["group"].startswith("Local/"), rows[0]["group"]
    assert rows[0]["group"] in _index(server)["groups"]


def test_every_node_of_the_tree_has_its_parents(server):
    groups = _index(server)["groups"]
    for node in groups:
        parts = node.split("/")
        for depth in range(1, len(parts)):
            assert "/".join(parts[:depth]) in groups, f"{node} has no parent"


def test_local_comes_before_remote(server):
    groups = _index(server)["groups"]
    locals_ = [i for i, g in enumerate(groups) if g.startswith("Local")]
    remotes = [i for i, g in enumerate(groups) if g.startswith("Remote")]
    if locals_ and remotes:
        assert max(locals_) < min(remotes)


def test_removing_a_node_removes_what_sits_under_it(server):
    key = _first_key(server)
    node = next(s["group"] for s in _sessions(server) if s["key"] == key)
    top = node.split("/")[0]
    status, payload = _delete(server + f"/api/group?name={top}")
    assert status == 200 and payload["removed"] >= 1
    assert not [s for s in _sessions(server) if s["key"] == key]


def test_a_node_name_is_required(server):
    status, payload = _delete(server + "/api/group?name=")
    assert status == 400 and "folder is required" in payload["error"]


def test_clearing_the_library_forgets_everything(server):
    from transcript_viewer import library

    key = _first_key(server)
    _try_post(server + "/api/library", {"key": key, "title": "kept", "tags": ["t"]})
    assert _sessions(server)

    status, payload = _delete(server + "/api/library?all=1")
    assert status == 200 and payload["removed"] >= 1
    assert _sessions(server) == []
    assert library.load() == {}, "annotations outlived their sessions"


def test_clearing_leaves_a_scanned_file_on_disk(server):
    """Forgetting is not deleting; only the viewer's own copies go."""
    path = Path(_sessions(server)[0]["path"])
    _delete(server + "/api/library?all=1")
    assert path.exists()


def test_clearing_deletes_the_copies_the_viewer_made(server):
    status, _ = _post(server + "/api/open", LOG.encode(), "brought-in.jsonl")
    assert status == 200
    copy = Path(next(s["path"] for s in _sessions(server) if "brought-in" in s["path"]))
    assert copy.exists()

    _delete(server + "/api/library?all=1")
    assert not copy.exists(), "the viewer's own copy was left behind"


def test_clearing_an_empty_library_is_harmless(server):
    _delete(server + "/api/library?all=1")
    status, payload = _delete(server + "/api/library?all=1")
    assert status == 200 and payload["removed"] == 0


def test_the_page_is_re_read_when_it_changes(tmp_path, monkeypatch):
    """Editing the interface and seeing nothing change reads as a broken edit."""
    from transcript_viewer import viewer

    page_file = tmp_path / "page.html"
    page_file.write_text("<p>first</p>")
    monkeypatch.setattr(viewer, "PAGE_PATH", page_file)
    monkeypatch.setattr(viewer, "_page", None)

    assert viewer.page() == "<p>first</p>"

    # A same-size rewrite is the hard case: only the timestamp separates them.
    page_file.write_text("<p>secnd</p>")
    import os
    stamp = page_file.stat().st_mtime + 10
    os.utime(page_file, (stamp, stamp))

    assert viewer.page() == "<p>secnd</p>"


def test_an_unreadable_page_serves_what_was_already_read(tmp_path, monkeypatch):
    """A file being written to should not take the interface down mid-save."""
    from transcript_viewer import viewer

    page_file = tmp_path / "page.html"
    page_file.write_text("<p>kept</p>")
    monkeypatch.setattr(viewer, "PAGE_PATH", page_file)
    monkeypatch.setattr(viewer, "_page", None)
    assert viewer.page() == "<p>kept</p>"

    page_file.unlink()
    assert viewer.page() == "<p>kept</p>"


def test_nothing_the_server_sends_is_cached(server):
    """The page is re-read whenever it changes, which a cached copy undoes.

    Everything here is live local data: a session on disk grows while it is
    being read, and a trajectory is reconverted whenever its parser changes.
    A browser holding an earlier copy makes both look like they did not happen.
    """
    import urllib.request

    for path in ("/", "/api/index"):
        with urllib.request.urlopen(f"{server}{path}") as response:
            assert response.headers["Cache-Control"] == "no-store", path


def test_a_server_holding_its_own_keys_refuses_to_take_one(server):
    """`config.secret` stops reading the file in this mode, so a key accepted
    here would look saved and change nothing — a worse answer than a refusal."""
    import urllib.error
    import urllib.request

    from transcript_viewer import deploy

    body = json.dumps({"name": "github", "token": "g" * 40}).encode()
    request = urllib.request.Request(
        f"{server}/api/settings", data=body, method="POST"
    )
    try:
        deploy.use(deploy.Deployment(secrets="server"))
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
    finally:
        deploy.use(None)

    assert refused.value.code == 403
    assert b"holds its own credentials" in refused.value.read()
