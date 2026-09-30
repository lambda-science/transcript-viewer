"""Corpus indexing.

The point of an index is to answer "what sessions do I have, across every agent"
without paying to convert them. Scanning reads only a file's leading lines, so a
143 MB rollout costs the same as a 4 KB one; full conversion happens lazily, when
something actually asks for that trajectory.
"""

from __future__ import annotations

import hashlib
from collections import Counter
import re
import json
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path

from atif_make.container import is_container, open_container

from . import store
from atif_make.convert import AGENTS
from atif_make.detect import detect_format, head_lines

INDEX_PATH = store.ROOT / "index.json"
# Where the viewer keeps files brought in by hand. Anything here is "opened"
# whoever asks, so a plain rescan classifies them correctly on its own.
OPENED_ROOT = store.ROOT / "opened"

# Where a collection's pieces are kept. A benchmark published as one JSON array
# becomes a transcript per file, and those files are what the index records, so
# they need somewhere that outlives the process that made them. Keyed by the
# source's content, so re-indexing the same download reuses the split and a
# changed file gets a new one.
SPLIT_ROOT = store.ROOT / "split"

# A first line shorter than this is structural rather than identifying — a
# pretty-printed JSON document opens with a bare "{".


def content_key(path: Path) -> str:
    """A stable identity for a log file: the hash of everything in it.

    Prefix hashing was tried first and does not hold. It has to assume the
    opening of a file identifies it, which is true of session logs — they carry
    a session id on line one — and false of curated corpora: SLEIGHT-Bench opens
    every transcript with the same canary, and pairs each attack with a benign
    twin that follows the same script until it diverges. Even nine leading lines
    separated only 78 of its 86 files, so the whole corpus collapsed into one
    index row.

    Hashing everything cannot collide, and makes a genuine duplicate — the same
    transcript downloaded twice — collapse to one row, which is the behaviour
    worth having. It costs little: 1.26 GB of real corpus hashes in half a
    second.

    The price is that a session still being written re-keys as it grows, so an
    annotation made mid-session can detach from it. `merge` keeps that from
    doubling the row; the detaching is accepted rather than engineered around,
    because annotating a transcript while it is still being written is rare and
    the machinery to follow it would cost more than it saves.
    """
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        # Fall back to the path so an unreadable file still gets *an* identity.
        return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


@dataclass(slots=True)
class Entry:
    key: str
    origin: str  # "scanned" (found on this machine) | "opened" (brought in)
    path: str
    format: str
    agent: str
    session_id: str | None
    project: str | None
    modified: str
    size_bytes: int
    subagents: int
    # What the agent called this session, if it named one. Not the reader's
    # title — that is theirs, and lives in the library.
    session_title: str | None = None


def _session_id(fmt: str, lines: list[dict]) -> str | None:
    for line in lines:
        if fmt == "claude-code-transcript" and isinstance(line.get("sessionId"), str):
            return line["sessionId"]
        if fmt == "claude-code-stream" and isinstance(line.get("session_id"), str):
            return line["session_id"]
        if fmt == "codex-rollout" and line.get("type") == "session_meta":
            payload = line.get("payload")
            if isinstance(payload, dict):
                return payload.get("session_id")
        if fmt == "codex-exec" and isinstance(line.get("thread_id"), str):
            return line["thread_id"]
    return None


def _escape(name: str) -> str:
    """A directory name as Claude Code would write it into a slug."""
    return re.sub(r"[^A-Za-z0-9]+", "-", name)


def _decode_cwd(slug: str) -> str:
    """Rebuild a working directory from Claude Code's escaped slug.

    The escaping replaces every run of non-alphanumeric characters with "-", so
    "/", " " and "_" all become the same thing: "Build An LLM" and
    "Build/An/LLM" escape identically, and neither can be recovered by reading
    the slug.

    So read the filesystem instead. At each level, look for a real child whose
    own escaped name opens what is left of the slug, preferring the longest —
    which is what tells "atif-make" apart from "atif" followed by "make".
    Whatever cannot be matched is taken literally, which is the best guess
    available for a directory since deleted.
    """
    rest = slug.lstrip("-")
    here = Path("/")
    while rest:
        try:
            children = sorted(here.iterdir(), key=lambda c: -len(c.name))
        except OSError:
            children = []

        for child in children:
            # Trimmed at both ends: a name like "s_" escapes to "s-", and the
            # separator that follows it in the slug is the same "-".
            escaped = _escape(child.name).strip("-")
            if escaped and (rest == escaped or rest.startswith(escaped + "-")):
                here, rest = child, rest[len(escaped):].lstrip("-")
                break
        else:
            # Nothing here matches, so the rest never existed or is long gone.
            return str(here / rest.replace("-", "/")) if rest else str(here)
    return str(here)


def _project(path: Path, fmt: str) -> str | None:
    if fmt.startswith("claude-code"):
        # ~/.claude/projects/<slug>/<session>.jsonl — the slug is the cwd, escaped.
        slug = path.parent.name
        return _decode_cwd(slug) if slug.startswith("-") else slug
    if fmt.startswith("codex"):
        for line in head_lines(path, 5):
            payload = line.get("payload")
            if isinstance(payload, dict) and payload.get("cwd"):
                return payload["cwd"]
    return None


# The name is written again on every turn, so the newest sits near the end.
# Reading a tail costs nothing; reading a 14 MB transcript to find it would.
TITLE_TAIL = 256 * 1024
TITLE_HEAD = 64 * 1024


def _session_title(path: Path, fmt: str) -> str | None:
    """The name Claude Code gave a session, newest first.

    Falls back to the head, where the first name it chose lives, so a session
    whose last turn was enormous still shows something rather than a path.
    """
    if not fmt.startswith("claude-code"):
        return None
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TITLE_TAIL))
            tail = handle.read()
            if size > TITLE_TAIL:
                tail = tail.split(b"\n", 1)[-1]  # a half line helps nobody
            handle.seek(0)
            head = handle.read(TITLE_HEAD)
    except OSError:
        return None

    for chunk, newest_last in ((tail, True), (head, False)):
        found = None
        for line in chunk.splitlines():
            if b'"ai-title"' not in line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(row, dict) and row.get("aiTitle"):
                found = str(row["aiTitle"])
                if not newest_last:
                    return found
        if found:
            return found
    return None


def describe(path: Path, origin: str = "scanned") -> Entry | None:
    """Build an index entry for one file, or None if it is not convertible."""
    try:
        fmt = detect_format(path)
    except (ValueError, OSError):
        return None
    stat = path.stat()
    if OPENED_ROOT in path.parents:
        origin = "opened"
    lines = head_lines(path, 20) if fmt not in ("har", "atif") else []
    sub_dir = path.parent / path.stem / "subagents"
    return Entry(
        key=content_key(path),
        origin=origin,
        path=str(path),
        format=fmt,
        agent=AGENTS.get(fmt, "unknown"),
        session_id=_session_id(fmt, lines),
        project=_project(path, fmt),
        modified=datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        size_bytes=stat.st_size,
        subagents=len(list(sub_dir.glob("*.jsonl"))) if sub_dir.is_dir() else 0,
        session_title=_session_title(path, fmt),
    )


def merge(existing: list[Entry], new: list[Entry]) -> list[Entry]:
    """Combine index entries, newest first.

    One row per file, and one row per identity. A path has a single current
    entry, so a file that grew replaces its own older row rather than adding
    one. Identical content in two places is one document, and the copy that is
    actually on disk wins.
    """
    by_path: dict[str, Entry] = {}
    for entry in [*existing, *new]:
        by_path[entry.path] = entry

    grouped: dict[str, list[Entry]] = {}
    for entry in by_path.values():
        grouped.setdefault(entry.key, []).append(entry)

    # Of the places one identity lives, the row goes to the copy that is on
    # disk, and to the most recent of those.
    out = [
        max(group, key=lambda e: (Path(e.path).exists(), e.modified))
        for group in grouped.values()
    ]
    return sorted(out, key=lambda e: e.modified, reverse=True)


def _split_home(path: Path) -> Path:
    """Where this file's pieces belong, if it turns out to hold many.

    A download is unpacked beside itself on arrival, so prefer that: the pieces
    are already there, they sit with the thing they came from, and they go when
    the folder does. Anything else — a file being scanned where writing beside
    it would be rude — goes under the viewer's own directory instead.
    """
    beside = path.with_suffix("")
    if beside.is_dir() and any(beside.iterdir()):
        return beside
    return SPLIT_ROOT / content_key(path)[:16]


def scan(roots: list[Path], origin: str = "scanned") -> list[Entry]:
    """Find every convertible log under ``roots``. A root may be a single file."""
    entries: list[Entry] = []
    seen: set[Path] = set()

    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            # A zip, a tarball, or a benchmark published as one JSON array of a
            # thousand runs: a container of logs, not a log.
            if is_container(root):
                try:
                    root = open_container(root, _split_home(root))
                except (ValueError, OSError):
                    continue
            else:
                entry = describe(root, origin)
                if entry:
                    entries.append(entry)
                continue
        # A directory of archives is as ordinary as a directory of logs — a
        # bucket of agent runs is mostly zips — so look inside them too. Each is
        # unpacked once and its contents scanned in place.
        roots_here = [root]
        for item in sorted(root.rglob("*")):
            if not item.is_file() or not is_container(item):
                continue
            try:
                roots_here.append(open_container(item, _split_home(item)))
            except (ValueError, OSError):
                continue

        candidates = []
        for where in roots_here:
            candidates += (
                sorted(where.rglob("*.jsonl"))
                + sorted(where.rglob("*.har"))
                + sorted(where.rglob("*.json"))
                + sorted(where.rglob("*.md"))
                # Not a log, but a container of them, opened above.
                + sorted(where.rglob("*.eval"))
            )
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            # A container was opened above and its contents are already in this
            # list. Indexing it as well would offer a session that cannot be
            # opened, since it holds a thousand transcripts rather than one.
            if is_container(path):
                continue
            # Subagent traces are reached through their parent, not indexed alone.
            if path.parent.name == "subagents":
                continue
            seen.add(resolved)
            entry = describe(path, origin)
            if entry:
                entries.append(entry)

    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


def save(entries: list[Entry], path: Path | None = None) -> Path:
    # Resolved on call, not bound as a default: a default freezes at import and
    # cannot be redirected, which let a test write to the real index.
    path = path or INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) for e in entries], indent=2))
    return path


def load(path: Path | None = None) -> list[Entry]:
    """Read the index, tolerating rows written by an older version."""
    path = path or INDEX_PATH
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    fields = {f.name for f in dataclass_fields(Entry)}
    entries = []
    for row in rows:
        if not isinstance(row, dict) or "path" not in row:
            continue
        known = {k: v for k, v in row.items() if k in fields}
        # An index written before content keys existed has neither field.
        known.setdefault("key", content_key(Path(row["path"])))
        known.setdefault("origin", "scanned")
        try:
            entries.append(Entry(**known))
        except TypeError:
            continue
    return entries


def describe_contents(root: Path, limit: int = 4000) -> str:
    """What is inside a path, said in a sentence.

    Reached only when nothing in it converted. "Nothing convertible" on its own
    invites the reader to doubt the tool rather than the file — one real archive
    held 1,952 files and an audit that referenced a transcript which had not
    been shipped with it, and the answer gave no way to tell that from a bug.
    """
    if root.is_file():
        return f"{root.name} ({root.stat().st_size:,} bytes)"

    kinds: Counter[str] = Counter()
    total = 0
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        total += 1
        if total > limit:
            break
        kinds[item.suffix.lower() or "no extension"] += 1

    if not total:
        return "it is empty"

    common = ", ".join(f"{n:,} {ext}" for ext, n in kinds.most_common(4))
    more = "+" if total > limit else ""
    return f"{total:,}{more} files inside — {common}"
