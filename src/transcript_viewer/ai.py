"""Optional Claude-backed help: explain one tool call, or answer a question
about a transcript.

Nothing here runs unless asked. A transcript carries source code, file contents
and tool output, so no call is made in the background, on load, or ahead of a
click — the viewer is local-only until you press a button.

The `anthropic` SDK is an optional extra (`pip install "transcript-viewer[ai]"`) so the
package keeps its zero-dependency install; these features simply hide when it,
or a credential, is absent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from . import config, deploy

MODEL = "claude-opus-5"

# Most transcripts are small enough to send whole; a few are not. The largest
# here is 8,445 steps and 12.9M characters, so there has to be a ceiling — but
# it applies only when a session actually exceeds it.
ASK_BUDGET_CHARS = 300_000
ASK_STEPS = 40
STEP_CLIP = 8_000


class Unavailable(RuntimeError):
    """No SDK, or no credential. Both mean the same thing to a caller."""


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise Unavailable(
            'The anthropic package is not installed: uv tool install "transcript-viewer[ai]"'
        ) from exc
    try:
        # A key from settings, else the SDK's own resolution: ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile. Never a key sent
        # up by the page — it only ever travels from the browser to storage.
        stored = config.api_key()
        return anthropic.Anthropic(api_key=stored) if stored else anthropic.Anthropic()
    except Exception as exc:
        raise Unavailable(
            "No Anthropic credential found. Add a key here, or set ANTHROPIC_API_KEY."
        ) from exc


def status() -> tuple[bool, str]:
    """Whether AI can run, and if not, what is missing.

    A bare boolean was not enough: a saved key with no SDK installed looks
    exactly like no key at all, which reads as "the save didn't work".

    A deployment that has turned this off is answered first and without
    qualification. Everything below is about whether a call *could* be made;
    this is about whether it may be, and the two must not be confused — a
    refusal that reads like a missing dependency invites someone to install it.
    """
    if deploy.current().ai != "on":
        return False, "this viewer does not send transcripts to a model"
    try:
        _client()
    except Unavailable as exc:
        return False, str(exc)
    return True, ""


def available() -> bool:
    """Whether the controls should be offered at all."""
    return status()[0]


def _stream(
    client,
    system: str,
    messages: list[dict],
    max_tokens: int,
    thinking: bool = True,
) -> Iterator[tuple[str, str]]:
    """One request, yielding ("thinking" | "text", piece) as it arrives.

    Two kinds, not one, because thinking produces no text: a model that thinks
    for twenty seconds before its first word looks identical to a hang. The
    caller can say "thinking" while that is what is happening.

    `thinking` is off for work that does not need it — a two-sentence
    description of one tool call is delayed, not improved, by deliberation.
    """
    import anthropic

    request: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if thinking:
        request["thinking"] = {"type": "adaptive"}

    try:
        with client.messages.stream(**request) as stream:
            for event in stream:
                if event.type != "content_block_delta":
                    continue
                kind = getattr(event.delta, "type", "")
                if kind == "thinking_delta":
                    yield "thinking", event.delta.thinking
                elif kind == "text_delta":
                    yield "text", event.delta.text
            message = stream.get_final_message()
    except anthropic.NotFoundError as exc:
        raise Unavailable(f"Model {MODEL} is not available to this account.") from exc
    except anthropic.RateLimitError as exc:
        raise Unavailable("Rate limited by the API — try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise Unavailable(f"The API refused that request: {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise Unavailable("Could not reach the API.") from exc

    # Raised after the text, so a refusal is not silently shown as an answer.
    if message.stop_reason == "refusal":
        raise Unavailable("The model declined to answer that.")


def _text_only(pieces: Iterator[tuple[str, str]]) -> str:
    """Collect a stream into the answer, dropping the thinking."""
    return "".join(text for kind, text in pieces if kind == "text").strip()


def _clip(value: Any, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2)[:limit]
    return text[:limit]


# ---------------------------------------------------------------- one call ---

CALL_SYSTEM = """You explain a single step from an agent transcript to someone \
reviewing it later.

Two or three sentences. Start with the call itself. What's it asking, in layperson terms? Then \
explain what actually came back — including whether it failed. No markdown \
headings."""


def _call_prompt(call: dict, output: Any) -> str:
    parts = [
        f"Tool: {call.get('function_name', 'unknown')}",
        f"Arguments:\n{_clip(call.get('arguments', {}))}",
    ]
    parts.append(f"Output:\n{_clip(output, 6000)}" if output else "Output: (none recorded)")
    return "\n\n".join(parts)


def summarise_call_stream(call: dict, output: Any = None) -> Iterator[tuple[str, str]]:
    """Explain one tool call, a piece at a time.

    The client is built before returning, so a missing credential is an error
    the caller sees immediately rather than one that surfaces mid-stream.
    Thinking is off: this is a description, and the first word should be quick.
    """
    client = _client()
    return _stream(
        client,
        CALL_SYSTEM,
        [{"role": "user", "content": _call_prompt(call, output)}],
        max_tokens=1000,
        thinking=False,
    )


def summarise_call(call: dict, output: Any = None) -> str:
    """Explain one tool call and its result."""
    return _text_only(summarise_call_stream(call, output))


# ------------------------------------------------------------------- ask -----

ASK_SYSTEM = """You answer questions about an agent transcript, for someone \
reviewing what happened.

Answer only from the steps given. Cite the step numbers you used, as (step 42). \
If the steps do not contain the answer, say so plainly rather than guessing — \
they are a relevant-looking subset, not the whole transcript, so "it is not in \
what I was shown" is a useful answer.

This is a conversation. Each turn brings a fresh selection of steps chosen for \
that question, so steps quoted earlier may not be in front of you now; rely on \
what you said before rather than pretending to re-read them. A follow-up like \
"why?" refers to the previous answer.

Be direct and concrete. No preamble."""

# Enough for a real conversation, bounded so a long one cannot grow without
# limit — each turn already carries a fresh page of steps.
HISTORY_TURNS = 8
HISTORY_CHARS = 4000


def _text_of(step: dict) -> str:
    """Everything in a step a reader could have seen."""
    parts: list[str] = []
    message = step.get("message")
    if isinstance(message, str):
        parts.append(message)
    elif isinstance(message, list):
        parts += [p.get("text", "") for p in message if isinstance(p, dict)]
    if step.get("reasoning_content"):
        parts.append(step["reasoning_content"])
    for call in step.get("tool_calls") or []:
        parts.append(call.get("function_name", ""))
        parts.append(json.dumps(call.get("arguments", {})))
    for result in (step.get("observation") or {}).get("results") or []:
        content = result.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(p for p in parts if p)


def _fallback(steps: list[dict], limit: int, focus: list[int] | None) -> list[dict]:
    """Nothing in the question matched anything in the transcript.

    A follow-up is almost always about what the last answer read, so those steps
    are the better guess than an arbitrary slice. Only a question with no prior
    turn to lean on gets the closing steps.
    """
    if focus:
        wanted = set(focus)
        carried = [s for s in steps if s.get("step_id") in wanted]
        if carried:
            return carried[:limit]
    # Latest first: this is a recency guess, so if the budget forces a trim it
    # should give up the oldest steps, not the newest.
    return list(reversed(steps[-limit:]))


def _ranked_steps(
    question: str,
    steps: list[dict],
    limit: int = ASK_STEPS,
    focus: list[int] | None = None,
) -> list[dict]:
    """The candidate steps, best first.

    Priority order rather than document order, so that a budget trim gives up
    the least useful steps instead of whichever happen to come last.
    """
    words = {w for w in re.findall(r"[a-zA-Z_][\w./-]{2,}", question.lower())}

    # A question says "authentication" where the transcript says "test_auth.py",
    # so a long word also scores, at a discount, on its first four characters.
    # Cheaper than tokenising every step, and wrong guesses only mis-rank.
    stems = {w: w[:4] for w in words if len(w) > 6}

    scored = []
    for step in steps:
        haystack = _text_of(step).lower()
        score = 0
        for word in words:
            if word in haystack:
                score += len(word)
            elif word in stems and stems[word] in haystack:
                score += 2
        if score:
            scored.append((score, step.get("step_id", 0), step))

    # "Nothing matched" is the condition that matters, not "no words": a
    # question can be all common words and still hit nothing.
    if not scored:
        return _fallback(steps, limit, focus)

    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:limit]]


def relevant_steps(
    question: str,
    steps: list[dict],
    limit: int = ASK_STEPS,
    focus: list[int] | None = None,
) -> list[dict]:
    """The steps most likely to bear on the question, in document order.

    Scored by how many of the question's words a step mentions, preferring
    longer words, then sorted chronologically so an answer reads in order.
    Only reached when a transcript is too large to send whole.
    """
    return sorted(
        _ranked_steps(question, steps, limit, focus),
        key=lambda s: s.get("step_id", 0),
    )


def _block(step: dict) -> str:
    body = _text_of(step)[:STEP_CLIP]
    return f"--- step {step.get('step_id')} ({step.get('source')}) ---\n{body}"


def _ask_prompt(
    question: str, steps: list[dict], focus: list[int] | None = None
) -> tuple[str, list[int]]:
    """The prompt, and the step numbers that went into it.

    When the whole transcript fits, the whole transcript goes. Choosing forty
    steps out of a session that would fit entire only throws information away,
    and most sessions are that size — selection is the exception, not the rule.
    """
    whole = [_block(s) for s in steps]
    if sum(len(b) + 2 for b in whole) <= ASK_BUDGET_CHARS:
        chosen = steps
    else:
        # Trimmed in priority order, then read back in document order: what
        # survives the budget should be the most useful steps, but the model
        # should still see them chronologically.
        kept: list[dict] = []
        budget = ASK_BUDGET_CHARS
        for step in _ranked_steps(question, steps, focus=focus):
            size = len(_block(step))
            # Skipped, not stopped at: one oversized step used to truncate every
            # step after it, including small ones that would have fitted.
            if size > budget:
                continue
            budget -= size
            kept.append(step)
        chosen = sorted(kept, key=lambda s: s.get("step_id", 0))

    used = [s.get("step_id", 0) for s in chosen]
    body = "\n\n".join(_block(s) for s in chosen)
    return f"Question: {question}\n\nSteps:\n\n{body}", used


def _history(turns: list[dict] | None) -> list[dict]:
    """Prior questions and answers as messages.

    The steps that were sent with an earlier question are deliberately not
    replayed: they are already digested into the answer, and re-sending a page
    of transcript per turn would make a long conversation quadratic.
    """
    messages: list[dict] = []
    for turn in (turns or [])[-HISTORY_TURNS:]:
        question = str(turn.get("q") or "").strip()[:HISTORY_CHARS]
        answer = str(turn.get("a") or "").strip()[:HISTORY_CHARS]
        if not question or not answer:
            continue
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    return messages


def ask_stream(
    question: str,
    steps: list[dict],
    history: list[dict] | None = None,
    focus: list[int] | None = None,
) -> tuple[list[int], Iterator[tuple[str, str]]]:
    """The steps being used, and the answer as it arrives.

    The steps are known before the first token, so the viewer can say what it is
    reading while the answer is still being written.
    """
    client = _client()
    prompt, used = _ask_prompt(question, steps, focus)
    messages = [*_history(history), {"role": "user", "content": prompt}]
    return used, _stream(client, ASK_SYSTEM, messages, max_tokens=4000)


def ask(
    question: str,
    steps: list[dict],
    history: list[dict] | None = None,
    focus: list[int] | None = None,
) -> tuple[str, list[int]]:
    """Answer a question about a transcript. Returns the answer and steps used."""
    used, chunks = ask_stream(question, steps, history, focus)
    return _text_only(chunks), used
