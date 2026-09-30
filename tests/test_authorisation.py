"""What each route lets a stranger do.

Every route in this program was written against a loopback threat model, and
said so. Off loopback several of them are not merely unprotected: one rewrites
the server's credentials, one wipes the library, one spawns a process with a
path the caller chose. These pin the answers so that widening the bind is a
decision rather than an accident.

The test that matters most is the boring one — that every route has an answer at
all. A forgotten route defaults to reachable, and a policy nobody wrote is the
failure that actually happens.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from transcript_viewer import deploy, identity, viewer

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "src/transcript_viewer/viewer.py").read_text()

SHARED = deploy.Deployment(
    identity_header="x-amzn-oidc-identity", admins=("ann@example.com",)
)


def _routes() -> set[tuple[str, str]]:
    """Every (verb, path) the server answers, read off the source.

    Read rather than listed, so this cannot agree with a stale copy of itself.
    Both spellings are picked up: most routes compare with `==`, and the last
    branch of a chain compares with `!=` before falling through to a 404.
    """
    found = set()
    for verb in ("GET", "POST", "DELETE"):
        start = SOURCE.index(f"def do_{verb}")
        after = [
            SOURCE.index(f"def do_{other}")
            for other in ("GET", "POST", "DELETE")
            if SOURCE.index(f"def do_{other}") > start
        ]
        body = SOURCE[start : min(after) if after else len(SOURCE)]
        for path in re.findall(r'url\.path [=!]= "([^"]+)"', body):
            found.add((verb, path))
    return found


def test_every_route_has_been_given_an_answer():
    """A route nobody classified is reachable by anyone, which is the failure
    that happens — not a policy someone chose wrongly."""
    missing = _routes() - set(viewer.POLICY)
    assert not missing, f"routes with no policy: {sorted(missing)}"


def test_no_policy_describes_a_route_that_is_gone():
    stale = set(viewer.POLICY) - _routes()
    assert not stale, f"policies for routes that no longer exist: {sorted(stale)}"


def test_the_dangerous_routes_are_the_ones_marked_dangerous():
    """Named individually rather than counted, so that relaxing one is a visible
    edit to this list rather than a number going down."""
    for route in [
        ("POST", "/api/settings"),
        ("POST", "/api/scan"),
        ("POST", "/api/fetch"),
        ("POST", "/api/refetch"),
        ("DELETE", "/api/library"),
        ("DELETE", "/api/group"),
    ]:
        assert viewer.POLICY[route] == "admin", route
    assert viewer.POLICY[("GET", "/api/reveal")] == "local"


class _Ask:
    """Just enough of a request for the guard to judge it."""

    def __init__(self, verb: str, path: str, headers: dict | None = None):
        self.command = verb
        self.headers = headers or {}
        self.path = path
        self.refused = False

    def _json(self, *args, **kwargs):
        self.refused = True

    _refuse = viewer._Handler._refuse
    _may = viewer._Handler._may


def _asks(verb, path, who=None, site=SHARED):
    headers = {"x-amzn-oidc-identity": who} if who else {}
    ask = _Ask(verb, path, headers)
    try:
        deploy.use(site)
        return ask._may(
            __import__("urllib.parse", fromlist=["urlparse"]).urlparse(path)
        )
    finally:
        deploy.use(None)


@pytest.mark.parametrize(
    ("verb", "path"),
    [("POST", "/api/settings"), ("POST", "/api/fetch"), ("DELETE", "/api/library")],
)
def test_a_stranger_cannot_reconfigure_a_shared_server(verb, path):
    """The one that matters: without this, the first visitor to a shared
    instance can overwrite the API keys everyone else's requests spend."""
    assert _asks(verb, path, who="bo@example.com") is False
    assert _asks(verb, path, who="ann@example.com") is True


def test_reading_a_transcript_needs_nothing_of_anybody():
    for path in ("/", "/api/index", "/api/trajectory", "/api/raw"):
        assert _asks("GET", path) is True


def test_annotating_needs_a_person_but_not_an_admin():
    assert _asks("POST", "/api/library") is False
    assert _asks("POST", "/api/library", who="bo@example.com") is True


def test_revealing_a_path_is_refused_wherever_it_would_be_somebody_elses_machine():
    """It opens a window on the server's desktop, and the path is the caller's
    to choose. Admin is not the question; being here is."""
    assert _asks("GET", "/api/reveal", who="ann@example.com") is False
    assert _asks("GET", "/api/reveal", site=deploy.Deployment()) is True


def test_a_laptop_is_left_exactly_as_it_was():
    """Nothing above applies until somebody can sign in."""
    local = deploy.Deployment()
    for verb, path in sorted(_routes()):
        assert _asks(verb, path, site=local) is True, (verb, path)


def test_an_unknown_path_is_a_404_rather_than_a_refusal():
    """A route that does not exist should say so. Answering 403 would tell a
    stranger which paths are real."""
    assert _asks("GET", "/api/invented") is True


def test_a_shared_server_stops_saying_what_it_is_holding():
    """Four characters is not a key, but it says which one is installed, and
    the profile list is the naming of an organisation's accounts. Both go out
    on every page load."""
    try:
        deploy.use(SHARED)
        shared = viewer._ai_state()
        deploy.use(deploy.Deployment())
        local = viewer._ai_state()
    finally:
        deploy.use(None)

    assert shared["aws_profiles"] == [] and shared["hint"] == ""
    assert shared["secrets"] == {} and shared["managed"] is True
    # The page still learns whether the feature works at all.
    assert "available" in shared
    assert local["managed"] is False


def test_who_may_administer_is_asked_of_identity_rather_than_answered_here():
    """One place decides, so relaxing it is one edit rather than a search."""
    assert identity.may_administer("ann@example.com", SHARED) is True
    assert identity.may_administer("bo@example.com", SHARED) is False


def test_the_operators_key_is_not_outranked_by_one_left_in_the_file(tmp_path, monkeypatch):
    from transcript_viewer import config

    settings = tmp_path / "config.json"
    settings.write_text('{"api_key": "sk-ant-typed-in-here-long-enough"}')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-operator")

    try:
        deploy.use(deploy.Deployment())
        assert config.secret("anthropic", settings) == "sk-ant-typed-in-here-long-enough"
        deploy.use(deploy.Deployment(secrets="server"))
        assert config.secret("anthropic", settings) == "sk-ant-from-the-operator"
    finally:
        deploy.use(None)
