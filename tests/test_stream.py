"""The one function that talks to the SDK.

Every other test stubs `_stream`, which left the part most likely to break —
event dispatch, error mapping, the refusal check — never executed. These drive
it with a fake client, so no key and no network are involved, but the real
`anthropic` exception classes are used: if the SDK renames or re-parents one,
these fail rather than the first real call.
"""

from types import SimpleNamespace

import pytest

from transcript_viewer import ai

anthropic = pytest.importorskip("anthropic", reason='needs the "ai" extra')

# The SDK moved to httpx2 at 1.0; older releases are on httpx. Building a real
# response is the only way to construct its error classes.
try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover - depends on the installed SDK
    httpx = pytest.importorskip("httpx")


def _delta(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def _event(kind, delta=None):
    return SimpleNamespace(type=kind, delta=delta)


class _Stream:
    """Stands in for the SDK's streaming context manager."""

    def __init__(self, events, stop_reason="end_turn", raises=None):
        self.events, self.stop_reason, self.raises = events, stop_reason, raises

    def __enter__(self):
        if self.raises:
            raise self.raises
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return SimpleNamespace(stop_reason=self.stop_reason)


def _client(stream, seen=None):
    def make(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return stream

    return SimpleNamespace(messages=SimpleNamespace(stream=make))


def _request(status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status, request=req)


MESSAGES = [{"role": "user", "content": "hello"}]


# ---- dispatch -------------------------------------------------------------------


def test_text_and_thinking_are_told_apart():
    stream = _Stream(
        [
            _event("content_block_delta", _delta("thinking_delta", thinking="hmm")),
            _event("content_block_delta", _delta("text_delta", text="the ")),
            _event("content_block_delta", _delta("text_delta", text="answer")),
        ]
    )
    assert list(ai._stream(_client(stream), "sys", MESSAGES, 100)) == [
        ("thinking", "hmm"),
        ("text", "the "),
        ("text", "answer"),
    ]


def test_events_that_are_not_deltas_are_ignored():
    stream = _Stream(
        [
            _event("message_start"),
            _event("content_block_start"),
            _event("content_block_delta", _delta("text_delta", text="hi")),
            _event("content_block_stop"),
            _event("message_stop"),
        ]
    )
    assert list(ai._stream(_client(stream), "sys", MESSAGES, 100)) == [("text", "hi")]


def test_an_unfamiliar_delta_kind_is_skipped_rather_than_crashing():
    """A new delta type in a future SDK must not take the viewer down."""
    stream = _Stream(
        [
            _event("content_block_delta", _delta("signature_delta", signature="x")),
            _event("content_block_delta", _delta("text_delta", text="fine")),
        ]
    )
    assert list(ai._stream(_client(stream), "sys", MESSAGES, 100)) == [("text", "fine")]


def test_a_delta_with_no_type_is_skipped():
    stream = _Stream(
        [
            _event("content_block_delta", SimpleNamespace()),
            _event("content_block_delta", _delta("text_delta", text="ok")),
        ]
    )
    assert list(ai._stream(_client(stream), "sys", MESSAGES, 100)) == [("text", "ok")]


# ---- the request --------------------------------------------------------------


def test_adaptive_thinking_is_asked_for_by_default():
    seen = {}
    list(ai._stream(_client(_Stream([]), seen), "sys", MESSAGES, 100))
    assert seen["thinking"] == {"type": "adaptive"}
    assert seen["model"] == ai.MODEL
    assert seen["max_tokens"] == 100
    assert seen["messages"] == MESSAGES


def test_thinking_is_absent_rather_than_disabled_when_not_wanted():
    """A budget_tokens-style key would be rejected outright by this model."""
    seen = {}
    list(ai._stream(_client(_Stream([]), seen), "sys", MESSAGES, 100, thinking=False))
    assert "thinking" not in seen


# ---- failures ------------------------------------------------------------------


def test_a_refusal_is_raised_rather_than_returned_as_an_answer():
    stream = _Stream(
        [_event("content_block_delta", _delta("text_delta", text="I cannot"))],
        stop_reason="refusal",
    )
    with pytest.raises(ai.Unavailable, match="declined"):
        list(ai._stream(_client(stream), "sys", MESSAGES, 100))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            anthropic.NotFoundError("no", response=_request(404), body=None),
            "not available to this account",
        ),
        (
            anthropic.RateLimitError("slow", response=_request(429), body=None),
            "Rate limited",
        ),
        (
            anthropic.APIStatusError("boom", response=_request(500), body=None),
            "refused that request: 500",
        ),
        (
            anthropic.APIConnectionError(
                message="down",
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
            "Could not reach",
        ),
    ],
    ids=["not-found", "rate-limited", "server-error", "no-connection"],
)
def test_every_api_failure_becomes_something_a_reader_can_act_on(error, expected):
    stream = _Stream([], raises=error)
    with pytest.raises(ai.Unavailable, match=expected):
        list(ai._stream(_client(stream), "sys", MESSAGES, 100))


def test_the_most_specific_error_wins():
    """NotFoundError subclasses APIStatusError, so order in the chain matters."""
    stream = _Stream(
        [], raises=anthropic.NotFoundError("no", response=_request(404), body=None)
    )
    with pytest.raises(ai.Unavailable) as caught:
        list(ai._stream(_client(stream), "sys", MESSAGES, 100))
    assert "refused that request" not in str(caught.value)


def test_text_already_yielded_survives_a_later_refusal():
    """The generator is consumed lazily; earlier pieces are real output."""
    stream = _Stream(
        [_event("content_block_delta", _delta("text_delta", text="partial"))],
        stop_reason="refusal",
    )
    pieces = []
    with pytest.raises(ai.Unavailable):
        for piece in ai._stream(_client(stream), "sys", MESSAGES, 100):
            pieces.append(piece)
    assert pieces == [("text", "partial")]
