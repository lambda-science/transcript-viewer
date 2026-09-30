"""The AI layer, tested without calling the API.

The model call itself is stubbed — what matters here is that nothing is sent
unasked, that a paid-for summary is not paid for twice, and that a question
against a transcript far larger than any context window still gets a sensible
subset of steps.
"""

import pytest

from transcript_viewer import ai


def _step(step_id, source="agent", message="", tool=None, output=None, reasoning=""):
    step = {"step_id": step_id, "source": source, "message": message}
    if reasoning:
        step["reasoning_content"] = reasoning
    if tool:
        step["tool_calls"] = [
            {
                "tool_call_id": f"c{step_id}",
                "function_name": tool[0],
                "arguments": tool[1],
            }
        ]
        if output is not None:
            step["observation"] = {
                "results": [{"source_call_id": f"c{step_id}", "content": output}]
            }
    return step


def _capture(seen, pieces=(("text", "ok"),)):
    """A stub for ai._stream that records what the request would have carried."""

    def stub(client, system, messages, max_tokens, thinking=True):
        seen["messages"] = messages
        seen["thinking"] = thinking
        return iter(list(pieces))

    return stub


# ---- nothing runs without a credential ---------------------------------------


def test_unavailable_without_the_extra_or_a_credential(monkeypatch):
    def refuse():
        raise ai.Unavailable("nope")

    monkeypatch.setattr(ai, "_client", refuse)
    assert ai.available() is False


def test_available_when_a_client_can_be_built(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    assert ai.available() is True


# ---- picking what to send -----------------------------------------------------


def test_relevant_steps_prefers_steps_that_mention_the_question():
    steps = [
        _step(1, message="setting up the project"),
        _step(2, message="editing the authentication middleware"),
        _step(3, message="running the formatter"),
        _step(4, tool=("Bash", {"command": "pytest tests/test_auth.py"})),
    ]
    picked = ai.relevant_steps("where did it change authentication?", steps, limit=2)
    assert [s["step_id"] for s in picked] == [2, 4]


def test_a_long_word_also_matches_on_its_stem():
    """A question says "authentication"; the transcript says "test_auth.py"."""
    steps = [_step(1, message="unrelated"), _step(2, message="ran test_auth.py")]
    assert ai.relevant_steps("authentication", steps, limit=1)[0]["step_id"] == 2


def test_a_stem_match_ranks_below_a_whole_word_match():
    steps = [_step(1, message="auth helper"), _step(2, message="authentication flow")]
    assert ai.relevant_steps("authentication", steps, limit=1)[0]["step_id"] == 2


def test_relevant_steps_keeps_document_order():
    """An answer that jumps around the transcript is harder to check."""
    steps = [_step(i, message="auth" if i % 2 else "other") for i in range(1, 11)]
    picked = ai.relevant_steps("auth", steps, limit=5)
    assert [s["step_id"] for s in picked] == sorted(s["step_id"] for s in picked)


def test_relevant_steps_falls_back_to_the_end_when_nothing_matches():
    steps = [_step(i, message="unrelated") for i in range(1, 21)]
    picked = ai.relevant_steps("quantum tunnelling", steps, limit=3)
    assert [s["step_id"] for s in picked] == [18, 19, 20]


def test_relevant_steps_handles_a_question_with_no_usable_words():
    steps = [_step(i) for i in range(1, 6)]
    assert len(ai.relevant_steps("?? !!", steps, limit=2)) == 2


def test_a_step_search_sees_tool_arguments_and_output():
    """What a reader can see, the search can see."""
    steps = [
        _step(1, message="nothing here"),
        _step(2, tool=("Bash", {"command": "grep -r needle src/"}), output="found it"),
    ]
    assert ai.relevant_steps("needle", steps, limit=1)[0]["step_id"] == 2
    assert ai.relevant_steps("found", steps, limit=1)[0]["step_id"] == 2


def test_ask_stays_inside_its_character_budget(monkeypatch):
    """A transcript can be far larger than any context window."""
    sent = {}

    def fake_stream(client, system, messages, max_tokens, thinking=True):
        sent["prompt"] = messages[-1]["content"]
        return iter([("text", "an "), ("text", "answer")])

    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", fake_stream)

    huge = [_step(i, message="auth " + "x" * 9000) for i in range(1, 400)]
    answer, used = ai.ask("auth", huge)

    assert answer == "an answer"
    assert len(sent["prompt"]) < ai.ASK_BUDGET_CHARS + 20_000
    assert used and len(used) <= ai.ASK_STEPS


def test_ask_reports_which_steps_it_used(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", lambda *a, **k: iter([("text", "answer")]))
    steps = [
        _step(1, message="alpha"),
        _step(2, message="beta"),
        _step(3, message="beta"),
    ]
    _, used = ai.ask("beta", steps)
    assert used == [1, 2, 3], "a three-step transcript should go whole"


# ---- summarising one call ------------------------------------------------------


def test_summarise_call_sends_the_call_and_its_output(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai,
        "_stream",
        lambda client, system, messages, max_tokens, thinking=True: iter(
            [("text", seen.setdefault("p", messages[-1]["content"]) and "ok")]
        ),
    )
    ai.summarise_call(
        {"function_name": "Bash", "arguments": {"command": "ls -la"}}, "README.md"
    )
    assert "Bash" in seen["p"] and "ls -la" in seen["p"] and "README.md" in seen["p"]


def test_summarise_call_copes_with_no_output(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai,
        "_stream",
        lambda client, system, messages, max_tokens, thinking=True: iter(
            [("text", seen.setdefault("p", messages[-1]["content"]) and "ok")]
        ),
    )
    ai.summarise_call({"function_name": "Bash", "arguments": {}}, None)
    assert "none recorded" in seen["p"]


def test_large_output_is_clipped_rather_than_sent_whole(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai,
        "_stream",
        lambda client, system, messages, max_tokens, thinking=True: iter(
            [("text", seen.setdefault("p", messages[-1]["content"]) and "ok")]
        ),
    )
    ai.summarise_call({"function_name": "Bash", "arguments": {}}, "x" * 500_000)
    assert len(seen["p"]) < 20_000


# ---- streaming ----------------------------------------------------------------


def test_summarise_streams_in_pieces(monkeypatch):
    """The point of streaming: text is available before the call finishes."""
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai, "_stream", lambda *a, **k: iter([("text", "It "), ("text", "ran "), ("text", "ls.")])
    )
    assert [t for _, t in ai.summarise_call_stream({"function_name": "Bash"})] == [
        "It ", "ran ", "ls.",
    ]


def test_the_collected_answer_matches_the_stream(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(
        ai,
        "_stream",
        lambda *a, **k: iter([("thinking", "hmm"), ("text", "It "), ("text", "ran ls. ")]),
    )
    assert ai.summarise_call({"function_name": "Bash"}) == "It ran ls."


def test_ask_knows_its_steps_before_the_first_token(monkeypatch):
    """The viewer says what it is reading while the answer is still arriving."""
    started = []

    def fake_stream(*a, **k):
        started.append(True)
        yield "text", "answer"

    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", fake_stream)

    used, chunks = ai.ask_stream("beta", [_step(1, message="alpha"), _step(2, message="beta")])
    assert used == [1, 2]
    assert not started, "the steps must be known without consuming the stream"
    assert "".join(t for _, t in chunks) == "answer"


def test_a_missing_credential_is_raised_before_streaming_starts(monkeypatch):
    """So the server can refuse with a status code rather than mid-stream."""

    def refuse():
        raise ai.Unavailable("no credential")

    monkeypatch.setattr(ai, "_client", refuse)
    with pytest.raises(ai.Unavailable):
        ai.summarise_call_stream({"function_name": "Bash"})
    with pytest.raises(ai.Unavailable):
        ai.ask_stream("q", [])


# ---- conversation ---------------------------------------------------------------


def test_a_follow_up_carries_the_earlier_turns(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))
    ai.ask("why?", [_step(1)], history=[{"q": "what broke?", "a": "the parser"}])
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert seen["messages"][0]["content"] == "what broke?"
    assert seen["messages"][1]["content"] == "the parser"


def test_earlier_step_dumps_are_not_replayed(monkeypatch):
    """Re-sending a page of transcript per turn would make a long chat quadratic."""
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))
    ai.ask("and then?", [_step(1, message="x" * 5000)],
           history=[{"q": "first", "a": "an answer"}])
    assert "--- step" not in seen["messages"][0]["content"]


def test_a_half_finished_turn_is_left_out_of_history(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))
    ai.ask("next", [_step(1)], history=[{"q": "asked", "a": ""}, {"q": "", "a": "orphan"}])
    assert len(seen["messages"]) == 1, "an unanswered turn must not be sent"


def test_history_is_bounded(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))
    long = [{"q": f"q{i}", "a": "a" * 9000} for i in range(40)]
    ai.ask("now", [_step(1)], history=long)
    assert len(seen["messages"]) == ai.HISTORY_TURNS * 2 + 1
    assert all(len(m["content"]) <= ai.HISTORY_CHARS for m in seen["messages"][:-1])


def test_a_call_summary_does_not_wait_on_thinking(monkeypatch):
    """A two-sentence description is delayed, not improved, by deliberation."""
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))
    ai.summarise_call({"function_name": "Bash"})
    assert seen["thinking"] is False


def test_a_question_does_use_thinking(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))
    ai.ask("why", [_step(1)])
    assert seen["thinking"] is True


# ---- how much of a transcript goes ------------------------------------------------


def test_a_transcript_that_fits_is_sent_whole(monkeypatch):
    """Selecting forty steps from a session that fits entire only loses data."""
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))

    steps = [_step(i, message=f"step number {i}") for i in range(1, 121)]
    _, used = ai.ask("anything", steps)

    assert used == list(range(1, 121)), "a small transcript was needlessly trimmed"
    assert len(used) > ai.ASK_STEPS, "the flat step cap is still being applied"


def test_a_transcript_too_large_falls_back_to_selection(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture(seen))

    steps = [_step(i, message="filler " * 2000) for i in range(1, 200)]
    _, used = ai.ask("filler", steps)

    assert len(used) <= ai.ASK_STEPS
    assert len(seen["messages"][-1]["content"]) <= ai.ASK_BUDGET_CHARS + 5_000


def test_one_huge_step_does_not_truncate_the_rest(monkeypatch):
    """It used to stop at the first oversized block, losing every step after."""
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture({}))
    monkeypatch.setattr(ai, "ASK_BUDGET_CHARS", 3_000)

    steps = [_step(1, message="x" * 60_000)]
    steps += [_step(i, message="small") for i in range(2, 12)]
    _, used = ai.ask("small", steps)

    assert 1 not in used, "the oversized step should not fit"
    assert used == list(range(2, 12)), "steps after the oversized one were dropped"


def test_a_follow_up_with_no_keywords_reuses_the_last_turn_steps(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture({}))
    monkeypatch.setattr(ai, "ASK_BUDGET_CHARS", 3_000)

    steps = [_step(i, message="content " * 60) for i in range(1, 40)]
    _, used = ai.ask("why?", steps, focus=[7, 8, 9])
    assert used == [7, 8, 9]


def test_a_first_question_with_no_keywords_still_gets_the_closing_steps(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture({}))
    monkeypatch.setattr(ai, "ASK_BUDGET_CHARS", 3_000)

    steps = [_step(i, message="content " * 60) for i in range(1, 40)]
    _, used = ai.ask("why?", steps)
    assert used and used[-1] == 39


def test_a_focus_naming_steps_that_are_gone_falls_back(monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture({}))
    monkeypatch.setattr(ai, "ASK_BUDGET_CHARS", 3_000)

    steps = [_step(i, message="content " * 60) for i in range(1, 40)]
    _, used = ai.ask("why?", steps, focus=[900, 901])
    assert used, "an unusable focus left nothing to read"


def test_selection_still_ranks_by_the_question_when_it_must(monkeypatch):
    """The scoring is unchanged; it is simply no longer used unnecessarily."""
    monkeypatch.setattr(ai, "_client", lambda: object())
    monkeypatch.setattr(ai, "_stream", _capture({}))
    monkeypatch.setattr(ai, "ASK_BUDGET_CHARS", 4_000)

    steps = [_step(i, message="padding " * 100) for i in range(1, 30)]
    steps[14]["message"] = "the authentication middleware " + "padding " * 90
    _, used = ai.ask("authentication", steps)
    assert 15 in used
