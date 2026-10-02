"""transcript-viewer command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from . import config, corpus, db, deploy, fetch, ingest, store, viewer
from atif_make.container import is_container

from .viewer import serve


def _single_entry(path: Path) -> list[corpus.Entry]:
    """One file as an index entry, so it can be viewed without indexing.

    Built by corpus.describe rather than assembled here: an Entry that is put
    together in two places drifts the moment a field is added, and this one had
    already fallen behind.
    """
    entry = corpus.describe(path)
    return [entry] if entry else []


def cmd_view(args: argparse.Namespace) -> int:
    # The tool was called atif-view and kept its library under ~/.atif. Adopt
    # one if it is still there, before anything reads an index that would
    # otherwise look empty.
    if store.adopt_legacy():
        print(
            f"transcript-viewer: moved your library from {store.LEGACY_ROOT} "
            f"to {store.ROOT}",
            file=sys.stderr,
        )

    # Annotations used to be one JSON document. Import it here, after any rename
    # has settled, so a library left by either older shape is picked up on the
    # first run that finds it and never looks like it went missing.
    if moved := db.adopt_json():
        print(
            f"transcript-viewer: moved {moved} records into {db.DB_PATH.name}",
            file=sys.stderr,
        )

    if args.input:
        path = Path(args.input).expanduser()
        if not path.exists():
            print(f"transcript-viewer: no such path: {path}", file=sys.stderr)
            return 2
        # A directory or a container holds many sessions; a plain file
        # holds one.
        entries = (
            corpus.scan([path])
            if path.is_dir() or is_container(path)
            else _single_entry(path)
        )
        # An explicit path that holds nothing is a mistake worth reporting; an
        # empty library is not, so this only guards the argument.
        if not entries:
            print(
                f"transcript-viewer: no transcript in {path}. "
                f"{corpus.describe_contents(path)}, and none of it is a log "
                f"this can read.",
                file=sys.stderr,
            )
            return 1
    else:
        # Only what has been added deliberately. Scanning a machine because the
        # library happens to be empty indexes someone's whole history of every
        # agent without being asked; the viewer opens empty and offers to.
        entries = corpus.load()
    serve(
        entries,
        port=args.port or 7433,
        open_browser=not args.no_open,
        explicit_port=args.port is not None,
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Ask each source what it has now, and take what is not here yet.

    A command rather than a thread inside the server: it can be run by a
    schedule, by hand, or by neither, and a fetch that wedges takes nothing
    down with it. The same pipeline the Add dialog uses, so a place followed
    automatically and a place fetched by hand end in the same state.
    """
    urls = args.url or list(deploy.current().sources)
    if not urls:
        print(
            "transcript-viewer: nothing to sync. Name a URL, or list them under "
            f"[sources] in {deploy.CONFIG_PATH}.",
            file=sys.stderr,
        )
        return 2

    entries = corpus.load()
    into = fetch.destination(None)
    trouble = 0
    for url in urls:
        try:
            plan = fetch.plan(url, config.tokens())
        except fetch.FetchError as exc:
            print(f"transcript-viewer: {url}: {exc}", file=sys.stderr)
            trouble = 1
            continue

        print(f"{url} — {len(plan.files)} files", file=sys.stderr)
        for frame in ingest.bring(plan, into, entries, viewer._source_of):
            if frame["t"] == "error":
                print(f"transcript-viewer: {url}: {frame['error']}", file=sys.stderr)
                trouble = 1
            elif frame["t"] == "added":
                # `seen` counts what the place holds; `added` what was not here
                # already, which is the only number a schedule cares about.
                print(
                    f"{url} — {frame['added']} new of {frame['seen']}",
                    file=sys.stderr,
                )
        entries = corpus.load()
    return trouble


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcript-viewer",
        description="Browse agent transcripts in a local viewer.",
    )
    parser.add_argument("input", nargs="?",
                        help="file, directory, archive or S3 object (default: the local library)")
    parser.add_argument("--profile", help="AWS profile to use for an S3 object")
    parser.add_argument("--port", type=int, default=None,
                        help="port to listen on (default: 7433, or the next free one)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.set_defaults(func=cmd_view)
    return parser


def sync_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcript-viewer sync",
        description="Take what is new from each source.",
    )
    parser.add_argument("url", nargs="*",
                        help="where to look (default: the configured sources)")
    parser.set_defaults(func=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # One subcommand, matched before parsing rather than with subparsers: the
    # ordinary form takes a path, and argparse cannot be given both an optional
    # positional and a subcommand without one swallowing the other. A file
    # actually named `sync` is reachable as `./sync`.
    parser = sync_parser() if argv[:1] == ["sync"] else build_parser()
    args = parser.parse_args(argv[1:] if argv[:1] == ["sync"] else argv)
    if parser.prog == "transcript-viewer" and args.input and args.input.startswith("s3://"):
        bucket, _, key = args.input[len("s3://"):].partition("/")
        if (not fetch.S3_BUCKET.fullmatch(bucket) or not key or key.endswith("/")
                or Path(key).name in {"", ".", ".."}):
            print("transcript-viewer: expected s3://bucket/path/to/file", file=sys.stderr)
            return 2
        tokens = config.tokens()
        if args.profile is not None:
            tokens["aws"] = args.profile
        remote = fetch.Remote(name=Path(key).name, url=args.input)
        try:
            with TemporaryDirectory(prefix="transcript-viewer-") as directory:
                args.input = str(next(fetch.download("s3", [remote], Path(directory), tokens)))
                return args.func(args)
        except (fetch.FetchError, OSError) as exc:
            print(f"transcript-viewer: {exc}", file=sys.stderr)
            return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
