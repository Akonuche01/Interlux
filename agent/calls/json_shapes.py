"""The JSON family: every way a model spells a call in JSON.

One dialect, not several, because these spellings are not independent
languages -- they are one language written carelessly, and the order they
are tried in is part of the behaviour:

  1. closed ```json fences -- all of them, since a round can carry several
  2. an unclosed trailing fence -- Atria writes the opener, the JSON, and
     then simply stops
  3. no fence at all -- deliberately strict: a JSON *list* of objects that
     each name a tool, nothing looser

Step 3 is strict on purpose. The prompt asks for the fence, so an unfenced
payload is rare, and requiring the exact documented shape is what keeps a
code sample inside a real answer from being executed as a tool request.

Within any of those, the call object itself is accepted in the several
shapes models write: the documented {"tool", "parameters"}, a bare object, a
{"tool_calls": [...]} wrapper, the OpenAI wire naming (name/arguments), and
a flat object carrying its parameters as sibling keys. Anything without a
tool name is not a call and is dropped -- that guard is what keeps ordinary
JSON in an answer from being executed.
"""

from __future__ import annotations

import json
import re

from .base import CallParse

DIALECT = "json"
PRIORITY = 10
RUN_WHEN = "always"

_CLOSED_FENCE = re.compile(r"```[ \t]*json[ \t]*\r?\n?(.*?)```", re.DOTALL)
_OPEN_FENCE = re.compile(r"```[ \t]*json[ \t]*\r?\n?(.*)$", re.DOTALL)


def normalize_calls(parsed) -> list[dict]:
    """One call shape out of the several a model writes.

    Anything without a tool name is not a call and is dropped -- that guard
    is what keeps ordinary JSON in an answer from being executed as a tool
    request.
    """
    if isinstance(parsed, dict):
        inner = parsed.get("tool_calls")
        parsed = inner if isinstance(inner, list) else [parsed]
    if not isinstance(parsed, list):
        return []
    out: list[dict] = []
    skip = {"tool", "name", "tool_name", "parameters", "arguments", "params",
            "tool_calls"}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        # OpenAI's nested wire shape: the name and the arguments live under
        # `function`, and `arguments` is usually a JSON *string*. Unwrapped
        # here so every shape below reads the same way. Without it a provider
        # that speaks the OpenAI dialect parsed to zero calls.
        fn = item.get("function")
        if isinstance(fn, dict):
            item = {**fn, **{k: v for k, v in item.items() if k != "function"}}
        tool = item.get("tool") or item.get("name") or item.get("tool_name")
        if not isinstance(tool, str) or not tool:
            continue
        params = item.get("parameters")
        if params is None:
            params = item.get("arguments")
        if params is None:
            params = item.get("params")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, ValueError):
                params = {}
        if not isinstance(params, dict):
            params = {}
        if not params:
            # Flat shape: parameters ride as sibling keys.
            params = {k: v for k, v in item.items() if k not in skip}
        out.append({"tool": tool, "parameters": params})
    return out


def json_span(text: str):
    """The first parseable JSON array/object inside `text`, or None."""
    for opener, closer in (("[", "]"), ("{", "}")):
        i = text.find(opener)
        j = text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def parse(text: str) -> CallParse:
    calls: list[dict] = []
    cleaned = text
    found = False
    broke = False

    blocks = _CLOSED_FENCE.findall(text)
    if blocks:
        found = True
        cleaned = _CLOSED_FENCE.sub("", text)
        for block in blocks:
            calls.extend(normalize_calls(json_span(block)))
            if not calls and block.strip():
                # Fenced, but not even valid JSON (or valid JSON with no
                # call in it): an attempt that failed, not an answer that
                # happens to contain a fence. A code sample that decodes
                # to non-call JSON stays an answer.
                broke = json_span(block) is None
    else:
        # Unclosed fence: opener through the end of the round.
        m = _OPEN_FENCE.search(text)
        if m:
            found = True
            cleaned = text[:m.start()]
            calls.extend(normalize_calls(json_span(m.group(1))))
            if not calls and m.group(1).strip():
                broke = json_span(m.group(1)) is None
        else:
            parsed = json_span(text)
            # A bare payload naming a tool, as a list of calls OR a single
            # object. The object case was missing, so `{"name": "shell",
            # "arguments": {...}}` -- the OpenAI wire naming, written on its
            # own -- parsed to zero calls and the raw call was broadcast as
            # the answer.
            #
            # The guard is `normalize_calls` itself rather than a name check
            # here: it already drops anything without a tool name, and it is
            # the only place that knows every spelling of one (`tool`, `name`,
            # `tool_name`, and the OpenAI `function` wrapper). A second,
            # narrower check in front of it is what kept the nested form from
            # ever being seen.
            items = parsed if isinstance(parsed, list) else [parsed]
            if parsed and all(isinstance(x, dict) for x in items):
                calls = normalize_calls(parsed)
                if calls:
                    if isinstance(parsed, list):
                        i, j = text.find("["), text.rfind("]")
                    else:
                        i, j = text.find("{"), text.rfind("}")
                    cleaned = text[:i] + text[j + 1:]
                    found = True

    return CallParse(calls=calls, text=cleaned, found=found, broke=broke)


ADAPTER = parse
