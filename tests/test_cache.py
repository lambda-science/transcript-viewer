"""What is kept, and what is let go.

The fault this replaces was not subtle: a dictionary on the handler class that
nothing removed from. It is fine for an afternoon on a laptop and fatal in a
container with a memory limit, and it is exactly the kind of thing that never
shows up in a test suite because no test opens two hundred transcripts.
"""

from __future__ import annotations

from transcript_viewer.cache import Kept, weigh


def test_what_was_asked_for_is_there():
    kept = Kept()
    kept["a"] = {"steps": [1, 2, 3]}
    assert kept.get("a") == {"steps": [1, 2, 3]}
    assert kept.get("b") is None


def test_it_stops_growing():
    """The whole point. Two hundred sessions must not mean two hundred held."""
    kept = Kept(budget=10_000)
    for i in range(200):
        kept[f"s{i}"] = {"filler": "x" * 500}

    assert kept.held <= 10_000
    assert len(kept) < 200


def test_the_one_nobody_has_wanted_for_longest_goes_first():
    kept = Kept(budget=2_000)
    for name in ("a", "b", "c"):
        kept[name] = {"filler": "x" * 500}
    kept.get("a")  # asked for again, so no longer the oldest
    kept["d"] = {"filler": "x" * 500}

    assert kept.get("a") is not None
    assert kept.get("b") is None, "b had gone longest without being asked for"


def test_something_larger_than_the_whole_budget_is_simply_not_kept():
    """Rather than evicting everything to make room for one thing nobody else
    can then share. A 143 MB rollout should cost its own conversion, not
    everyone else's."""
    kept = Kept(budget=1_000)
    kept["small"] = {"filler": "x" * 100}
    kept["enormous"] = {"filler": "x" * 100_000}

    assert kept.get("enormous") is None
    assert kept.get("small") is not None


def test_replacing_something_does_not_count_it_twice():
    kept = Kept(budget=100_000)
    kept["a"] = {"filler": "x" * 500}
    once = kept.held
    kept["a"] = {"filler": "x" * 500}

    assert kept.held == once


def test_images_are_weighed_as_bytes_rather_than_serialised():
    """Serialising an image to measure it would be both wrong and enormous."""
    assert weigh({"a.png": b"x" * 1000}) == 1000
    assert weigh(b"x" * 40) == 40


def test_something_that_cannot_be_serialised_is_still_given_a_size():
    """It is taking up room whether or not it can be measured, and refusing to
    guess would mean never caching it."""

    class Odd:
        pass

    assert weigh({"x": Odd()}) > 0


def test_a_budget_of_one_thing_still_answers():
    """Falling to nothing cached would turn every request into a conversion."""
    kept = Kept(budget=10)
    kept["a"] = {"f": "x" * 100}
    kept["b"] = {"f": "x" * 100}

    assert len(kept) <= 1
