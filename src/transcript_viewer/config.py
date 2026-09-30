"""Viewer settings, including optional API tokens.

A token kept in a file is weaker than one in the system keychain or a password
manager, so this module is deliberately narrow about them:

  * they live at ~/.transcript-viewer/config.json, 0600, inside a 0700 directory;
  * they are never returned to the page, never logged, and never put in a URL —
    the page only ever learns that one is set and its last four characters;
  * they are only ever read to authenticate a request.

The environment is still honoured. With nothing stored, each service's usual
variables apply as they would anywhere else.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import deploy, store

CONFIG_PATH = store.ROOT / "config.json"
VERSION = 1

# Long enough to be a real token, short enough to reject a pasted paragraph.
MIN_TOKEN = 20
MAX_TOKEN = 300


@dataclass(frozen=True)
class Secret:
    """One credential the viewer can hold."""

    name: str
    label: str
    field: str  # where it is kept in config.json
    env: tuple[str, ...]  # the variables that stand in for it
    placeholder: str


SECRETS: dict[str, Secret] = {
    "github": Secret(
        "github",
        "GitHub token",
        "github_token",
        ("GITHUB_TOKEN", "GH_TOKEN"),
        "ghp_…",
    ),
}


# The AWS profile is a name rather than a credential — the CLI holds the
# credential — so it is stored and shown in full.
PROFILE_FIELD = "aws_profile"
# A leading "-" would be read as an option by the CLI, so a name may not
# start with one. Empty is allowed and means the default profile.
PROFILE_OK = re.compile(r"^(?:[\w.@][\w.@-]{0,63})?$")


def aws_profile(path: Path | None = None) -> str:
    value = load(path).get(PROFILE_FIELD)
    return value if isinstance(value, str) else ""


def set_aws_profile(value: str, path: Path | None = None) -> None:
    value = (value or "").strip()
    if not PROFILE_OK.match(value):
        raise ValueError("that does not look like an AWS profile name")
    path = path or CONFIG_PATH
    store.write_json(
        path, {**load(path), "version": VERSION, PROFILE_FIELD: value}, private=True
    )


def load(path: Path | None = None) -> dict[str, Any]:
    return store.read_json(path or CONFIG_PATH)


def _spec(name: str) -> Secret:
    secret = SECRETS.get(name)
    if secret is None:
        raise ValueError(f"unknown secret: {name}")
    return secret


def secret(name: str, path: Path | None = None) -> str | None:
    """The stored token, else whatever the environment supplies.

    A token typed into settings wins: it is the more explicit, more recent act,
    and `source()` shows which is in use so the precedence is never a surprise.

    Unless the operator supplies them. Then the file is not consulted at all —
    not merely outranked. A deployment's credentials are its own, and a key
    left in the file by an earlier local run would otherwise outrank the one
    the operator configured and quietly spend the wrong account.
    """
    spec = _spec(name)
    if deploy.current().secrets != "server":
        value = load(path).get(spec.field)
        if isinstance(value, str) and value:
            return value
    for variable in spec.env:
        from_env = os.environ.get(variable)
        if from_env:
            return from_env
    return None


def stored(name: str, path: Path | None = None) -> str | None:
    """Only what settings holds, ignoring the environment."""
    value = load(path).get(_spec(name).field)
    return value if isinstance(value, str) and value else None


def source(name: str, path: Path | None = None) -> str | None:
    """Where a credential comes from — for display, not for logic."""
    if stored(name, path):
        return "settings"
    if any(os.environ.get(v) for v in _spec(name).env):
        return "environment"
    return None


def hint(name: str, path: Path | None = None) -> str:
    """The last four characters of a stored token, enough to tell two apart."""
    value = stored(name, path)
    return f"…{value[-4:]}" if value else ""


def set_secret(name: str, value: str, path: Path | None = None) -> None:
    """Store a token. Raises ValueError on anything that is plainly not one."""
    spec = _spec(name)
    value = (value or "").strip()
    if not value:
        raise ValueError("no token given")
    if len(value) < MIN_TOKEN or len(value) > MAX_TOKEN:
        raise ValueError(f"that does not look like a {spec.label.lower()}")
    if any(c.isspace() for c in value):
        raise ValueError("a token contains no spaces — check what was pasted")

    path = path or CONFIG_PATH
    store.write_json(
        path, {**load(path), "version": VERSION, spec.field: value}, private=True
    )


def clear_secret(name: str, path: Path | None = None) -> None:
    """Forget a stored token, falling back to the environment."""
    path = path or CONFIG_PATH
    data = load(path)
    data.pop(_spec(name).field, None)
    store.write_json(path, {**data, "version": VERSION}, private=True)


def state(path: Path | None = None) -> dict[str, dict[str, str]]:
    """What the page may know: which tokens exist and where from. Never a value."""
    return {
        name: {
            "label": spec.label,
            "placeholder": spec.placeholder,
            "env": spec.env[0],
            "source": source(name, path) or "",
            "hint": hint(name, path),
        }
        for name, spec in SECRETS.items()
    }


def tokens(path: Path | None = None) -> dict[str, str | None]:
    """The fetch credentials, by service name."""
    return {
        "github": secret("github", path),
        # Not a credential: the aws CLI resolves the session from this name.
        "aws": aws_profile(path),
    }
