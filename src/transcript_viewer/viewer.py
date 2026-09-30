"""Local trajectory viewer.

Serves a single self-contained page on loopback. Trajectories are converted on
demand and cached in memory, so opening the viewer over a large corpus is cheap
and only the sessions actually opened pay conversion cost.

The server binds 127.0.0.1 only — it reads local session logs, which routinely
contain source code and credentials in tool output, and must never be reachable
off-host.
"""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from atif_make.atif import ContentPart, Trajectory
from atif_make.container import is_container, open_container
from atif_make.convert import convert
from atif_make.detect import FORMATS

from transcript_viewer.corpus import Entry, scan

from . import ai, config, corpus, deploy, fetch, identity, ingest, library
from . import cache as cachelib

# The page is a real .html file rather than a string in here: it is 2,232 lines
# of CSS and JavaScript, which is not Python and should not be typed as though
# it were. Read once at import, and served from memory.
PAGE_PATH = Path(__file__).parent / "page.html"

# What was read, and the state of the file it was read from.
_page: tuple[float, int, str] | None = None


def page() -> str:
    """The interface, re-read when the file behind it changes.

    The whole interface is that one file, so editing it and seeing nothing
    change until the server is restarted reads as the edit not having worked.
    Twice now it has. The cost is one stat per page load — not per request, and
    not on anything the page then calls — and for an installed copy, where the
    file never changes, it is a stat that always says the same thing.
    """
    global _page
    try:
        info = PAGE_PATH.stat()
        stamp = (info.st_mtime, info.st_size)
    except OSError:
        # If it cannot be stat'd, what was already read is better than nothing.
        if _page is not None:
            return _page[2]
        raise
    if _page is None or (_page[0], _page[1]) != stamp:
        _page = (stamp[0], stamp[1], PAGE_PATH.read_text(encoding="utf-8"))
    return _page[2]


def _point_images_at_server(trajectory: Trajectory, index: str) -> None:
    """Rewrite relative image paths to a URL this server can answer.

    On disk an image path resolves next to the trajectory file, but nothing is
    written to disk here — the bytes are in memory, so they need an endpoint.
    """

    def parts(value):
        return (
            [p for p in value if isinstance(p, ContentPart)]
            if isinstance(value, list)
            else []
        )

    def walk(t: Trajectory) -> None:
        for step in t.steps:
            targets = list(parts(step.message))
            for result in step.observation.results if step.observation else ():
                targets += parts(result.content)
            for part in targets:
                if (
                    part.type == "image"
                    and part.source
                    and not part.source.path.startswith(
                        ("http://", "https://", "data:")
                    )
                ):
                    name = part.source.path.rsplit("/", 1)[-1]
                    part.source.path = f"/api/image?id={index}&name={name}"
        for sub in t.subagent_trajectories or ():
            walk(sub)

    walk(trajectory)


def _reveal(target: Path) -> bool:
    """Show a path in the OS file manager.

    Reveal-only, never open: `open -R` selects the item in Finder rather than
    launching whatever application is registered for it, so clicking a path in
    a log cannot execute anything.
    """
    if not target.exists():
        return False
    if sys.platform == "darwin":
        command = ["open", "-R", str(target)]
    elif sys.platform.startswith("linux") and shutil.which("xdg-open"):
        # xdg-open has no reveal equivalent, so open the containing directory.
        command = ["xdg-open", str(target if target.is_dir() else target.parent)]
    else:
        return False
    try:
        subprocess.run(command, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# Enough of a log to inspect its shape without shipping a 143 MB file.
RAW_LIMIT = 512 * 1024

# A browser upload crosses loopback, so this is generous; it exists to stop a
# stray multi-gigabyte archive from filling the disk, not to be restrictive.
UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024

# Where opened files live. atif-make owns the location because it also has to
# recognise one during a scan; a second constant here could drift from it.


def _groups(rows: list[dict]) -> list[str]:
    """Every node of the tree, implied parents included, in reading order."""
    seen: set[str] = set()
    for row in rows:
        parts = [p for p in row["group"].split("/") if p]
        for depth in range(1, len(parts) + 1):
            seen.add("/".join(parts[:depth]))
    # Local before Remote, then alphabetically within each.
    return sorted(seen, key=lambda path: (not path.startswith("Local"), path))


SITES = {"huggingface.co": "Hugging Face", "github.com": "GitHub"}
AGENTS = {"claude-code": "Claude Code", "codex": "Codex", "copilot": "Copilot"}


def _group(entry, source: str) -> str:
    """Where a session belongs in the tree.

    Derived, never stored. Collections were a second way to organise laid over
    one that already existed — where a transcript came from — and the two could
    disagree. This reads the truth instead: local sessions under the project
    that produced them, remote ones under the repository they came from. Tags
    remain for grouping that cuts across both.
    """
    if entry.origin == "fetched":
        if not source:
            return "Remote/Elsewhere"
        if source.startswith("s3://"):
            rest = source[len("s3://") :].strip("/")
            return "/".join(["Remote", "S3", *[p for p in rest.split("/") if p]])
        parsed = urlparse(source)
        site = SITES.get(parsed.netloc, parsed.netloc or "Elsewhere")
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc == "huggingface.co" and parts[:1] == ["datasets"]:
            parts = parts[1:]
        # <owner>/<repo>/tree/<rev>/<inner…>
        inner = (
            parts[4:] if len(parts) > 4 and parts[2:3] in (["tree"], ["blob"]) else []
        )
        return "/".join(["Remote", site, *parts[:2], *inner])

    # Local mirrors Remote: the thing that produced it, then the unit of work.
    agent = AGENTS.get(entry.agent, entry.agent or "Unknown")
    if entry.origin == "opened":
        return f"Local/Opened/{agent}"

    # A project is a directory path; only its own name belongs in the tree, or
    # every session would nest one node deep per directory above it.
    project = (
        Path(entry.project).name if entry.project else Path(entry.path).parent.name
    )
    return f"Local/{agent}/{project or 'Elsewhere'}"


def _source_of(entry, home: Path, web: str, unpacked: dict | None = None) -> str:
    """Where a fetched session came from, folder and all.

    A file that arrived inside an archive is placed by the archive, not by the
    directories the archive happens to contain: a zip whose insides are called
    "transcripts/transcripts" should not read that way in the tree.
    """
    if not web:
        return ""
    here = Path(entry.path).parent
    for root, came_from in (unpacked or {}).items():
        if here == root or root in here.parents:
            here = came_from
            break
    try:
        inner = here.relative_to(home).as_posix()
    except ValueError:
        inner = ""
    return web if inner in (".", "") else f"{web}/{inner}"


def _ai_state() -> dict:
    """What the page may know about credentials: whether, and from where.

    Never the key itself — only a four-character tail, enough to tell two
    apart. A page that cannot read the key cannot leak it.

    Shared, not even that. Four characters is not a key, but it identifies
    which one is installed, and the list of AWS profiles is the naming of an
    organisation's accounts and roles. Both are answers to "what is this server
    holding", which is a question for whoever runs it and nobody else. This is
    reached on every page load, so it is the widest disclosure in the program.
    """
    ok, reason = ai.status()
    site = deploy.current()
    state = {
        "available": ok,
        "reason": reason,
        "source": config.source("anthropic"),
        "model": ai.MODEL,
        "hosts": sorted(set(fetch.HOSTS)),
    }
    if site.shared:
        # Enough for the page to hide the controls nobody here may use.
        return {**state, "secrets": {}, "aws_profile": "", "aws_profiles": [],
                "hint": "", "managed": True}
    return {
        **state,
        "hint": config.hint("anthropic"),
        # Every credential the viewer can hold — names and tails, never values.
        "secrets": config.state(),
        "aws_profile": config.aws_profile(),
        "aws_profiles": fetch._profiles(),
        "managed": False,
    }


def _safe_name(raw: str) -> str:
    """Reduce a client-supplied filename to a leaf, so it cannot escape."""
    name = Path(unquote(raw or "")).name.strip()
    return name or "upload"


def _associated_files(source: Path) -> list[dict]:
    """Everything that travelled with a session: subagents, sidecars, siblings.

    A session is rarely one file — Claude Code keeps subagent traces in a
    sibling directory, and a bundle carries images next to the trajectory.
    """
    found: list[dict] = []
    seen: set[Path] = set()

    def add(path: Path, role: str) -> None:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append(
            {
                "name": path.name,
                "path": str(path),
                "role": role,
                "size": path.stat().st_size,
            }
        )

    add(source, "source")
    subagents = source.parent / source.stem / "subagents"
    if subagents.is_dir():
        for file in sorted(subagents.iterdir()):
            add(file, "subagent")
    # A bundle keeps its images and manifest beside the trajectory.
    for sibling in sorted(source.parent.iterdir()):
        if sibling == source:
            continue
        if sibling.is_dir() and sibling.name == "images":
            for image in sorted(sibling.iterdir()):
                add(image, "image")
        elif sibling.is_file() and sibling.suffix in {
            ".json",
            ".jsonl",
            ".har",
            ".md",
            ".txt",
        }:
            add(sibling, "sibling")
    return found


# What each route needs of whoever is asking. Every route is named here, and a
# test fails if one is added without an answer — the failure this guards against
# is not a wrong policy but a forgotten one, which defaults to open.
#
#   read   anyone who can reach the server at all
#   write  a person, acting on what is theirs
#   admin  reconfigures the server, spends its money, or destroys shared state
#   local  meaningless anywhere but the machine the server runs on
POLICY: dict[tuple[str, str], str] = {
    ("GET", "/"): "read",
    ("GET", "/api/index"): "read",
    ("GET", "/api/raw"): "read",
    ("GET", "/api/files"): "read",
    ("GET", "/api/image"): "read",
    ("GET", "/api/trajectory"): "read",
    # Opens a window on the server's own desktop. On a shared host that is
    # somebody else's machine, and the path is a caller's to choose.
    ("GET", "/api/reveal"): "local",
    ("POST", "/api/library"): "write",
    ("POST", "/api/ai"): "write",
    # All four drive the server's own AWS, Hugging Face and GitHub identity at a
    # URL the caller picked, and three of them write to its disk.
    ("POST", "/api/browse"): "admin",
    ("POST", "/api/measure"): "admin",
    ("POST", "/api/fetch"): "admin",
    ("POST", "/api/refetch"): "admin",
    # Indexes whatever the server can see.
    ("POST", "/api/scan"): "admin",
    # Writes the credentials every other request then spends.
    ("POST", "/api/settings"): "admin",
    # Takes up to two gigabytes onto the server's disk and adds it to a
    # catalogue everyone reads. Closer to fetching than to annotating, and on a
    # deployment fed from a bucket nobody needs to upload by hand.
    ("POST", "/api/open"): "admin",
    # The catalogue is shared, so removing from it is removing for everyone.
    ("DELETE", "/api/library"): "admin",
    ("DELETE", "/api/group"): "admin",
}


class _Handler(BaseHTTPRequestHandler):
    entries: list[Entry] = []
    # Bounded, and shared by every connection. Unbounded dictionaries here held
    # every transcript anyone opened until the process ended.
    cache: cachelib.Kept = cachelib.Kept()
    media: cachelib.Kept = cachelib.Kept()
    lock = threading.Lock()

    def log_message(self, *args):  # keep the console clean
        pass

    def _may(self, url) -> bool:
        """Whether this request is allowed to be the one it is asking to be.

        Alone at a laptop everything is permitted, which is what the deployment
        defaults say and what this tool has always done. The moment anyone can
        sign in, the routes that reconfigure the server, spend its money or
        remove things for everybody stop being ordinary.

        An unlisted path answers True and falls through to the 404 below: a
        route that does not exist should say so, not say no.
        """
        need = POLICY.get((self.command, url.path))
        if need is None or need == "read":
            return True
        site = deploy.current()
        if need == "local":
            return not site.shared
        if need == "write":
            return not site.shared or identity.who(self.headers, site) is not None
        return identity.may_administer(identity.who(self.headers, site), site)

    def _refuse(self) -> None:
        self._json({"error": "not yours to do"}, 403)

    def log_message(self, fmt: str, *args) -> None:
        """Quiet on a laptop, and a record once anyone else can reach this.

        A shared service with no access log cannot answer who did something,
        which is the first question asked after anything goes wrong. On one
        person's machine it is their own tool talking to itself, and a line per
        request is noise they did not ask for.
        """
        site = deploy.current()
        if not site.shared:
            return
        who = identity.who(self.headers, site) or "-"
        sys.stderr.write(
            f"{self.address_string()} {who} [{self.log_date_time_string()}] "
            f"{fmt % args}\n"
        )

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here is worth keeping: the page is re-read whenever it
        # changes, a trajectory is reconverted whenever its parser does, and a
        # session on disk grows while it is being read. `page()` goes to the
        # trouble of stat-ing the file on every load precisely so that an edit
        # shows up — which a browser holding yesterday's copy quietly undoes.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _entry(self, url):
        """Resolve ?id=<content key> to an entry, or None.

        Addressing by list position broke the moment entries could be added,
        removed or reordered — a stale link would open a different session.
        """
        key = (parse_qs(url.query).get("id") or [""])[0]
        if not key:
            return None
        return next((e for e in self.entries if e.key == key), None)

    def _forget(self, keys: set[str]) -> int:
        """Drop sessions from the library and the index.

        Files are removed only where they are the viewer's own. A copy under
        ~/.transcript-viewer/opened exists solely because the viewer made it; a scanned
        session and a URL download are somebody's own files, sitting where they
        chose, and forgetting a row is not permission to delete them.
        """
        removed = 0
        for key in keys:
            entry = next((e for e in self.entries if e.key == key), None)
            library.remove(key)
            if entry is None:
                continue
            removed += 1
            if entry.origin != "opened":
                continue
            source = Path(entry.path)
            if corpus.OPENED_ROOT not in source.parents:
                continue
            # An unpacked archive puts several logs under one directory, so
            # clearing it wholesale would delete the siblings' files and leave
            # their rows pointing at nothing.
            store = corpus.OPENED_ROOT / source.relative_to(corpus.OPENED_ROOT).parts[0]
            shares = any(
                e.key not in keys and store in Path(e.path).parents
                for e in self.entries
            )
            if shares:
                source.unlink(missing_ok=True)
            else:
                shutil.rmtree(store, ignore_errors=True)

        with self.lock:
            self.entries = [e for e in self.entries if e.key not in keys]
            _Handler.entries = self.entries
        corpus.save(self.entries)
        return removed

    def _in_group(self, name: str) -> set[str]:
        """Keys sitting under a node of the tree, nested ones included.

        Read once, outside the comprehension. Written inside it this asked the
        library for every entry once per entry — cheap against a cached
        dictionary, a query per session against rows.
        """
        records = library.load()
        return {
            e.key
            for e in self.entries
            if (at := _group(e, records.get(e.key, {}).get("source", "")))
            and (at == name or at.startswith(f"{name}/"))
        }

    def do_DELETE(self) -> None:
        url = urlparse(self.path)
        if not self._may(url):
            self._refuse()
            return
        query = parse_qs(url.query)

        if url.path == "/api/group":
            name = (query.get("name") or [""])[0]
            if not name:
                self._json({"error": "a folder is required"}, 400)
                return
            # A node of the tree is a fact about where sessions came from, so it
            # cannot be removed on its own — only what sits under it can.
            self._json({"removed": self._forget(self._in_group(name)), "folder": name})
            return

        if url.path != "/api/library":
            self.send_error(404)
            return

        if (query.get("all") or [""])[0] == "1":
            # Everything the library knows. Files are left where they are by the
            # same rule as a single removal: only a copy the viewer made is ours
            # to delete.
            self._json({"removed": self._forget({e.key for e in self.entries})})
            return

        key = (query.get("id") or [""])[0]
        if not key:
            self._json({"error": "a key is required"}, 400)
            return
        entry = next((e for e in self.entries if e.key == key), None)
        origin = entry.origin if entry else ""
        self._forget({key})
        self._json(
            {
                "removed": True,
                # Say plainly whether anything left the disk.
                "removed_copy": origin == "opened",
                "origin": origin,
            }
        )

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not self._may(url):
            self._refuse()
            return

        if url.path == "/api/scan":
            # Deliberate, not automatic: this indexes every agent session on the
            # machine, which is not something to do because a library is empty.
            try:
                found = scan()
            except (ValueError, OSError) as exc:
                self._json({"error": str(exc)}, 500)
                return
            with self.lock:
                merged = corpus.merge(self.entries, found)
                self.entries = merged
                _Handler.entries = merged
            corpus.save(merged)
            self._json({"found": len(found), "total": len(merged)})
            return

        if url.path == "/api/refetch":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            # A node knows where it came from and where it was put, so refreshing
            # is the original fetch asked again — no new decisions to make.
            node = body.get("node") or ""
            keys = self._in_group(node)
            records = [library.get(k) for k in keys]
            source = next((r["source"] for r in records if r.get("source")), "")
            into = next((r["into"] for r in records if r.get("into")), "")
            if not source:
                self._json({"error": "that folder was not fetched from anywhere"}, 400)
                return
            self._fetch({"url": source, "into": into, "confirm": True})
            return

        if url.path == "/api/browse":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            try:
                nodes = fetch.browse(
                    body.get("url") or "", body.get("path") or "", config.tokens()
                )
            except fetch.FetchError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"nodes": [n._asdict() for n in nodes]})
            return

        if url.path == "/api/measure":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            try:
                self._json(
                    fetch.measure(
                        body.get("url") or "", body.get("paths") or [], config.tokens()
                    )
                )
            except fetch.FetchError as exc:
                self._json({"error": str(exc)}, 400)
            return

        if url.path == "/api/fetch":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            self._fetch(body)
            return

        if url.path == "/api/settings":
            if deploy.current().secrets == "server":
                # Accepting one would write it to a file `config.secret` no
                # longer reads, so the key would appear to be saved and change
                # nothing — a worse answer than a refusal.
                self._json({"error": "this server holds its own credentials"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            # The key is read out of the body and handed straight to storage:
            # not logged, not echoed, not kept in memory beyond this call.
            try:
                name = body.get("name") or "anthropic"
                if body.get("clear"):
                    config.clear_secret(name)
                elif "aws_profile" in body:
                    config.set_aws_profile(body.get("aws_profile") or "")
                elif "token" in body or "api_key" in body:
                    config.set_secret(
                        name, body.get("token") or body.get("api_key") or ""
                    )
                else:
                    self._json({"error": "nothing to change"}, 400)
                    return
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            except OSError as exc:
                self._json({"error": f"could not save settings: {exc.strerror}"}, 500)
                return
            self._json(_ai_state())
            return

        if url.path == "/api/ai":
            if not ai.available():
                self._json({"error": "AI features are not configured here."}, 501)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return

            entry = next((e for e in self.entries if e.key == body.get("key")), None)
            if entry is None:
                self._json({"error": "no such session"}, 404)
                return
            trajectory = self._trajectory(entry)
            if trajectory is None:
                self._json({"error": "could not read that session"}, 500)
                return

            try:
                if body.get("what") == "call":
                    self._stream_call(body, entry, trajectory)
                elif body.get("what") == "ask":
                    question = (body.get("question") or "").strip()
                    if not question:
                        self._json({"error": "ask what?"}, 400)
                        return
                    self._stream_ask(
                        question, trajectory, body.get("history"), body.get("focus")
                    )
                else:
                    self._json({"error": "unknown request"}, 400)
            except ai.Unavailable as exc:
                # Raised while building the client, before a byte is written, so
                # a status code is still available.
                self._json({"error": str(exc)}, 503)
            return

        if url.path == "/api/library":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "expected a JSON body"}, 400)
                return
            key = body.get("key")
            if not isinstance(key, str) or not key:
                self._json({"error": "a key is required"}, 400)
                return
            fields = {k: v for k, v in body.items() if k != "key"}
            try:
                record = library.update(key, **fields)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"key": key, **record})
            return

        if url.path != "/api/open":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if length <= 0:
            self._json({"error": "empty upload"}, 400)
            return
        if length > UPLOAD_LIMIT:
            self._json(
                {"error": f"file is larger than {UPLOAD_LIMIT // 1024**3} GB"}, 413
            )
            return

        name = _safe_name(self.headers.get("X-Filename", ""))
        staging = Path(tempfile.mkdtemp(prefix="atif-open-"))
        staged = staging / name
        try:
            remaining = length
            with staged.open("wb") as handle:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            self._json({"error": f"could not save upload: {exc}"}, 500)
            return

        # Derive the identity before choosing where it lands, so re-opening the
        # same file updates its place instead of accumulating copies.
        home = corpus.OPENED_ROOT / corpus.content_key(staged)
        try:
            home.mkdir(parents=True, exist_ok=True)
            if is_container(staged):
                # Keep what is inside, not the container: the logs are what get
                # indexed, and unpacking here keeps their paths stable and any
                # sibling images resolvable. Storing the zip — or a benchmark's
                # thousand-run JSON array — would mean opening it again on every
                # start.
                unpacked = open_container(staged)
                for item in unpacked.iterdir():
                    shutil.move(str(item), home / item.name)
                target = home
            else:
                target = home / name
                shutil.move(str(staged), target)
        except (OSError, ValueError) as exc:
            shutil.rmtree(home, ignore_errors=True)
            self._json({"error": f"could not store upload: {exc}"}, 500)
            return
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        # Exactly what `transcript-viewer <path>` and `atif-make index` do: the CLI and
        # this button must never disagree about what counts as openable.
        try:
            found = scan([target], origin="opened")
        except (ValueError, OSError) as exc:
            shutil.rmtree(home, ignore_errors=True)
            self._json({"error": str(exc)}, 400)
            return
        if not found:
            # Nothing usable in it — do not keep the copy around. Say what was
            # in it on the way out: an answer of "nothing convertible" with
            # nothing behind it reads as the tool failing rather than the file
            # not holding what was hoped.
            inside = corpus.describe_contents(target)
            shutil.rmtree(home, ignore_errors=True)
            self._json(
                {
                    "error": f"No transcript in {name}. {inside}, and none of it "
                    f"is a log this can read ({', '.join(FORMATS)})."
                },
                415,
            )
            return

        with self.lock:
            merged = corpus.merge(self.entries, found)
            self.entries = merged
            _Handler.entries = merged
        # Persist so an opened file is present next start without a re-scan.
        corpus.save(merged)
        self._json(
            {
                "added": len(found),
                "keys": [e.key for e in found],
                "names": [Path(e.path).name for e in found],
            }
        )

    def _fetch(self, body: dict) -> None:
        """List what a URL holds, then — only when told twice — download it.

        Two steps on purpose: a dataset URL can name hundreds of files, and
        nobody should discover that by pressing a button once.
        """
        try:
            picked = body.get("paths")
            plan = (
                fetch.select(body.get("url") or "", picked, config.tokens())
                if picked
                else fetch.plan(body.get("url") or "", config.tokens())
            )
            service, label, files = plan.service, plan.label, plan.files
            into = fetch.destination(body.get("into"))
        except fetch.FetchError as exc:
            self._json({"error": str(exc)}, 400)
            return

        known = sum(f.size or 0 for f in files)
        home = into / label
        if not body.get("confirm"):
            self._json(
                {
                    "plan": True,
                    "service": service,
                    "label": label,
                    "count": len(files),
                    "bytes": known,
                    "names": [f.name for f in files[:12]],
                    "into": str(home),
                }
            )
            return

        def adopt(merged):
            with self.lock:
                self.entries = merged
                _Handler.entries = merged

        self._frames(
            lambda: ingest.bring(plan, into, self.entries, _source_of, adopt)
        )

    def _trajectory(self, entry) -> dict | None:
        """The converted trajectory for an entry, from cache when it is there."""
        cached = self.cache.get(entry.key)
        if cached is not None:
            return cached
        try:
            trajectory, _ = convert(Path(entry.path), entry.format)
        except Exception:
            return None
        payload = trajectory.to_dict()
        self.cache[entry.key] = payload
        return payload

    def _find_call(self, trajectory: dict, call_id: str) -> tuple[dict | None, Any]:
        """One tool call and its output, by id."""
        for step in trajectory.get("steps", []):
            for candidate in step.get("tool_calls") or []:
                if candidate.get("tool_call_id") == call_id:
                    output = None
                    for row in (step.get("observation") or {}).get("results") or []:
                        if row.get("source_call_id") == call_id:
                            output = row.get("content")
                    return candidate, output
        return None, None

    def _frames(self, produce) -> None:
        """Stream newline-delimited JSON frames as they are produced.

        Not server-sent events: EventSource reconnects on close, which would
        silently repeat a paid call. A plain streamed response read by fetch
        does not.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        # Without this the browser withholds the first bytes while it sniffs the
        # type, which swallows the beginning of a stream.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            for frame in produce():
                self.wfile.write(json.dumps(frame).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The reader navigated away. Nothing to report to.
            pass

    def _stream_call(self, body: dict, entry, trajectory: dict) -> None:
        """Explain one tool call, reusing a summary already paid for."""
        call_id = body.get("call_id")
        stored = library.get(entry.key).get("summaries") or {}
        if call_id in stored and not body.get("again"):
            self._frames(
                lambda: [{"t": "delta", "text": stored[call_id]}, {"t": "done"}]
            )
            return

        call, output = self._find_call(trajectory, call_id)
        if call is None:
            self._json({"error": "no such call"}, 404)
            return

        # Built here so a credential problem is a status code, not a half-stream.
        chunks = ai.summarise_call_stream(call, output)

        def produce():
            parts: list[str] = []
            try:
                for kind, piece in chunks:
                    if kind != "text":
                        yield {"t": "thinking"}
                        continue
                    parts.append(piece)
                    yield {"t": "delta", "text": piece}
            except ai.Unavailable as exc:
                yield {"t": "error", "error": str(exc)}
                return
            summary = "".join(parts).strip()
            if summary:
                library.update(entry.key, summaries={**stored, call_id: summary})
            yield {"t": "done"}

        self._frames(produce)

    def _stream_ask(
        self, question: str, trajectory: dict, history: Any, focus: Any
    ) -> None:
        """Answer a question, saying which steps it is reading first."""
        turns = history if isinstance(history, list) else []
        earlier = (
            [n for n in focus if isinstance(n, int)] if isinstance(focus, list) else []
        )
        steps = trajectory.get("steps", [])
        used, chunks = ai.ask_stream(question, steps, turns, earlier)

        def produce():
            # The total matters as much as the count: 40 of 40 means the whole
            # transcript was read, 40 of 3,000 means the answer saw a sliver.
            yield {"t": "steps", "steps": used, "total": len(steps)}
            try:
                for kind, piece in chunks:
                    # Thinking is signalled, not shown: the point is that
                    # something is happening, not what it says.
                    yield (
                        {"t": "thinking"}
                        if kind != "text"
                        else {"t": "delta", "text": piece}
                    )
            except ai.Unavailable as exc:
                yield {"t": "error", "error": str(exc)}
                return
            yield {"t": "done"}

        self._frames(produce)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if not self._may(url):
            self._refuse()
            return

        if url.path == "/":
            self._send(page().encode(), "text/html; charset=utf-8")
            return

        if url.path == "/api/index":
            # Read once. Asking per session made a page load parse the whole
            # library as many times as there were sessions in it.
            records = library.load()
            rows = [
                {
                    "key": e.key,
                    "origin": e.origin,
                    "path": e.path,
                    "agent": e.agent,
                    "format": e.format,
                    "session_id": e.session_id,
                    "project": e.project,
                    "modified": e.modified,
                    "size_bytes": e.size_bytes,
                    "subagents": e.subagents,
                    "session_title": e.session_title,
                    "group": _group(e, records.get(e.key, {}).get("source", "")),
                }
                for e in self.entries
                # A downloaded folder the reader deleted, or a drive not mounted
                # right now. Hidden rather than purged: an entry is cheap to
                # keep, and deleting one because a volume is offline is not
                # recoverable.
                if Path(e.path).exists()
            ]
            payload = {
                "ai": _ai_state(),
                "downloads": str(fetch.destination(None)),
                "sessions": library.decorate(rows),
                "groups": _groups(rows),
                "tags": [{"name": t, "count": n} for t, n in library.tags()],
            }
            self._send(json.dumps(payload).encode(), "application/json")
            return

        if url.path == "/api/reveal":
            raw = (parse_qs(url.query).get("path") or [""])[0]
            if not raw:
                self.send_error(400)
                return
            target = Path(raw).expanduser()
            if _reveal(target):
                self._send(b"ok", "text/plain")
            else:
                self.send_error(404)
            return

        if url.path == "/api/raw":
            entry = self._entry(url)
            if entry is None:
                self.send_error(404)
                return
            source = Path(entry.path)
            try:
                # A rollout can be hundreds of MB; send a head, not the lot.
                with source.open("rb") as handle:
                    body = handle.read(RAW_LIMIT + 1)
            except OSError:
                self.send_error(404)
                return
            truncated = len(body) > RAW_LIMIT
            payload = {
                "path": str(source),
                "size": source.stat().st_size,
                "truncated": truncated,
                "text": body[:RAW_LIMIT].decode("utf-8", errors="replace"),
            }
            self._send(json.dumps(payload).encode(), "application/json")
            return

        if url.path == "/api/files":
            entry = self._entry(url)
            if entry is None:
                self.send_error(404)
                return
            self._send(
                json.dumps(_associated_files(Path(entry.path))).encode(),
                "application/json",
            )
            return

        if url.path == "/api/image":
            query = parse_qs(url.query)
            index = (query.get("id") or [""])[0]
            name = (query.get("name") or [""])[0]
            with self.lock:
                item = self.media.get(index, {}).get(name)
            if item is None:
                self.send_error(404)
                return
            self._send(item.data, item.media_type)
            return

        if url.path == "/api/trajectory":
            entry = self._entry(url)
            if entry is None:
                self._send(
                    json.dumps({"error": "no such session"}).encode(),
                    "application/json",
                )
                return
            index = entry.key

            with self.lock:
                cached = self.cache.get(index)
            if cached is None:
                try:
                    trajectory, _ = convert(Path(entry.path), entry.format)
                    store = trajectory.all_media()
                    if store:
                        _point_images_at_server(trajectory, index)
                        with self.lock:
                            self.media[index] = dict(store.items)
                    cached = trajectory.to_dict()
                except Exception as exc:  # a bad log should not kill the server
                    self._send(
                        json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                        "application/json",
                    )
                    return
                with self.lock:
                    self.cache[index] = cached
            self._send(json.dumps(cached).encode(), "application/json")
            return

        self.send_error(404)


def _bind(port: int, handler, explicit: bool) -> ThreadingHTTPServer:
    """Bind to `port`, or to the next free one when the choice was ours.

    Viewing a second session while a first is still open is normal, so a busy
    default should not be an error. An explicitly requested port is honoured or
    reported — silently moving it would be worse than failing.
    """
    last: OSError | None = None
    for candidate in range(port, port + (1 if explicit else 20)):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last = exc
    raise SystemExit(
        f"transcript-viewer: port {port} is already in use"
        + ("" if explicit else f" (tried {port}-{port + 19})")
        + ".\nPass --port to choose another, or stop the running viewer."
    ) from last


def serve(
    entries: list[Entry] | None = None,
    port: int = 7433,
    open_browser: bool = True,
    explicit_port: bool = False,
) -> None:
    handler = partial(_Handler)
    _Handler.entries = entries if entries is not None else scan()
    budget = deploy.current().cache_bytes
    _Handler.cache = cachelib.Kept(int(budget) if budget else cachelib.DEFAULT_BUDGET)
    _Handler.media = cachelib.Kept(int(budget) if budget else cachelib.DEFAULT_BUDGET)

    # Loopback only: these logs contain source code and tool output.
    server = _bind(port, handler, explicit_port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"transcript-viewer: {url}  ({len(_Handler.entries)} sessions)")
    print("Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
