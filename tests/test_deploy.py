"""How a copy of this is set up, before anyone uses it.

The property worth guarding is that a deployment which sets nothing is the
laptop tool it has always been. Everything else here is one program configured
two ways, so a default that quietly changed would change what people already
have.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from transcript_viewer import deploy


@pytest.fixture
def written(tmp_path):
    def write(text: str):
        path = tmp_path / "deploy.toml"
        path.write_text(text)
        return path

    return write


def test_a_copy_that_is_told_nothing_is_the_tool_it_always_was(tmp_path):
    """The whole plan rests on this: one program, and the defaults are local."""
    site = deploy.read(tmp_path / "absent.toml", environ={})

    assert site.host == "127.0.0.1"
    assert site.identity_header == ""
    assert site.shared is False
    assert site.database == ""
    assert site.secrets == "user"
    assert site.scan_roots == ()


def test_every_setting_has_somewhere_to_be_written(written):
    """A field nobody can set from the file is a field only its default has."""
    assert set(deploy.WHERE) == {spec.name for spec in fields(deploy.Deployment)}


def test_the_file_is_read(written):
    site = deploy.read(
        written(
            """
            name = "Redwood Transcripts"

            [server]
            host = "0.0.0.0"

            [identity]
            header = "x-amzn-oidc-identity"
            admins = ["ann@example.com", "bo@example.com"]

            [scan]
            roots = ["/data/transcripts"]
            """
        ),
        environ={},
    )

    assert site.host == "0.0.0.0"
    assert site.identity_header == "x-amzn-oidc-identity"
    assert site.admins == ("ann@example.com", "bo@example.com")
    assert site.scan_roots == ("/data/transcripts",)
    assert site.name == "Redwood Transcripts"
    assert site.shared is True


def test_the_environment_beats_the_file(written):
    """A container's environment is a more recent act than an image's file."""
    path = written('[server]\nhost = "0.0.0.0"\n')
    site = deploy.read(path, environ={"TRANSCRIPT_VIEWER_HOST": "10.0.0.1"})

    assert site.host == "10.0.0.1"


def test_a_list_can_be_given_where_the_environment_has_no_lists(written):
    site = deploy.read(
        written(""), environ={"TRANSCRIPT_VIEWER_ADMINS": "ann@x.com, bo@x.com"}
    )

    assert site.admins == ("ann@x.com", "bo@x.com")


def test_a_damaged_file_leaves_the_defaults_rather_than_refusing_to_start(written):
    """Refusing to start because one line of TOML is wrong is worse than
    starting as a laptop does — and on a laptop there is no file at all."""
    site = deploy.read(written("this is not toml {{{"), environ={})

    assert site.host == "127.0.0.1"
    assert site.shared is False


def test_what_is_current_can_be_replaced_and_put_back():
    """Tests and the command line both need to say what this copy is."""
    try:
        deploy.use(deploy.Deployment(name="Somewhere else"))
        assert deploy.current().name == "Somewhere else"
    finally:
        deploy.use(None)


def test_a_deployment_can_refuse_to_send_transcripts_anywhere(written):
    """The transcripts in a deployment are not always the operator's to send.

    A run classified above the lowest level is still classified once it has
    been summarised, and the summarising is an egress to a third party. The
    per-transcript switch that used to cover this was removed as redundant when
    the only data in question was the operator's own.
    """
    assert deploy.read(written(""), environ={}).ai == "on"

    off = deploy.read(written("[ai]\nenabled = \"off\"\n"), environ={})
    assert off.ai == "off"


def test_turning_the_model_off_beats_any_key(written, monkeypatch):
    """No key, typed or configured, brings it back — and the refusal says so
    rather than reading like a missing dependency, which would invite someone
    to go and install one."""
    from transcript_viewer import ai

    try:
        deploy.use(deploy.Deployment(ai="off"))
        monkeypatch.setattr(ai, "_client", lambda: object())
        ok, why = ai.status()
    finally:
        deploy.use(None)

    assert ok is False
    assert "does not send" in why
