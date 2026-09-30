"""Pull trajectories from Hugging Face and GitHub by URL.

Two rules shape this module.

**Private address space is unreachable.** The page hands a URL to a server
running as you, on your network, so an unguarded fetcher would let it reach your
router, a cloud metadata endpoint, or a service bound to localhost. Any URL is
allowed, but the host is resolved first and refused if it lands on a loopback,
link-local, private or otherwise reserved address — checked again after
redirects, since a redirect is a second request to a second host.

Hugging Face and GitHub are understood well enough to list a repository; every
other host is treated as a direct link to one file.

**S3 goes through the aws CLI.** `s3://bucket/prefix` is listed and downloaded
by running `aws`, never by holding a credential here: the CLI already owns the
SSO session, the profile configuration and the refresh logic, so shelling out to
it means this code never sees a key and never has to renew one. Arguments are
passed as a list with no shell, and anything arriving from the page is checked
against a strict pattern first, since an argument beginning with "-" would
otherwise be read as a flag.

**Nothing is downloaded without being listed first.** A dataset URL can name
hundreds of files; `plan()` says what would be fetched and how much it weighs,
and `download()` only runs once that has been seen.

Uses urllib rather than an SDK, so the viewer keeps its dependency-free install.
"""

from __future__ import annotations

import json
import re
import ipaddress
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, urlparse

# Hosts whose layout is understood well enough to list a repository. Anything
# else is fetched as a single file.
HOSTS = {
    "huggingface.co": "hf",
    "cdn-lfs.huggingface.co": "hf",
    "github.com": "github",
    "api.github.com": "github",
    "raw.githubusercontent.com": "github",
}

# What is worth fetching. Logs and trajectories, and the archives they arrive
# in: a bucket of agent transcripts is far more likely to hold one zip per
# session than loose JSONL, so excluding archives made S3 useless for exactly
# the case it was added for.
# A transcript is not always JSON: some corpora ship the readable rendering.
LOGS = (".jsonl", ".json", ".har", ".md", ".markdown")
# A published dataset is a container of runs rather than a log, but it is still
# a thing worth fetching: METR's transcripts come as Parquet shards.
# An Inspect .eval is a zip of JSON, and a corpus of them is where most
# control research keeps its transcripts.
DATASETS = (".parquet", ".eval")
ARCHIVES = (".zip", ".tgz", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".gz")
SUFFIXES = LOGS + DATASETS + ARCHIVES

# Downloads land beside where the viewer was launched, so they are visible and
# usable by other tools rather than buried in a dot-directory.
DEFAULT_DIR = "transcript-downloads"

# Somewhere a download must never be pointed at, however the path was typed.
# Deliberately not /var: macOS puts every temporary directory under
# /private/var/folders, so banning it refuses ordinary scratch paths.
PROTECTED = (
    "/bin", "/sbin", "/usr", "/etc", "/dev", "/System", "/Library", "/private/etc",
)

# Bucket, key prefix and profile names, strict enough that nothing reaches the
# argument list that could be read as a flag.
S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
S3_PREFIX = re.compile(r"^[\w!.*'()/ -]*$")
AWS_PROFILE = re.compile(r"^[\w.@][\w.@-]{0,63}$")
AWS_TIMEOUT = 300

# One folder's listing. Generous, but a browser draws it, so not unbounded.
BROWSE_LIMIT = 3_000

MAX_FILES = 2_000
# A ceiling on one fetch, not a judgement about what is worth having: published
# corpora are routinely several gigabytes — MonitoringBench ships as a single
# 2.5 GB archive — and two was refusing them. What still guards against an
# accident is the picker, which measures a selection and shows it before
# anything downloads; this is the backstop for the bucket nobody meant to take
# whole, which in practice is the 144 GB one.
MAX_TOTAL_BYTES = 32 * 1024**3
TIMEOUT = 60


class FetchError(RuntimeError):
    """Anything the person who pasted the URL needs to know about."""


class Node(NamedTuple):
    """One entry in a remote folder listing."""

    name: str
    path: str  # full path within the location, as the remote spells it
    kind: str  # "folder" | "file"
    size: int | None


class Plan(NamedTuple):
    """What a URL would bring in."""

    service: str
    label: str
    files: list["Remote"]
    web: str  # where to browse this on the site, "" if there is nowhere


@dataclass(frozen=True)
class Remote:
    """One file to fetch."""

    name: str  # path within the repo, used as the local name
    url: str
    size: int | None = None
    # What the far end says about this version of the file. Kept so a second
    # fetch of the same place can tell what changed rather than downloading a
    # bucket again to find out; a listing already carries both, and throwing
    # them away was what made "keep this in step with S3" mean "fetch it all".
    etag: str | None = None
    modified: str | None = None


def _check_host(url: str) -> str:
    """Refuse anything that resolves into private address space.

    An allowlist was simpler but ruled out every other host; this is the usual
    mitigation and lets a bare link work. It resolves the name and inspects the
    addresses rather than pattern-matching the text, so `127.0.0.1`,
    `localhost`, `0x7f.1`, and a public name pointed at a private address are
    all caught the same way.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise FetchError("Only https URLs are fetched.")

    host = parsed.hostname
    if not host:
        raise FetchError("That URL has no host.")

    try:
        addresses = {
            info[4][0] for info in socket.getaddrinfo(host, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise FetchError(f"Could not look up {host}.") from exc

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global or ip.is_multicast:
            raise FetchError(
                f"{host} resolves to {ip}, which is on this machine or this "
                f"network. Only public addresses are fetched."
            )

    return HOSTS.get(parsed.netloc, "direct")


def _open(url: str, token: str | None, service: str) -> Any:
    """A GET with the right auth header, refusing a redirect off the allowlist."""
    _check_host(url)
    request = urllib.request.Request(url)
    if token:
        # In a header, never in the URL: a URL reaches logs and history.
        request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", "transcript-viewer")
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            where = "Hugging Face" if service == "hf" else "GitHub"
            raise FetchError(
                f"{where} refused that ({exc.code}). "
                f"A gated dataset needs a token in Settings, and its conditions "
                f"accepted on its page first."
            ) from exc
        if exc.code == 404:
            raise FetchError("Nothing there — check the URL.") from exc
        raise FetchError(f"{urlparse(url).netloc} said {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"Could not reach {urlparse(url).netloc}.") from exc

    # urllib follows redirects itself, so verify where it actually landed.
    _check_host(response.geturl())
    return response


def _json(url: str, token: str | None, service: str) -> Any:
    with _open(url, token, service) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------------- s3 ---


def _aws(args: list[str], profile: str, json_out: bool = True) -> str:
    """Run the aws CLI and return its stdout.

    No shell, and the profile is checked before it reaches the argument list.
    An expired session is reported rather than renewed: logging in is an
    interactive, browser-based act that belongs to the person at the keyboard.
    """
    if shutil.which("aws") is None:
        raise FetchError(
            "The aws CLI is not installed, and S3 is read through it. "
            "See https://aws.amazon.com/cli/"
        )
    if profile and not AWS_PROFILE.match(profile):
        raise FetchError(f"{profile!r} is not a usable AWS profile name.")

    command = ["aws", *args]
    if json_out:
        command += ["--output", "json"]
    if profile:
        command += ["--profile", profile]

    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=AWS_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FetchError(f"Could not run the aws CLI: {exc}") from exc

    if done.returncode != 0:
        lines = (done.stderr or done.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit {done.returncode}"
        # The CLI reports a missing session several ways — "Token has expired",
        # "Unable to locate credentials", "NoCredentials" — and its own advice
        # ("run aws login") is not the SSO command. Say the one that works.
        lowered = detail.lower()
        if any(word in lowered for word in ("expired", "sso", "credential", "token")):
            named = profile or "<your-profile>"
            raise FetchError(
                "No usable AWS session. In a terminal, run: "
                f"aws sso login --profile {named}"
            )
        raise FetchError(detail.removeprefix("aws: ").strip())
    return done.stdout


def _profiles() -> list[str]:
    """Profile names the aws CLI knows about."""
    if shutil.which("aws") is None:
        return []
    try:
        done = subprocess.run(
            ["aws", "configure", "list-profiles"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def resolve_profile(configured: str) -> str:
    """Which AWS profile to use, asking as little as possible.

    Settings first, then AWS_PROFILE, then — if the machine has exactly one
    profile — that one, because there is nothing to disambiguate. Naming a
    profile should be something only people with several ever have to do.
    """
    if configured:
        return configured
    from_env = os.environ.get("AWS_PROFILE", "").strip()
    if from_env and AWS_PROFILE.match(from_env):
        return from_env
    known = [p for p in _profiles() if AWS_PROFILE.match(p)]
    return known[0] if len(known) == 1 else ""


def _s3_plan(url: str, profile: str) -> tuple[str, list[Remote], str]:
    rest = url[len("s3://") :].strip("/")
    bucket, _, prefix = rest.partition("/")
    if not S3_BUCKET.match(bucket):
        raise FetchError(f"{bucket!r} is not a valid S3 bucket name.")
    if not S3_PREFIX.match(prefix):
        raise FetchError("That key prefix has characters this will not pass to the CLI.")

    # Capped rather than exhaustive: a bucket can hold six figures of objects,
    # and pulling the whole listing to then refuse it wastes everyone's time.
    args = ["s3api", "list-objects-v2", "--bucket", bucket,
            "--max-items", str(MAX_FILES + 1)]
    if prefix:
        args += ["--prefix", prefix]
    listing = json.loads(_aws(args, profile) or "{}")

    cut = len(prefix.rstrip("/")) + 1 if prefix else 0
    files = [
        Remote(
            name=str(row["Key"])[cut:].lstrip("/") or Path(str(row["Key"])).name,
            url=f"s3://{bucket}/{row['Key']}",
            size=row.get("Size"),
            etag=(str(row.get("ETag") or "")).strip('"') or None,
            modified=row.get("LastModified"),
        )
        for row in listing.get("Contents", [])
        if str(row.get("Key", "")).endswith(SUFFIXES)
    ]
    return bucket, files, f"s3://{bucket}/{prefix}".rstrip("/")


def _s3_download(remote: Remote, target: Path, profile: str) -> None:
    """One object straight to disk; the CLI streams it, so nothing is buffered."""
    _aws(
        ["s3", "cp", remote.url, str(target), "--only-show-errors"],
        profile,
        json_out=False,
    )


# ------------------------------------------------------------- hugging face ---

_HF = re.compile(
    r"^/(?:(?P<kind>datasets|spaces)/)?(?P<repo>[^/]+/[^/]+)"
    r"(?:/(?P<action>tree|blob|resolve)/(?P<rev>[^/]+)(?P<path>/.*)?)?/?$"
)


def _hf_plan(url: str, token: str | None) -> tuple[str, list[Remote], str]:
    parsed = urlparse(url)
    match = _HF.match(parsed.path)
    if not match:
        raise FetchError("That does not look like a Hugging Face repo or file URL.")

    repo = match.group("repo")
    kind = match.group("kind") or "models"
    rev = match.group("rev") or "main"
    inner = (match.group("path") or "").strip("/")
    api_kind = {"datasets": "datasets", "spaces": "spaces", "models": "models"}[kind]
    prefix = f"{kind}/" if match.group("kind") else ""

    def raw(path: str) -> str:
        return f"https://huggingface.co/{prefix}{repo}/resolve/{rev}/{quote(path)}"

    web = f"https://huggingface.co/{prefix}{repo}/tree/{rev}"
    if match.group("action") == "blob" or (inner and inner.endswith(SUFFIXES)):
        return repo.replace("/", "--"), [
            Remote(name=inner.split("/")[-1], url=raw(inner))
        ], web

    listing = _json(
        f"https://huggingface.co/api/{api_kind}/{repo}/tree/{rev}/{quote(inner)}"
        f"?recursive=true",
        token,
        "hf",
    )
    files = [
        Remote(name=row["path"], url=raw(row["path"]), size=row.get("size"))
        for row in listing
        if row.get("type") == "file" and str(row.get("path", "")).endswith(SUFFIXES)
    ]
    return repo.replace("/", "--"), files, web


# ------------------------------------------------------------------ github ---

_GH = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"(?:/(?P<action>tree|blob|raw)/(?P<rev>[^/]+)(?P<path>/.*)?)?/?$"
)


def _github_plan(url: str, token: str | None) -> tuple[str, list[Remote], str]:
    parsed = urlparse(url)

    if parsed.netloc == "raw.githubusercontent.com":
        name = parsed.path.strip("/").split("/")[-1]
        return "github", [Remote(name=name, url=url)], ""

    match = _GH.match(parsed.path)
    if not match:
        raise FetchError("That does not look like a GitHub repo or file URL.")

    owner, repo = match.group("owner"), match.group("repo").removesuffix(".git")
    rev = match.group("rev")
    inner = (match.group("path") or "").strip("/")

    if rev is None:
        info = _json(f"https://api.github.com/repos/{owner}/{repo}", token, "github")
        rev = info.get("default_branch") or "main"

    def raw(path: str) -> str:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rev}/{quote(path)}"

    web = f"https://github.com/{owner}/{repo}/tree/{rev}"
    if match.group("action") in ("blob", "raw"):
        return f"{owner}--{repo}", [
            Remote(name=inner.split("/")[-1], url=raw(inner))
        ], web

    tree = _json(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{rev}?recursive=1",
        token,
        "github",
    )
    if tree.get("truncated"):
        raise FetchError(
            "That repository is too large to list in one request. "
            "Link a subdirectory instead."
        )
    files = [
        Remote(name=row["path"], url=raw(row["path"]), size=row.get("size"))
        for row in tree.get("tree", [])
        if row.get("type") == "blob"
        and str(row.get("path", "")).endswith(SUFFIXES)
        and (not inner or str(row.get("path", "")).startswith(f"{inner}/"))
    ]
    return f"{owner}--{repo}", files, web


# ---------------------------------------------------------------- browsing ---


def browse(url: str, inside: str, tokens: dict[str, str | None]) -> list[Node]:
    """One level of a remote location.

    A level at a time rather than the whole thing: the bucket this was built for
    holds 118,801 objects, and listing all of them to draw a picker would be
    slower and larger than most of the downloads it is meant to avoid.
    """
    url = (url or "").strip()
    inside = (inside or "").strip("/")

    if url.startswith("s3://"):
        return _s3_browse(url, inside, resolve_profile(tokens.get("aws") or ""))

    service = _check_host(url)
    if service == "hf":
        return _hf_browse(url, inside, tokens.get("hf"))
    if service == "github":
        return _github_browse(url, inside, tokens.get("github"))
    raise FetchError("That is a single file, so there is nothing to look inside.")


def _s3_browse(url: str, inside: str, profile: str) -> list[Node]:
    bucket, _, base = url[len("s3://") :].strip("/").partition("/")
    if not S3_BUCKET.match(bucket):
        raise FetchError(f"{bucket!r} is not a valid S3 bucket name.")
    prefix = "/".join(p for p in (base, inside) if p)
    if prefix:
        prefix += "/"
    if not S3_PREFIX.match(prefix):
        raise FetchError("That key prefix has characters this will not pass to the CLI.")

    args = ["s3api", "list-objects-v2", "--bucket", bucket, "--delimiter", "/",
            "--max-items", str(BROWSE_LIMIT)]
    if prefix:
        args += ["--prefix", prefix]
    listing = json.loads(_aws(args, profile) or "{}")

    nodes = [
        Node(
            name=row["Prefix"][len(prefix):].strip("/"),
            path=f"{inside}/{row['Prefix'][len(prefix):]}".strip("/"),
            kind="folder",
            size=None,
        )
        for row in listing.get("CommonPrefixes", [])
    ]
    nodes += [
        Node(
            name=row["Key"][len(prefix):],
            path=f"{inside}/{row['Key'][len(prefix):]}".strip("/"),
            kind="file",
            size=row.get("Size"),
        )
        for row in listing.get("Contents", [])
        if row["Key"] != prefix and str(row.get("Key", "")).endswith(SUFFIXES)
    ]
    return nodes


def _hf_browse(url: str, inside: str, token: str | None) -> list[Node]:
    parsed = urlparse(url)
    match = _HF.match(parsed.path)
    if not match:
        raise FetchError("That does not look like a Hugging Face repo URL.")
    repo = match.group("repo")
    kind = match.group("kind") or "models"
    rev = match.group("rev") or "main"
    base = (match.group("path") or "").strip("/")
    where = "/".join(p for p in (base, inside) if p)

    rows = _json(
        f"https://huggingface.co/api/{kind if match.group('kind') else 'models'}"
        f"/{repo}/tree/{rev}/{quote(where)}",
        token,
        "hf",
    )
    return _level(rows, "type", "directory", "path", where, inside, "size")


def _github_browse(url: str, inside: str, token: str | None) -> list[Node]:
    parsed = urlparse(url)
    match = _GH.match(parsed.path)
    if not match:
        raise FetchError("That does not look like a GitHub repo URL.")
    owner, repo = match.group("owner"), match.group("repo").removesuffix(".git")
    rev = match.group("rev")
    base = (match.group("path") or "").strip("/")
    if rev is None:
        info = _json(f"https://api.github.com/repos/{owner}/{repo}", token, "github")
        rev = info.get("default_branch") or "main"
    where = "/".join(p for p in (base, inside) if p)

    rows = _json(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(where)}?ref={rev}",
        token,
        "github",
    )
    if not isinstance(rows, list):
        raise FetchError("That path is a file, not a folder.")
    return _level(rows, "type", "dir", "path", where, inside, "size")


def _level(rows, type_key, folder_word, path_key, where, inside, size_key) -> list[Node]:
    """Turn one API listing into nodes, keeping only what is worth fetching."""
    nodes: list[Node] = []
    for row in rows:
        full = str(row.get(path_key, ""))
        name = full[len(where):].strip("/") if where else full
        if not name:
            continue
        here = f"{inside}/{name}".strip("/")
        if row.get(type_key) == folder_word:
            nodes.append(Node(name=name, path=here, kind="folder", size=None))
        elif full.endswith(SUFFIXES):
            nodes.append(
                Node(name=name, path=here, kind="file", size=row.get(size_key))
            )
    return nodes


# -------------------------------------------------------------------- api ----


def destination(raw: str | None) -> Path:
    """Resolve where a download should land.

    The path comes from the page, so it is resolved before it is trusted: `..`
    and a symlink both collapse here, and a handful of places nothing should
    ever be written into are refused outright.
    """
    text = (raw or "").strip() or DEFAULT_DIR
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    home = Path.home().resolve()
    if (
        path == Path(path.anchor)
        or path == home
        or path in home.parents
        or any(path == Path(p) or Path(p) in path.parents for p in PROTECTED)
    ):
        raise FetchError(f"{path} is not somewhere to download into — pick a folder.")
    return path


def _sized(plan: "Plan") -> "Plan":
    """Refuse a plan that is too big before a byte of it is downloaded."""
    if not plan.files:
        raise FetchError(
            "Nothing convertible there — looking for "
            + ", ".join(LOGS)
            + " files, or an archive of them."
        )
    if len(plan.files) > MAX_FILES:
        where = "a prefix" if plan.service == "s3" else "a subdirectory"
        raise FetchError(
            f"{len(plan.files):,}+ files is more than this fetches at once "
            f"(limit {MAX_FILES:,}). Point at {where}."
        )
    known = sum(f.size or 0 for f in plan.files)
    if known > MAX_TOTAL_BYTES:
        raise FetchError(
            f"That is {known / 1024**3:.1f} GB, over the "
            f"{MAX_TOTAL_BYTES // 1024**3} GB limit for one fetch. "
            f"Pick fewer folders, or take it in parts."
        )
    return plan


def _under(name: str, wanted: set[str]) -> bool:
    """Whether a file was ticked, directly or by one of its folders."""
    name = name.strip("/")
    return name in wanted or any(name.startswith(w + "/") for w in wanted)


def _inner_base(url: str) -> str:
    """The folder a repository URL already points at.

    Browse paths are relative to that folder, while a plan names files relative
    to the repository root — so one has to be rebased onto the other or a ticked
    folder matches nothing. S3 needs none of this: both are relative to the
    prefix.
    """
    if url.startswith("s3://"):
        return ""
    parsed = urlparse(url)
    pattern = _HF if HOSTS.get(parsed.netloc) == "hf" else _GH
    match = pattern.match(parsed.path)
    return (match.group("path") or "").strip("/") if match else ""


def _rebase(url: str, paths: list[str]) -> set[str]:
    base = _inner_base(url)
    return {
        "/".join(p for p in (base, path.strip("/")) if p)
        for path in paths
        if path and path.strip("/")
    }


def _s3_under(url: str, paths: set[str], profile: str) -> tuple[list[Remote], bool]:
    """Every object beneath the ticked paths, and whether a listing was capped.

    Listed per ticked path rather than filtered out of a listing of the whole
    location. The bucket this was built for holds 118,801 objects and a listing
    is capped, so anything alphabetically past the cap — "screwtape" is, "chippy"
    is not — was simply absent and a selection resolved to nothing.
    """
    bucket, _, base = url[len("s3://") :].strip("/").partition("/")
    if not S3_BUCKET.match(bucket):
        raise FetchError(f"{bucket!r} is not a valid S3 bucket name.")

    cut = len(base.rstrip("/")) + 1 if base else 0
    found: dict[str, Remote] = {}
    capped = False
    for path in sorted(paths):
        prefix = "/".join(p for p in (base, path) if p)
        if not S3_PREFIX.match(prefix):
            raise FetchError("That prefix has characters this will not pass on.")
        args = ["s3api", "list-objects-v2", "--bucket", bucket,
                "--max-items", str(MAX_FILES + 1)]
        if prefix:
            args += ["--prefix", prefix]
        rows = json.loads(_aws(args, profile) or "{}").get("Contents", [])
        capped = capped or len(rows) > MAX_FILES
        for row in rows:
            key = str(row.get("Key", ""))
            if not key.endswith(SUFFIXES):
                continue
            # A ticked folder and a ticked file inside it must not count twice.
            found[key] = Remote(
                name=key[cut:] or Path(key).name,
                url=f"s3://{bucket}/{key}",
                size=row.get("Size"),
                etag=(str(row.get("ETag") or "")).strip('"') or None,
                modified=row.get("LastModified"),
            )
    return list(found.values()), capped


def measure(url: str, paths: list[str], tokens: dict[str, str | None]) -> dict:
    """Exactly how much a selection is, by listing inside what was ticked.

    A tick on a folder is a promise about its contents, and "1+ files" is not an
    answer to "how much am I getting?". This costs one listing per folder, which
    is the price of a real number.
    """
    wanted = {p.strip("/") for p in paths if p and p.strip("/")}
    if not wanted:
        return {"files": 0, "bytes": 0, "capped": False}

    if url.startswith("s3://"):
        found, capped = _s3_under(
            url, wanted, resolve_profile(tokens.get("aws") or "")
        )
        return {
            "files": len(found),
            "bytes": sum(f.size or 0 for f in found),
            "capped": capped,
        }

    whole = plan(url, tokens, sized=False)
    against = _rebase(url, list(wanted))
    keep = [f for f in whole.files if _under(f.name, against)]
    return {
        "files": len(keep),
        "bytes": sum(f.size or 0 for f in keep),
        "capped": False,
    }


def select(url: str, paths: list[str], tokens: dict[str, str | None]) -> Plan:
    """A plan covering only what was ticked.

    A ticked folder means everything under it, so folders are expanded here
    rather than in the page: the page would have to walk the remote to find out
    what it had just agreed to, which is the work this avoids.
    """
    picked = {p.strip("/") for p in paths if p and p.strip("/")}
    if not picked:
        raise FetchError("Nothing was ticked.")

    if url.startswith("s3://"):
        bucket = url[len("s3://") :].strip("/").partition("/")[0]
        found, _ = _s3_under(url, picked, resolve_profile(tokens.get("aws") or ""))
        base = url[len("s3://") :].strip("/").partition("/")[2]
        return _sized(
            Plan("s3", bucket, found, f"s3://{bucket}/{base}".rstrip("/"))
        )

    whole = plan(url, tokens, sized=False)
    wanted = _rebase(url, paths)
    chosen = [f for f in whole.files if _under(f.name, wanted)]
    return _sized(Plan(whole.service, whole.label, chosen, whole.web))


def plan(url: str, tokens: dict[str, str | None], sized: bool = True) -> Plan:
    """What fetching this URL would pull: the service, a label, and the files."""
    url = (url or "").strip()
    if not url:
        raise FetchError("No URL given.")

    if url.startswith("s3://"):
        bucket, files, where = _s3_plan(url, resolve_profile(tokens.get("aws") or ""))
        made = Plan("s3", bucket, files, where)
        return _sized(made) if sized else made

    service = _check_host(url)
    token = tokens.get(service)

    if service == "direct":
        name = Path(urlparse(url).path).name or "download.jsonl"
        if not name.endswith(SUFFIXES):
            raise FetchError(
                f"{name} is not a kind this reads — looking for "
                f"{', '.join(LOGS)} or an archive of them."
            )
        label, files, web = urlparse(url).hostname or "download", [
            Remote(name=name, url=url)
        ], ""
    else:
        label, files, web = (_hf_plan if service == "hf" else _github_plan)(url, token)
    made = Plan(service, label, files, web)
    return _sized(made) if sized else made


def download(
    service: str, files: list[Remote], into: Path, tokens: dict[str, str | None]
) -> Iterator[Path]:
    """Fetch each file under ``into``, yielding each as it lands.

    A generator so a caller can report progress: a dataset of several hundred
    files takes long enough that a still screen reads as a hang.
    """
    token = tokens.get(service)
    total = 0

    for remote in files:
        # The name comes from a remote listing, so it is not trusted to stay
        # inside the directory.
        parts = [p for p in Path(remote.name).parts if p not in ("..", "/", "")]
        if not parts:
            continue
        target = into.joinpath(*parts)
        if not target.resolve().is_relative_to(into.resolve()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)

        # Already here, at the size the listing gives. Agent logs are written
        # once and not edited, so for the case this exists to serve — asking a
        # bucket again for what is new — name and size agreeing is enough, and
        # the alternative is downloading gigabytes to arrive at the same files.
        # Not yielded: the count is what was fetched, and the scan afterwards
        # picks up everything on disk either way.
        if remote.size is not None and target.exists():
            if target.stat().st_size == remote.size:
                continue

        if service == "s3":
            _s3_download(remote, target, resolve_profile(tokens.get("aws") or ""))
            yield target
            continue

        with _open(remote.url, token, service) as response:
            body = response.read(MAX_TOTAL_BYTES - total + 1)
        total += len(body)
        if total > MAX_TOTAL_BYTES:
            raise FetchError("That is larger than the fetch limit.")
        target.write_bytes(body)
        yield target
