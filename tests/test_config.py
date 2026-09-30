"""Settings, and the handling of a stored API key.

The key is the one piece of data here that would matter if it leaked, so most
of these tests are about where it does *not* go: not into the page, not into a
world-readable file, not into a response.
"""

import json
import stat

import pytest

from transcript_viewer import config


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "nested" / "config.json"


KEY = "sk-ant-api03-" + "x" * 40


def test_no_key_no_source(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert config.stored("anthropic", cfg) is None
    assert config.source("anthropic", cfg) is None
    assert config.hint("anthropic", cfg) == ""


def test_a_key_round_trips(cfg):
    config.set_secret("anthropic", KEY, cfg)
    assert config.stored("anthropic", cfg) == KEY
    assert config.source("anthropic", cfg) == "settings"


def test_the_environment_is_seen_when_nothing_is_stored(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    assert config.source("anthropic", cfg) == "environment"
    assert config.stored("anthropic", cfg) is None


def test_a_stored_key_wins_over_the_environment(cfg, monkeypatch):
    """Whichever the user set most explicitly should be the one in use."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    config.set_secret("anthropic", KEY, cfg)
    assert config.source("anthropic", cfg) == "settings"


def test_clearing_falls_back_to_the_environment(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    config.set_secret("anthropic", KEY, cfg)
    config.clear_secret("anthropic", cfg)
    assert config.stored("anthropic", cfg) is None
    assert config.source("anthropic", cfg) == "environment"


def test_the_hint_shows_four_characters_and_no_more(cfg):
    config.set_secret("anthropic", KEY, cfg)
    hint = config.hint("anthropic", cfg)
    assert hint == "…xxxx"
    assert KEY not in hint


def test_the_file_is_readable_only_by_its_owner(cfg):
    config.set_secret("anthropic", KEY, cfg)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.parent.stat().st_mode) == 0o700


def test_clearing_does_not_leave_the_key_in_the_file(cfg):
    config.set_secret("anthropic", KEY, cfg)
    config.clear_secret("anthropic", cfg)
    assert KEY not in cfg.read_text()


def test_other_settings_survive_a_key_change(cfg):
    config.store.write_json(cfg, {"version": 1, "theme": "dark"}, private=True)
    config.set_secret("anthropic", KEY, cfg)
    config.clear_secret("anthropic", cfg)
    assert json.loads(cfg.read_text())["theme"] == "dark"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "short", "sk-ant " + "x" * 40, "x" * 400],
    ids=["empty", "blank", "too-short", "contains-a-space", "too-long"],
)
def test_obvious_non_keys_are_refused(cfg, bad):
    with pytest.raises(ValueError):
        config.set_secret("anthropic", bad, cfg)
    assert not cfg.exists()


def test_a_damaged_config_reads_as_empty_rather_than_raising(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not json")
    assert config.stored("anthropic", cfg) is None


# ---- more than one credential --------------------------------------------------


@pytest.mark.parametrize("name", sorted(config.SECRETS))
def test_every_secret_round_trips(cfg, name):
    value = "x" * 40
    config.set_secret(name, value, cfg)
    assert config.stored(name, cfg) == value
    assert config.source(name, cfg) == "settings"


def test_secrets_do_not_collide(cfg):
    config.set_secret("anthropic", "a" * 40, cfg)
    config.set_secret("hf", "h" * 40, cfg)
    config.set_secret("github", "g" * 40, cfg)
    assert config.stored("anthropic", cfg) == "a" * 40
    assert config.stored("hf", cfg) == "h" * 40
    assert config.stored("github", cfg) == "g" * 40


def test_clearing_one_leaves_the_others(cfg):
    config.set_secret("hf", "h" * 40, cfg)
    config.set_secret("github", "g" * 40, cfg)
    config.clear_secret("hf", cfg)
    assert config.stored("hf", cfg) is None
    assert config.stored("github", cfg) == "g" * 40


@pytest.mark.parametrize(
    ("name", "variable"),
    [("hf", "HF_TOKEN"), ("github", "GITHUB_TOKEN"), ("anthropic", "ANTHROPIC_API_KEY")],
)
def test_the_environment_stands_in_for_a_missing_token(cfg, monkeypatch, name, variable):
    monkeypatch.setenv(variable, "from-the-environment")
    assert config.source(name, cfg) == "environment"
    assert config.secret(name, cfg) == "from-the-environment"
    assert config.stored(name, cfg) is None, "the environment is not 'stored'"


def test_state_never_carries_a_value(cfg):
    secret = "sk-ant-api03-" + "z" * 40
    config.set_secret("anthropic", secret, cfg)
    state = config.state(cfg)
    assert secret not in json.dumps(state), "a token reached what the page is sent"
    assert state["anthropic"]["hint"] == "…zzzz"
    assert state["anthropic"]["source"] == "settings"
    assert set(state) == set(config.SECRETS)
    assert not any("token" in v or "key" in v for v in state.values())


def test_an_unknown_secret_is_refused(cfg):
    with pytest.raises(ValueError, match="unknown secret"):
        config.set_secret("nope", "x" * 40, cfg)


def test_tokens_are_gathered_for_fetching(cfg, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    config.set_secret("hf", "h" * 40, cfg)
    assert config.tokens(cfg) == {"hf": "h" * 40, "github": None, "aws": ""}


def test_an_aws_profile_is_stored_in_full(cfg):
    """A profile is a name, not a credential — the CLI holds the credential."""
    config.set_aws_profile("rw-eng", cfg)
    assert config.aws_profile(cfg) == "rw-eng"
    assert config.tokens(cfg)["aws"] == "rw-eng"


def test_a_profile_name_that_could_be_a_flag_is_refused(cfg):
    """It reaches an argument list, where a leading dash reads as an option."""
    for bad in ("--profile", "a b", "x;rm -rf /", "y" * 100):
        with pytest.raises(ValueError, match="AWS profile name"):
            config.set_aws_profile(bad, cfg)


def test_clearing_the_profile_is_allowed(cfg):
    config.set_aws_profile("rw-eng", cfg)
    config.set_aws_profile("", cfg)
    assert config.aws_profile(cfg) == ""
