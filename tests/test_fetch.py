"""Fetching trajectories by URL.

Nothing here reaches the network: `_open` is stubbed. What is tested is the part
that has to be right whether or not a host is reachable — that a URL the page
supplies cannot make the server talk to anything but the four allowed hosts, and
that a remote listing cannot write outside the directory it was given.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from transcript_viewer import fetch


class _Response(io.BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _stub(monkeypatch, routes: dict, landed: str | None = None):
    """Answer known URLs from a dict; record the auth header each call carried."""
    seen: dict = {"auth": [], "urls": []}

    def fake_open(url, token, service):
        fetch._check_host(url)
        seen["urls"].append(url)
        seen["auth"].append(token)
        for pattern, body in routes.items():
            if pattern in url:
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                return _Response(payload, landed or url)
        raise fetch.FetchError(f"no stub for {url}")

    monkeypatch.setattr(fetch, "_open", fake_open)
    return seen


# ---- what may be reached ---------------------------------------------------------


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Resolve names without touching the network.

    Anything not named here resolves to a public address, so a test that cares
    about address space says so explicitly.
    """
    table = {
        "localhost": "127.0.0.1",
        "127.0.0.1": "127.0.0.1",
        "169.254.169.254": "169.254.169.254",
        "192.168.1.1": "192.168.1.1",
        "10.0.0.5": "10.0.0.5",
        "internal.example": "172.16.4.4",
        "nowhere.example": None,
    }

    def resolve(host, port, *args, **kwargs):
        address = table.get(host, "93.184.216.34")
        if address is None:
            raise fetch.socket.gaierror("no such host")
        return [(2, 1, 6, "", (address, port or 443))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/x.jsonl",
        "https://127.0.0.1:8080/admin.jsonl",
        "https://169.254.169.254/latest/meta-data.json",
        "https://192.168.1.1/admin.jsonl",
        "https://10.0.0.5/x.jsonl",
        "https://internal.example/x.jsonl",
    ],
    ids=["localhost", "loopback", "cloud-metadata", "router", "private-10", "public-name"],
)
def test_an_address_on_this_machine_or_network_is_refused(url):
    """The page hands this to a server on your network; it must not be a proxy.

    The check is on the resolved address, not the text, so a perfectly ordinary
    hostname pointed at a private address is caught too.
    """
    with pytest.raises(fetch.FetchError, match="Only public addresses"):
        fetch.plan(url, {})


@pytest.mark.parametrize(
    "url",
    ["http://huggingface.co/datasets/a/b", "file:///etc/passwd", "ftp://example.com/x"],
    ids=["plain-http", "file", "ftp"],
)
def test_only_https_is_fetched(url):
    with pytest.raises(fetch.FetchError, match="https"):
        fetch.plan(url, {})


def test_a_host_that_does_not_resolve_is_reported():
    with pytest.raises(fetch.FetchError, match="Could not look up"):
        fetch.plan("https://nowhere.example/x.jsonl", {})


def test_an_empty_url_is_refused():
    with pytest.raises(fetch.FetchError, match="No URL"):
        fetch.plan("  ", {})


@pytest.mark.parametrize(
    ("url", "service"),
    [
        ("https://huggingface.co/datasets/a/b", "hf"),
        ("https://github.com/a/b", "github"),
        ("https://example.com/logs/a.jsonl", "direct"),
    ],
)
def test_a_public_host_is_classified_by_what_is_understood(url, service):
    assert fetch._check_host(url) == service


def test_a_bare_url_is_taken_as_one_file():
    """Only two hosts can be listed; everywhere else is a direct link."""
    _, label, files, _ = fetch.plan("https://example.com/runs/session.jsonl", {})
    assert label == "example.com"
    assert [f.name for f in files] == ["session.jsonl"]


def test_a_bare_url_to_something_unreadable_says_so():
    with pytest.raises(fetch.FetchError, match="not a kind this reads"):
        fetch.plan("https://example.com/notes.pdf", {})


def test_a_redirect_into_private_space_is_refused(monkeypatch):
    """A redirect is a second request to a second host, so it is checked again."""

    def landed_private(request, timeout=None):
        return _Response(b"{}", "https://192.168.1.1/x.jsonl")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", landed_private)
    with pytest.raises(fetch.FetchError, match="Only public addresses"):
        fetch._open("https://example.com/x.jsonl", None, "direct")


def test_a_redirect_between_public_hosts_is_fine(monkeypatch):
    def landed_public(request, timeout=None):
        return _Response(b"{}", "https://cdn.example.com/blob")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", landed_public)
    with fetch._open("https://example.com/x.jsonl", None, "direct") as response:
        assert response.read() == b"{}"


def test_a_token_travels_in_a_header_never_in_the_url(monkeypatch):
    """A URL reaches logs, history and error messages; a header does not."""
    captured = {}

    def capture(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return _Response(b"{}", request.full_url)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", capture)
    fetch._open("https://huggingface.co/api/datasets/a/b", "secret-token", "hf")
    assert captured["auth"] == "Bearer secret-token"
    assert "secret-token" not in captured["url"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [(401, "needs a token"), (403, "needs a token"), (404, "Nothing there"),
     (500, "said 500")],
)
def test_an_http_failure_says_what_to_do_about_it(monkeypatch, code, expected):
    def refuse(request, timeout=None):
        raise fetch.urllib.error.HTTPError(request.full_url, code, "no", {}, None)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", refuse)
    with pytest.raises(fetch.FetchError, match=expected):
        fetch._open("https://huggingface.co/x", None, "hf")


# ---- hugging face ----------------------------------------------------------------


def test_a_dataset_url_lists_its_convertible_files(monkeypatch):
    seen = _stub(
        monkeypatch,
        {
            "api/datasets": [
                {"type": "file", "path": "a/transcript.jsonl", "size": 10},
                {"type": "file", "path": "a/description.md", "size": 5},
                {"type": "directory", "path": "a"},
                {"type": "file", "path": "b/metadata.json", "size": 7},
            ]
        },
    )
    service, label, files, _ = fetch.plan(
        "https://huggingface.co/datasets/owner/name", {"hf": "tok"}
    )
    assert service == "hf"
    assert label == "owner--name"
    assert [f.name for f in files] == [
        "a/transcript.jsonl",
        "a/description.md",
        "b/metadata.json",
    ]
    assert all("/resolve/main/" in f.url for f in files)
    assert seen["auth"] == ["tok"], "the token was not sent"


def test_a_tree_url_with_a_revision_and_subdirectory(monkeypatch):
    seen = _stub(monkeypatch, {"api/datasets": [{"type": "file", "path": "x.jsonl"}]})
    fetch.plan(
        "https://huggingface.co/datasets/owner/name/tree/v2/attacks/one", {"hf": None}
    )
    assert "/tree/v2/attacks/one" in seen["urls"][0]


def test_a_single_file_url_needs_no_listing(monkeypatch):
    seen = _stub(monkeypatch, {})
    _, _, files, _ = fetch.plan(
        "https://huggingface.co/datasets/owner/name/blob/main/a/transcript.jsonl", {}
    )
    assert [f.name for f in files] == ["transcript.jsonl"]
    assert files[0].url.endswith("/resolve/main/a/transcript.jsonl")
    assert seen["urls"] == [], "a single file should not need the tree API"


def test_a_model_repo_works_as_well_as_a_dataset(monkeypatch):
    seen = _stub(monkeypatch, {"api/models": [{"type": "file", "path": "t.jsonl"}]})
    _, label, files, _ = fetch.plan("https://huggingface.co/owner/name", {})
    assert label == "owner--name"
    assert "api/models/owner/name" in seen["urls"][0]


def test_a_url_that_is_not_a_repo_is_refused():
    with pytest.raises(fetch.FetchError, match="does not look like"):
        fetch.plan("https://huggingface.co/", {})


# ---- github ----------------------------------------------------------------------


def test_a_repo_url_uses_the_default_branch(monkeypatch):
    seen = _stub(
        monkeypatch,
        {
            # More specific first: the repo-info URL is a prefix of the tree URL.
            "git/trees": {"tree": [{"type": "blob", "path": "logs/a.jsonl"}]},
            "api.github.com/repos/owner/name": {"default_branch": "trunk"},
        },
    )
    _, label, files, _ = fetch.plan("https://github.com/owner/name", {"github": "gh"})
    assert label == "owner--name"
    assert "/trunk/" in files[0].url
    assert seen["auth"] == ["gh", "gh"]


def test_a_subdirectory_narrows_the_listing(monkeypatch):
    _stub(
        monkeypatch,
        {
            "git/trees": {
                "tree": [
                    {"type": "blob", "path": "logs/a.jsonl"},
                    {"type": "blob", "path": "other/b.jsonl"},
                    {"type": "tree", "path": "logs"},
                ]
            }
        },
    )
    _, _, files, _ = fetch.plan("https://github.com/owner/name/tree/main/logs", {})
    assert [f.name for f in files] == ["logs/a.jsonl"]


def test_a_truncated_tree_is_reported_rather_than_silently_partial(monkeypatch):
    _stub(monkeypatch, {"git/trees": {"tree": [], "truncated": True}})
    with pytest.raises(fetch.FetchError, match="too large to list"):
        fetch.plan("https://github.com/owner/name/tree/main", {})


def test_a_raw_url_is_taken_as_one_file(monkeypatch):
    seen = _stub(monkeypatch, {})
    _, _, files, _ = fetch.plan(
        "https://raw.githubusercontent.com/owner/name/main/logs/a.jsonl", {}
    )
    assert [f.name for f in files] == ["a.jsonl"]
    assert seen["urls"] == []


def test_a_blob_url_is_rewritten_to_raw(monkeypatch):
    _stub(monkeypatch, {})
    _, _, files, _ = fetch.plan("https://github.com/owner/name/blob/main/a.jsonl", {})
    assert files[0].url.startswith("https://raw.githubusercontent.com/")


# ---- limits ----------------------------------------------------------------------


def test_nothing_convertible_is_said_plainly(monkeypatch):
    # Markdown is fetchable — some corpora publish transcripts as nothing else —
    # so the refusal is tested with something that could not be one.
    _stub(monkeypatch, {"api/datasets": [{"type": "file", "path": "diagram.png"}]})
    with pytest.raises(fetch.FetchError, match="Nothing convertible"):
        fetch.plan("https://huggingface.co/datasets/owner/name", {})


def test_a_transcript_published_as_markdown_is_worth_fetching(monkeypatch):
    """A bucket of 136 of these was unreachable while .md was filtered out."""
    _stub(
        monkeypatch,
        {"api/datasets": [{"type": "file", "path": "run/transcript_attack.md"}]},
    )
    plan = fetch.plan("https://huggingface.co/datasets/owner/name", {})
    assert [f.name for f in plan.files] == ["run/transcript_attack.md"]


def test_too_many_files_is_refused_before_downloading(monkeypatch):
    _stub(
        monkeypatch,
        {
            "api/datasets": [
                {"type": "file", "path": f"{i}.jsonl"}
                for i in range(fetch.MAX_FILES + 1)
            ]
        },
    )
    with pytest.raises(fetch.FetchError, match="more than this fetches"):
        fetch.plan("https://huggingface.co/datasets/owner/name", {})


def test_too_many_bytes_is_refused_before_downloading(monkeypatch):
    _stub(
        monkeypatch,
        {
            "api/datasets": [
                {"type": "file", "path": "big.jsonl", "size": fetch.MAX_TOTAL_BYTES + 1}
            ]
        },
    )
    with pytest.raises(fetch.FetchError, match="over the"):
        fetch.plan("https://huggingface.co/datasets/owner/name", {})


# ---- downloading -----------------------------------------------------------------


def test_files_land_under_the_directory_given(monkeypatch, tmp_path):
    _stub(monkeypatch, {"a.jsonl": b'{"role":"user"}'})
    written = list(
        fetch.download(
            "hf",
            [fetch.Remote("logs/a.jsonl", "https://huggingface.co/a.jsonl")],
            tmp_path,
            {},
        )
    )
    assert written == [tmp_path / "logs" / "a.jsonl"]
    assert written[0].read_bytes() == b'{"role":"user"}'


@pytest.mark.parametrize(
    "name",
    ["../escape.jsonl", "../../etc/passwd", "a/../../out.jsonl", "/absolute.jsonl"],
    ids=["parent", "deep-parent", "mid-parent", "absolute"],
)
def test_a_remote_name_cannot_escape_the_directory(monkeypatch, tmp_path, name):
    """The listing is remote data; a crafted path must not write outside."""
    _stub(monkeypatch, {"x": b"{}"})
    written = list(
        fetch.download("hf", [fetch.Remote(name, "https://huggingface.co/x")], tmp_path, {})
    )
    for path in written:
        assert path.resolve().is_relative_to(tmp_path.resolve()), f"escaped: {path}"


# ---- where a download lands --------------------------------------------------------


def test_the_default_is_beside_where_the_viewer_was_launched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert fetch.destination(None) == (tmp_path / fetch.DEFAULT_DIR).resolve()
    assert fetch.destination("  ") == (tmp_path / fetch.DEFAULT_DIR).resolve()


def test_a_relative_path_is_taken_from_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert fetch.destination("data/runs") == (tmp_path / "data" / "runs").resolve()


def test_a_tilde_is_expanded(tmp_path):
    assert fetch.destination("~/somewhere") == (Path.home() / "somewhere").resolve()


def test_dot_dot_collapses_before_the_path_is_used(tmp_path, monkeypatch):
    """The path arrives from the page, so it is resolved before it is trusted."""
    monkeypatch.chdir(tmp_path)
    assert ".." not in str(fetch.destination("a/../b"))


@pytest.mark.parametrize(
    "raw",
    ["/", "/usr", "/usr/local/lib", "/etc", "/System/Library", "~"],
    ids=["root", "usr", "under-usr", "etc", "system", "home-itself"],
)
def test_somewhere_nothing_should_be_written_is_refused(raw):
    with pytest.raises(fetch.FetchError, match="not somewhere to download into"):
        fetch.destination(raw)


def test_a_parent_of_home_is_refused():
    parent = str(Path.home().resolve().parent)
    with pytest.raises(fetch.FetchError):
        fetch.destination(parent)


def test_an_ordinary_folder_under_home_is_fine():
    target = fetch.destination("~/Documents/transcript-downloads")
    assert target.is_relative_to(Path.home().resolve())


def test_a_repository_reports_where_to_browse_it(monkeypatch):
    """A collection built from a repo links back to the folder it mirrors."""
    _stub(monkeypatch, {"api/datasets": [{"type": "file", "path": "a.jsonl"}]})
    plan = fetch.plan("https://huggingface.co/datasets/owner/name/tree/v2/inner", {})
    assert plan.web == "https://huggingface.co/datasets/owner/name/tree/v2"


def test_a_github_repository_reports_where_to_browse_it(monkeypatch):
    _stub(monkeypatch, {"git/trees": {"tree": [{"type": "blob", "path": "logs/a.jsonl"}]}})
    plan = fetch.plan("https://github.com/owner/name/tree/main/logs", {})
    assert plan.web == "https://github.com/owner/name/tree/main"


def test_a_bare_link_has_nowhere_to_browse():
    assert fetch.plan("https://example.com/a.jsonl", {}).web == ""


# ---- s3, read through the aws CLI --------------------------------------------------


def _aws_stub(monkeypatch, stdout="", code=0, stderr=""):
    """Stand in for the aws CLI, recording exactly what it was asked to run."""
    seen = {}

    class Done:
        returncode, stdout, stderr = code, "", ""

    def run(command, **kwargs):
        seen["command"] = command
        seen["shell"] = kwargs.get("shell", False)
        done = Done()
        done.stderr = stderr
        # Honour --prefix like the real thing, so a test that depends on the
        # prefix being sent actually depends on it.
        body = stdout
        if stdout and "--prefix" in command:
            prefix = command[command.index("--prefix") + 1]
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and "Contents" in data:
                data = dict(data)
                data["Contents"] = [
                    row for row in data["Contents"]
                    if str(row.get("Key", "")).startswith(prefix)
                ]
                body = json.dumps(data)
        done.stdout = body
        return done

    monkeypatch.setattr(fetch.shutil, "which", lambda name: "/usr/bin/aws")
    monkeypatch.setattr(fetch.subprocess, "run", run)
    return seen


LISTING = json.dumps({
    "Contents": [
        {"Key": "runs/a.jsonl", "Size": 10},
        {"Key": "runs/notes.md", "Size": 5},
        {"Key": "runs/deep/b.jsonl", "Size": 7},
    ]
})


def test_an_s3_url_lists_what_is_convertible(monkeypatch):
    seen = _aws_stub(monkeypatch, stdout=LISTING)
    plan = fetch.plan("s3://rr-agent-transcripts/runs", {"aws": "rw-eng"})
    assert plan.service == "s3"
    assert plan.label == "rr-agent-transcripts"
    assert [f.name for f in plan.files] == ["a.jsonl", "notes.md", "deep/b.jsonl"]
    assert plan.web == "s3://rr-agent-transcripts/runs"


def test_the_cli_is_run_without_a_shell(monkeypatch):
    """A shell would make every character of a bucket name a hazard."""
    seen = _aws_stub(monkeypatch, stdout=LISTING)
    fetch.plan("s3://rr-agent-transcripts/runs", {"aws": "rw-eng"})
    assert seen["shell"] is False
    assert seen["command"][0] == "aws"
    assert "--profile" in seen["command"]
    assert seen["command"][seen["command"].index("--profile") + 1] == "rw-eng"


def test_no_profile_means_the_default_one(monkeypatch):
    seen = _aws_stub(monkeypatch, stdout=LISTING)
    fetch.plan("s3://rr-agent-transcripts/runs", {})
    assert "--profile" not in seen["command"]


@pytest.mark.parametrize(
    "bucket",
    ["-rf", "UPPER", "a", "x" * 70, "has space", "semi;colon"],
    ids=["flag-like", "uppercase", "too-short", "too-long", "space", "semicolon"],
)
def test_a_bucket_name_that_is_not_one_is_refused(monkeypatch, bucket):
    _aws_stub(monkeypatch, stdout=LISTING)
    with pytest.raises(fetch.FetchError, match="valid S3 bucket"):
        fetch.plan(f"s3://{bucket}/runs", {})


def test_a_profile_that_could_be_a_flag_is_refused(monkeypatch):
    _aws_stub(monkeypatch, stdout=LISTING)
    with pytest.raises(fetch.FetchError, match="usable AWS profile"):
        fetch.plan("s3://rr-agent-transcripts/runs", {"aws": "--endpoint-url"})


@pytest.mark.parametrize(
    "said",
    [
        "Error when retrieving token from sso: Token has expired",
        "An error occurred (NoCredentials): Unable to locate credentials.",
        "The SSO session associated with this profile has expired",
    ],
    ids=["expired-token", "no-credentials", "expired-session"],
)
def test_any_missing_session_says_how_to_fix_it(monkeypatch, said):
    """The CLI words it several ways, and its own advice is the wrong command."""
    _aws_stub(monkeypatch, code=255, stderr=said)
    with pytest.raises(fetch.FetchError, match="aws sso login --profile rw-eng"):
        fetch.plan("s3://rr-agent-transcripts/runs", {"aws": "rw-eng"})


def test_the_only_configured_profile_is_used_without_being_named(monkeypatch):
    """With one profile there is nothing to disambiguate, so nothing to ask."""
    monkeypatch.setattr(fetch, "_profiles", lambda: ["rw-eng"])
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert fetch.resolve_profile("") == "rw-eng"


def test_several_profiles_means_one_must_be_chosen(monkeypatch):
    monkeypatch.setattr(fetch, "_profiles", lambda: ["a", "b"])
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert fetch.resolve_profile("") == ""


def test_a_configured_profile_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    monkeypatch.setattr(fetch, "_profiles", lambda: ["a", "b"])
    assert fetch.resolve_profile("chosen") == "chosen"
    assert fetch.resolve_profile("") == "from-env"


def test_a_profile_from_the_environment_that_could_be_a_flag_is_ignored(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "--endpoint-url")
    monkeypatch.setattr(fetch, "_profiles", lambda: [])
    assert fetch.resolve_profile("") == ""


def test_a_missing_cli_is_reported_plainly(monkeypatch):
    monkeypatch.setattr(fetch.shutil, "which", lambda name: None)
    with pytest.raises(fetch.FetchError, match="aws CLI is not installed"):
        fetch.plan("s3://rr-agent-transcripts/runs", {})


def test_an_s3_download_shells_out_per_object(monkeypatch, tmp_path):
    seen = _aws_stub(monkeypatch, stdout="")
    written = list(
        fetch.download(
            "s3",
            [fetch.Remote("a.jsonl", "s3://bucket/runs/a.jsonl")],
            tmp_path,
            {"aws": "rw-eng"},
        )
    )
    assert written == [tmp_path / "a.jsonl"]
    assert seen["command"][:3] == ["aws", "s3", "cp"]
    assert seen["command"][3] == "s3://bucket/runs/a.jsonl"


def test_an_empty_bucket_says_nothing_convertible(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": []}))
    with pytest.raises(fetch.FetchError, match="Nothing convertible"):
        fetch.plan("s3://rr-agent-transcripts/runs", {})


def test_an_archive_counts_as_something_worth_fetching(monkeypatch):
    """A bucket of agent runs is mostly zips, one per session."""
    _aws_stub(monkeypatch, stdout=json.dumps({
        "Contents": [
            {"Key": "chippy/abc/transcripts.zip", "Size": 11692},
            {"Key": "chippy/abc/notes.md", "Size": 12},
        ]
    }))
    plan = fetch.plan("s3://rr-agent-transcripts/chippy/abc", {"aws": "rw-eng"})
    # The Markdown comes too: a corpus that publishes transcripts as Markdown
    # and nothing else was unreachable while it was filtered out. One that is
    # not a transcript simply does not index.
    assert [f.name for f in plan.files] == ["transcripts.zip", "notes.md"]


def test_the_listing_is_capped_rather_than_exhaustive(monkeypatch):
    """A bucket can hold six figures of objects; pulling them all to refuse
    them wastes everyone's time."""
    seen = _aws_stub(monkeypatch, stdout=json.dumps({"Contents": []}))
    with pytest.raises(fetch.FetchError):
        fetch.plan("s3://rr-agent-transcripts", {})
    assert "--max-items" in seen["command"]
    assert seen["command"][seen["command"].index("--max-items") + 1] == str(
        fetch.MAX_FILES + 1
    )


def test_too_many_objects_suggests_a_prefix(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({
        "Contents": [{"Key": f"a/{i}.zip", "Size": 1} for i in range(fetch.MAX_FILES + 1)]
    }))
    with pytest.raises(fetch.FetchError, match="Point at a prefix"):
        fetch.plan("s3://rr-agent-transcripts", {})


def test_a_web_repository_is_told_to_use_a_subdirectory_instead(monkeypatch):
    _stub(monkeypatch, {"api/datasets": [
        {"type": "file", "path": f"{i}.jsonl"} for i in range(fetch.MAX_FILES + 1)
    ]})
    with pytest.raises(fetch.FetchError, match="Point at a subdirectory"):
        fetch.plan("https://huggingface.co/datasets/owner/name", {})


# ---- browsing a level at a time ----------------------------------------------------


def test_s3_browsing_returns_folders_and_files(monkeypatch):
    """A delimiter listing is what makes a 118,801-object bucket browsable."""
    seen = _aws_stub(monkeypatch, stdout=json.dumps({
        "CommonPrefixes": [{"Prefix": "chippy/abc/"}, {"Prefix": "chippy/def/"}],
        "Contents": [
            {"Key": "chippy/loose.zip", "Size": 40},
            {"Key": "chippy/readme.md", "Size": 2},
        ],
    }))
    nodes = fetch.browse("s3://bucket", "chippy", {"aws": "rw-eng"})
    assert "--delimiter" in seen["command"]
    assert [(n.kind, n.name) for n in nodes] == [
        ("folder", "abc"), ("folder", "def"),
        ("file", "loose.zip"), ("file", "readme.md"),
    ]
    assert [n.path for n in nodes][0] == "chippy/abc"


def test_browsing_a_single_file_url_says_there_is_nothing_inside():
    with pytest.raises(fetch.FetchError, match="nothing to look inside"):
        fetch.browse("https://example.com/a.jsonl", "", {})


def test_github_browsing_reads_one_level(monkeypatch):
    _stub(monkeypatch, {"contents": [
        {"type": "dir", "path": "logs/inner", "size": 0},
        {"type": "file", "path": "logs/a.jsonl", "size": 12},
        {"type": "file", "path": "logs/notes.md", "size": 3},
    ]})
    nodes = fetch.browse("https://github.com/owner/name/tree/main/logs", "", {})
    assert [(n.kind, n.name) for n in nodes] == [
        ("folder", "inner"), ("file", "a.jsonl"), ("file", "notes.md"),
    ]


def test_hf_browsing_reads_one_level(monkeypatch):
    _stub(monkeypatch, {"api/datasets": [
        {"type": "directory", "path": "attacks", "size": 0},
        {"type": "file", "path": "manifest.json", "size": 9},
    ]})
    nodes = fetch.browse("https://huggingface.co/datasets/owner/name", "", {})
    assert [(n.kind, n.name) for n in nodes] == [
        ("folder", "attacks"), ("file", "manifest.json"),
    ]


# ---- downloading only what was ticked ----------------------------------------------


def test_a_ticked_folder_means_everything_under_it(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": [
        {"Key": "chippy/abc/one.zip", "Size": 1},
        {"Key": "chippy/abc/two.zip", "Size": 2},
        {"Key": "chippy/def/three.zip", "Size": 3},
    ]}))
    plan = fetch.select("s3://bucket", ["chippy/abc"], {"aws": "rw-eng"})
    assert [f.name for f in plan.files] == ["chippy/abc/one.zip", "chippy/abc/two.zip"]


def test_a_ticked_file_brings_only_itself(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": [
        {"Key": "a/one.zip", "Size": 1},
        {"Key": "a/two.zip", "Size": 2},
    ]}))
    plan = fetch.select("s3://bucket", ["a/two.zip"], {"aws": "rw-eng"})
    assert [f.name for f in plan.files] == ["a/two.zip"]


def test_ticking_nothing_is_refused(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": [{"Key": "a.zip", "Size": 1}]}))
    with pytest.raises(fetch.FetchError, match="Nothing was ticked"):
        fetch.select("s3://bucket", [], {})


def test_a_selection_is_still_held_to_the_limits(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": [
        {"Key": f"a/{i}.zip", "Size": fetch.MAX_TOTAL_BYTES} for i in range(3)
    ]}))
    with pytest.raises(fetch.FetchError, match="over the"):
        fetch.select("s3://bucket", ["a"], {})


def test_a_prefix_name_cannot_be_smuggled_past_the_pattern(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": []}))
    with pytest.raises(fetch.FetchError, match="will not pass to the CLI"):
        fetch.browse("s3://bucket", "a$(whoami)", {})


# ---- browse paths and plan names have to meet --------------------------------------


def test_a_ticked_folder_in_a_repo_subdirectory_matches(monkeypatch):
    """Browse paths are relative to the URL's folder; a plan names files from the
    repository root. Without rebasing, ticking a folder matched nothing at all."""
    _stub(monkeypatch, {"api/datasets": [
        {"type": "file", "path": "attacks/model_priors/a/transcript.jsonl", "size": 10},
        {"type": "file", "path": "attacks/model_priors/b/transcript.jsonl", "size": 20},
        {"type": "file", "path": "attacks/counting/c/transcript.jsonl", "size": 40},
    ]})
    url = "https://huggingface.co/datasets/o/n/tree/main/attacks"
    got = fetch.measure(url, ["model_priors"], {})
    assert got == {"files": 2, "bytes": 30, "capped": False}


def test_the_same_rebasing_applies_when_downloading(monkeypatch):
    _stub(monkeypatch, {"api/datasets": [
        {"type": "file", "path": "attacks/model_priors/a/transcript.jsonl", "size": 10},
        {"type": "file", "path": "attacks/counting/c/transcript.jsonl", "size": 40},
    ]})
    url = "https://huggingface.co/datasets/o/n/tree/main/attacks"
    plan = fetch.select(url, ["model_priors"], {})
    assert [f.name for f in plan.files] == ["attacks/model_priors/a/transcript.jsonl"]


def test_a_repository_root_needs_no_rebasing(monkeypatch):
    _stub(monkeypatch, {"api/datasets": [
        {"type": "file", "path": "logs/a.jsonl", "size": 1},
        {"type": "file", "path": "other/b.jsonl", "size": 2},
    ]})
    got = fetch.measure("https://huggingface.co/datasets/o/n", ["logs"], {})
    assert got["files"] == 1


def test_s3_paths_need_no_rebasing(monkeypatch):
    """Both sides are already relative to the prefix."""
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": [
        {"Key": "chippy/abc/one.zip", "Size": 5},
        {"Key": "chippy/def/two.zip", "Size": 7},
    ]}))
    got = fetch.measure("s3://bucket/chippy", ["abc"], {"aws": "rw-eng"})
    assert got["files"] == 1 and got["bytes"] == 5


def test_measuring_nothing_is_nothing():
    assert fetch.measure("s3://bucket", [], {}) == {
        "files": 0, "bytes": 0, "capped": False,
    }


def test_a_measurement_over_the_limit_says_so(monkeypatch):
    _aws_stub(monkeypatch, stdout=json.dumps({"Contents": [
        {"Key": f"a/{i}.zip", "Size": 1} for i in range(fetch.MAX_FILES + 1)
    ]}))
    got = fetch.measure("s3://bucket", ["a"], {})
    assert got["capped"] is True


def test_a_file_already_here_at_the_size_the_listing_gives_is_not_fetched_again(
    tmp_path, monkeypatch
):
    """What makes following a bucket affordable. Without it every sync
    downloads the whole place to discover it already had it.

    Agent logs are written once and not edited, so name and size agreeing is
    enough for the case this exists to serve. It is a heuristic, and the
    stricter answer is on `Remote` waiting to be used.
    """
    (tmp_path / "here.jsonl").write_text("x" * 100)
    fetched = []
    monkeypatch.setattr(
        fetch, "_s3_download",
        lambda remote, target, profile: (
            fetched.append(remote.name), target.write_text("y" * 50)
        ),
    )

    got = list(fetch.download("s3", [
        fetch.Remote(name="here.jsonl", url="s3://b/here.jsonl", size=100),
        fetch.Remote(name="new.jsonl", url="s3://b/new.jsonl", size=50),
    ], tmp_path, {}))

    assert fetched == ["new.jsonl"]
    assert [p.name for p in got] == ["new.jsonl"]


def test_a_file_here_at_a_different_size_is_fetched_again(tmp_path, monkeypatch):
    """Half a download from an interrupted run must not be mistaken for the
    file it was going to be."""
    (tmp_path / "part.jsonl").write_text("x" * 20)
    fetched = []
    monkeypatch.setattr(
        fetch, "_s3_download",
        lambda remote, target, profile: (
            fetched.append(remote.name), target.write_text("y" * 100)
        ),
    )

    list(fetch.download("s3", [
        fetch.Remote(name="part.jsonl", url="s3://b/part.jsonl", size=100)
    ], tmp_path, {}))

    assert fetched == ["part.jsonl"]


def test_what_the_bucket_said_about_a_version_is_kept(monkeypatch):
    """Thrown away, "what changed since last time" is unanswerable without
    downloading the bucket to find out."""
    monkeypatch.setattr(fetch, "_aws", lambda *a, **k: json.dumps({
        "Contents": [{
            "Key": "runs/a.jsonl", "Size": 12,
            "ETag": '"d41d8cd98f00b204e9800998ecf8427e"',
            "LastModified": "2026-08-25T10:00:00+00:00",
        }]
    }))

    plan = fetch.plan("s3://bucket/runs", {})

    assert plan.files[0].etag == "d41d8cd98f00b204e9800998ecf8427e"
    assert plan.files[0].modified == "2026-08-25T10:00:00+00:00"
