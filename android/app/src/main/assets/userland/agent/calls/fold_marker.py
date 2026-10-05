"""Tool calls written inside the engine's own fold markers.

`threads.fold_output` wraps a tool's result in `[name] … [/name]` so the
client's rebuild can split it back out into activity rows. Those markers ride
into the thread history, and the model reads its own history: it learnt the
shape and began writing its *calls* in it —

    [shell {"tool": "shell", "parameters": {"command": "ps"}}]

— which no fence dialect accepts. The live consequence was a turn that parsed
to zero calls: the tool never ran, and the raw call was broadcast to the user
as the answer. Two separate defects, and this module is the second one.

`threads.split_tool_sections()` now lifts the markers out of the assistant's own
message before the history reaches the model, so the temptation is gone. That is
the cause; this is the net. A model may still write it, and the next provider may
write something else again — so the shape is a language like any other, and
adding it is this file.

Three spellings are covered, because a model that half-remembers a shape
produces all three:

    [shell] {json} [/shell]     both markers, name in brackets
    [shell] {json}              opener only — a stream that ended mid-call
    [shell {json}]              name, then the JSON, then the `]`

The third is the one that actually happened, and it is the one no
bracket-after-the-name pattern can see: the `]` comes *after* the JSON, so the
opener is not `[shell]` at all.

`RUN_WHEN = "no-calls"`: these markers are the engine's own bookkeeping, so a
round that also carried a real fence has already been served. Its syntax is
still stripped either way — raw markers must never reach the chat window — but
it does not execute a second time.
"""

from __future__ import annotations

import json
import re

from .base import CallParse
from .json_shapes import normalize_calls

DIALECT = "fold-marker"
# Before `json`, deliberately. `json`'s bare-payload path finds the call inside
# `[shell {…}]` too — it decodes the object and strips just that — and what it
# leaves behind is the marker debris `[shell ] … [/shell]`, which then reaches
# the chat window. Only this dialect knows the markers are part of the span, so
# it has to see the text first. Priority is the knob for exactly that.
PRIORITY = 5
RUN_WHEN = "no-calls"

# `[shell] … [/shell]`. The `[ \t]*` before each `]` is load-bearing: once the
# JSON is taken out of `[shell {…}]` the opener reads `[shell ]` — with a space
# — which a pattern demanding `]` straight after the name would miss. That
# exact miss is what let a marker survive into the chat.
_OPEN_MARK = re.compile(r"\[([a-z_][a-z0-9_.-]*)[ \t]*\]")
_CLOSE_MARK = re.compile(r"\[/([a-z_][a-z0-9_.-]*)[ \t]*\]")
_PAIR = re.compile(
    r"\[([a-z_][a-z0-9_.-]*)[ \t]*\]\s*(.*?)\s*\[/\1[ \t]*\]", re.S)
# An opener with no closer, which is what a stream that ended mid-call leaves.
_UNCLOSED = re.compile(r"\[([a-z_][a-z0-9_.-]*)[ \t]*\]\s*(\{.*)", re.S)
# `[shell {` — a name followed by the call JSON. Lookahead so the `{` is not
# consumed; the JSON is decoded from there to find where the call really ends.
_INLINE_OPEN = re.compile(r"\[([a-z_][a-z0-9_.-]*)[ \t]+(?=\{)")
# Whatever closes an inline marker: `]`, and optionally a `[/shell]` after it.
_INLINE_TAIL = re.compile(
    r"[ \t]*\](?:[ \t]*\[/[a-z_][a-z0-9_.-]*[ \t]*\])?")


def _inline_calls(text: str) -> tuple[list[dict], list[tuple[int, int]]]:
    """`[shell {…}]` — the name, then the JSON, then the `]`."""
    decoder = json.JSONDecoder()
    calls: list[dict] = []
    spans: list[tuple[int, int]] = []
    for m in _INLINE_OPEN.finditer(text):
        try:
            value, end = decoder.raw_decode(text, m.end())
        except Exception:
            continue
        got = normalize_calls(value)
        if not got:
            continue
        tail = _INLINE_TAIL.match(text, end)
        calls.extend(got)
        spans.append((m.start(), tail.end() if tail else end))
    return calls, spans


def parse(text: str) -> CallParse:
    """Calls out of `[name] …` markers, and the markers taken out."""
    if not text or "[" not in text:
        return CallParse(calls=[], text=text, found=False, broke=False)

    calls: list[dict] = []
    spans: list[tuple[int, int]] = []

    for m in _PAIR.finditer(text):
        got = normalize_calls(_decoded(m.group(2)))
        if got:
            calls.extend(got)
            spans.append(m.span())

    if not calls:
        for m in _UNCLOSED.finditer(text):
            got = normalize_calls(_decoded(m.group(2)))
            if got:
                calls.extend(got)
                spans.append(m.span())

    if not calls:
        calls, spans = _inline_calls(text)

    if not calls:
        # Markers with nothing call-shaped inside them. Not this language's
        # call — leave the text for the next dialect, and for the answer.
        return CallParse(calls=[], text=text, found=False, broke=False)

    cleaned = text
    for start, end in sorted(spans, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    # Any marker left over (an unmatched half of a pair) is fold syntax, not
    # an answer, and must not reach the chat window.
    cleaned = _CLOSE_MARK.sub("", _OPEN_MARK.sub("", cleaned))

    return CallParse(calls=calls, text=cleaned.strip(), found=True, broke=False)


def _decoded(body: str):
    """The JSON inside a marker body, or None."""
    from .json_shapes import json_span
    return json_span(body)


ADAPTER = parse
