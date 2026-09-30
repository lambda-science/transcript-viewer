"""What has been converted lately, kept only while there is room for it.

Converting is expensive — seconds, and hundreds of megabytes for the largest
session here — so the answer is worth keeping. It was kept in a dictionary on
the handler class that nothing ever removed from, which is fine for an afternoon
on a laptop and is a leak everywhere else: open enough transcripts and the
process holds every one of them until it is restarted. In a container with a
memory limit that is not a leak but a crash.

So: least-recently-used, with a budget in bytes rather than in entries. Entries
are the wrong unit when one of them is a 143 MB rollout and the next is a
four-step session; a count that holds the small ones comfortably will hold two
of the large ones and stop.

Nothing here persists. A restart re-converts, which costs seconds once — the
alternative is a store of converted output that grows on disk instead of in
memory and needs its own eviction, and that is worth building when a restart is
frequent enough to notice, not before.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import Any

# Enough for the large sessions people actually open, and small enough that a
# modest container does not have to be sized around it. Measured against the
# corpus this was built on: the biggest single converted trajectory is ~34 MB,
# so this holds a working set of several without ever holding all of them.
DEFAULT_BUDGET = 256 * 1024 * 1024


def weigh(value: Any) -> int:
    """Roughly how much room something takes.

    `len(json.dumps(...))` rather than anything exact: the real cost of a nested
    dict is not knowable without walking it, this is within a small factor of
    it, and the number is only ever compared against a budget that is itself a
    judgement. Bytes are counted for anything already bytes, since that is what
    an image is and serialising it would be both wrong and enormous.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if (
        isinstance(value, dict)
        and value
        and all(isinstance(v, (bytes, bytearray)) for v in value.values())
    ):
        return sum(len(v) for v in value.values())
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        # Something unserialisable is still taking up room; guess rather than
        # refuse, because refusing would mean not caching it at all.
        return 1024


class Kept:
    """A bounded store of what was asked for recently.

    Thread-safe because the server is threaded and two requests for the same
    unconverted session arrive together often — both will convert, and both will
    store, and that is cheaper than holding a lock across a conversion.
    """

    def __init__(self, budget: int = DEFAULT_BUDGET):
        self.budget = budget
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._lock = threading.Lock()
        self.held = 0

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key not in self._items:
                return default
            self._items.move_to_end(key)
            return self._items[key]

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def put(self, key: str, value: Any) -> None:
        size = weigh(value)
        with self._lock:
            if key in self._items:
                self.held -= self._sizes.pop(key)
                del self._items[key]
            # Something larger than the whole budget is not cached rather than
            # evicting everything to make room for one thing nobody else can
            # then share.
            if size > self.budget:
                return
            self._items[key] = value
            self._sizes[key] = size
            self.held += size
            while self.held > self.budget and len(self._items) > 1:
                oldest, _ = self._items.popitem(last=False)
                self.held -= self._sizes.pop(oldest)

    def __setitem__(self, key: str, value: Any) -> None:
        self.put(key, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._sizes.clear()
            self.held = 0
