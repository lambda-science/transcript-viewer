"""The choices made when this was deployed, as opposed to by whoever is using it.

`config.py` holds what a person types into settings: their API key, their AWS
profile. This holds what an operator decided before anyone arrived — what to
bind to, whose word to take for who someone is, where the annotations live.
Two different things with two different lifetimes, so two different files: one
is written by the running program, the other is never written at all.

Every value has a default, and the defaults are the tool as it has always been:
loopback, nobody signed in, SQLite beside the index. A deployment that sets
nothing behaves exactly as a laptop does, which is the point — there is one
program here, not a local one and a hosted one.

Resolution is environment first, then the file, then the default. That is the
opposite order to `config.secret`, deliberately: a typed-in key should outlast
a restart, while a container's environment is the more explicit and more recent
act than a file baked into an image.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from . import store

CONFIG_PATH = store.ROOT / "deploy.toml"

# Where an operator's answer is looked for, per setting. `section.name` in the
# file, `TRANSCRIPT_VIEWER_SECTION_NAME` in the environment.
PREFIX = "TRANSCRIPT_VIEWER"

# Where each setting is written in the file. Spelled out rather than derived
# from the field name: `admins` belongs under `[identity]`, and any rule clever
# enough to know that is a rule nobody can read off the page.
WHERE = {
    "host": "server.host",
    "identity_header": "identity.header",
    "admins": "identity.admins",
    "database": "storage.database",
    "secrets": "secrets.mode",
    "cache_bytes": "cache.bytes",
    "sources": "sources.urls",
    "name": "name",
}


@dataclass(frozen=True)
class Deployment:
    """How this copy is set up. Defaults are the local single-user tool."""

    # Loopback unless told otherwise. The threat model of every route assumes
    # it: there is no CSRF token and several routes spawn processes or spend
    # money, so widening this without setting `identity_header` would publish
    # an unauthenticated remote shell-adjacent surface.
    host: str = "127.0.0.1"

    # The header carrying an authenticated identity, injected by whatever sits
    # in front — an AWS load balancer's `x-amzn-oidc-identity`, Tailscale
    # Serve's user header. Empty means nobody is signed in and one person is
    # using this. Never sniffed: a header is trusted only when named here,
    # because a header nobody put there is a header anybody can put there.
    identity_header: str = ""

    # Who may reach the routes that configure the server rather than read it.
    # Empty in single-user mode means the one local person; empty with an
    # identity header set means nobody, which is the safe way round.
    admins: tuple[str, ...] = ()

    # Where annotations live. Empty is SQLite at the usual path.
    database: str = ""

    # "user" lets people type their own keys into settings, as now. "server"
    # means the operator supplies them and the settings route stops accepting
    # them at all.
    secrets: str = "user"

    # How much converted output to keep in memory, in bytes. The default suits
    # a laptop; a container sized smaller than it will be killed holding it.
    cache_bytes: str = ""

    # Places to keep in step with, for `transcript-viewer sync`. A deployment
    # fed from a bucket names it once here rather than in whatever runs the
    # schedule, so the two cannot disagree about what this holds.
    sources: tuple[str, ...] = ()

    name: str = "Transcript Viewer"

    @property
    def shared(self) -> bool:
        """Whether more than one person can reach this."""
        return bool(self.identity_header)


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _from_env(name: str, environ: dict[str, str]) -> str | None:
    return environ.get(f"{PREFIX}_{name.upper()}")


def read(path: Path | None = None, environ: dict[str, str] | None = None) -> Deployment:
    """Build the deployment from the environment and the file.

    Anything unreadable is ignored rather than raised. A viewer that refuses to
    start because one line of TOML is wrong is worse than one that starts as a
    laptop would — and on a laptop there is no file at all.
    """
    path = path if path is not None else CONFIG_PATH
    environ = os.environ if environ is None else environ

    on_disk: dict[str, Any] = {}
    try:
        on_disk = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        on_disk = {}

    values: dict[str, Any] = {}
    for spec in fields(Deployment):
        found = _from_env(spec.name, environ)
        if found is None:
            section, _, key = WHERE[spec.name].rpartition(".")
            where = on_disk.get(section, {}) if section else on_disk
            if isinstance(where, dict):
                found = where.get(key)
        if found is None:
            continue
        # The default says the shape. A setting that defaults to a tuple takes
        # a list in the file and a comma-separated string in the environment,
        # which has nowhere to put a list.
        if isinstance(spec.default, tuple):
            values[spec.name] = (
                _split(found)
                if isinstance(found, str)
                else tuple(str(x) for x in found)
            )
        else:
            values[spec.name] = str(found)
    return Deployment(**values)


_current: Deployment | None = None


def current() -> Deployment:
    """The deployment this process is running under, read once."""
    global _current
    if _current is None:
        _current = read()
    return _current


def use(deployment: Deployment | None) -> None:
    """Replace it. For the command line, and for tests."""
    global _current
    _current = deployment
