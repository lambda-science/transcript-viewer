"""Fetch credentials and AWS profile settings."""

import json
import stat

import pytest

from transcript_viewer import config


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "nested" / "config.json"


def test_a_token_round_trips_without_being_exposed_in_state(cfg):
    token = "g" * 40
    config.set_secret("github", token, cfg)

    assert config.secret("github", cfg) == token
    assert token not in json.dumps(config.state(cfg))
    assert config.state(cfg)["github"]["hint"] == "…gggg"


def test_the_environment_stands_in_for_a_missing_token(cfg, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-environment")

    assert config.source("github", cfg) == "environment"
    assert config.secret("github", cfg) == "from-the-environment"
    assert config.stored("github", cfg) is None


def test_clearing_falls_back_to_the_environment(cfg, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-environment")
    config.set_secret("github", "g" * 40, cfg)
    config.clear_secret("github", cfg)

    assert config.secret("github", cfg) == "from-the-environment"


def test_the_file_is_private(cfg):
    config.set_secret("github", "g" * 40, cfg)

    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("bad", ["", "short", "x y" * 10, "x" * 400])
def test_obvious_non_tokens_are_refused(cfg, bad):
    with pytest.raises(ValueError):
        config.set_secret("github", bad, cfg)


def test_tokens_are_gathered_for_fetching(cfg, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    config.set_secret("github", "g" * 40, cfg)

    assert config.tokens(cfg) == {"github": "g" * 40, "aws": ""}


def test_an_aws_profile_is_stored_in_full(cfg):
    config.set_aws_profile("rw-eng", cfg)

    assert config.aws_profile(cfg) == "rw-eng"
    assert config.tokens(cfg)["aws"] == "rw-eng"


def test_a_profile_name_that_could_be_a_flag_is_refused(cfg):
    for bad in ("--profile", "a b", "x;rm -rf /", "y" * 100):
        with pytest.raises(ValueError, match="AWS profile name"):
            config.set_aws_profile(bad, cfg)


def test_a_damaged_config_reads_as_empty(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not json")

    assert config.stored("github", cfg) is None
