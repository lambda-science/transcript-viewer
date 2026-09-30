"""Bringing transcripts in: download, unpack, index, record where they came from.

This was written inside the request that first needed it, as a closure over the
handler, so nothing else could call it — which is why "keep this in step with a
bucket" had nowhere to start. It is the same four steps whether a person pressed
a button or a schedule came round, so it lives here and both call it.

Progress is yielded rather than printed. The server turns each frame into a line
of newline-delimited JSON for the page; the command prints them. Neither owns
the pipeline.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from atif_make.container import is_container, open_container

from . import config, corpus, fetch, library
from .corpus import Entry, scan


def unpack(home: Path) -> dict[Path, Path]:
    """Open every archive under `home`, in place.

    An archive is a container of transcripts rather than a transcript, and a
    bucket of agent runs is mostly zips, so a fetch and a dropped archive should
    end in the same state. Returns where each unpacked tree came from, because
    what is inside an archive belongs in the tree at the archive's own location
    rather than under whatever folder names the archive happens to use.
    """
    came_from: dict[Path, Path] = {}
    for item in sorted(home.rglob("*")):
        if not item.is_file() or not is_container(item):
            continue
        try:
            unpacked = open_container(item)
        except (ValueError, OSError):
            continue
        beside = item.with_suffix("")
        beside.mkdir(parents=True, exist_ok=True)
        for inner in unpacked.iterdir():
            shutil.move(str(inner), beside / inner.name)
        came_from[beside] = item.parent
    return came_from


def bring(
    plan: Any,
    into: Path,
    entries: list[Entry],
    source_of: Callable[[Entry, Path, str, dict], str],
    adopt: Callable[[list[Entry]], None] | None = None,
) -> Iterator[dict]:
    """Fetch what a plan names, and fold it into the library.

    `adopt` is how a caller that holds the index in memory learns the new one;
    a command that does not simply leaves it out. `source_of` is passed in
    rather than imported because placing a session in the tree is the server's
    idea of where things belong, not this module's.
    """
    home = into / plan.label
    tokens = config.tokens()

    try:
        home.mkdir(parents=True, exist_ok=True)
        done = 0
        for path in fetch.download(plan.service, plan.files, home, tokens):
            done += 1
            yield {
                "t": "file",
                "done": done,
                "total": len(plan.files),
                "name": path.name,
            }
    except fetch.FetchError as exc:
        yield {"t": "error", "error": str(exc)}
        return
    except OSError as exc:
        yield {"t": "error", "error": f"could not store: {exc.strerror}"}
        return

    came_from = unpack(home)

    # The same path an uploaded file takes, so a URL and a drop cannot disagree
    # about what counts as openable.
    try:
        found = scan([home], origin="fetched")
    except (ValueError, OSError) as exc:
        yield {"t": "error", "error": str(exc)}
        return
    if not found:
        shutil.rmtree(home, ignore_errors=True)
        yield {"t": "error", "error": "nothing convertible was downloaded"}
        return

    # What is new, not what the destination happens to contain: a second fetch
    # into the same folder re-scans everything already there, and reporting that
    # as "added" is a lie.
    known = {e.key for e in entries}
    merged = corpus.merge(entries, found)
    if adopt is not None:
        adopt(merged)
    corpus.save(merged)
    for entry in found:
        library.update(
            entry.key,
            source=source_of(entry, home, plan.web, came_from),
            into=str(into),
        )
    yield {
        "t": "added",
        "added": sum(1 for e in found if e.key not in known),
        "seen": len(found),
        "into": str(home),
    }
