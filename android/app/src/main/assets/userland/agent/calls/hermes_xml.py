"""The Hermes XML dialect: <tool_call> ... </tool_call>.

Free-tier models disobey the fenced-JSON convention and write XML instead.
Observed flavours, all handled here: a leading tool name with
<arg_key>/<arg_value> pairs, JSON inside the tags (Hermes proper), and a bare
tag carrying the name (<shell>...</shell>).

Zero-width junk is normalized inside <...> tag regions only, before matching:
a ZW-shredded tag name would otherwise dodge the span regex and leak raw XML
to chat. Tag regions are ASCII syntax, so this never touches ZWJ emoji or
ZWNJ prose in visible text.

A span that parses to nothing still strips -- raw XML must never reach chat
-- and is warning-logged, because a call that silently never runs is worse
than a call that visibly fails.

This dialect is declared `RUN_WHEN = "no-calls"`: its calls are executed only
when no other language has already parsed one, so a round carrying both a
fence and XML cannot run twice. Its matched spans are stripped either way --
stripping is not executing, and raw XML reaching the chat window is the thing
this dialect exists to prevent. Before that rule was stated separately, a
mixed round parsed the fence, skipped this dialect entirely, and left the raw
XML in the answer.
"""

from __future__ import annotations

import json
import logging
import re

from .base import TOOL_NAME_RE, CallParse
from .json_shapes import json_span, normalize_calls

logger = logging.getLogger("calls.hermes_xml")

DIALECT = "hermes-xml"
PRIORITY = 20
RUN_WHEN = "no-calls"

# Zero-width characters models sprinkle inside call syntax (seen in the
# wild inside <tool_call> spans). Invisible and unparseable -- remove
# before parsing, never let them reach a tool name or argument.
_ZW_RE = re.compile("[\u200b\u200c\u200d\ufeff]")

_XML_CLOSED = re.compile(
    r"<tool[_ ]?call\b[^>]*>(.*?)</tool[_ ]?call\s*>",
    re.DOTALL | re.IGNORECASE,
)
_XML_OPEN = re.compile(r"<tool[_ ]?call\b[^>]*>(.*)$",
                       re.DOTALL | re.IGNORECASE)
_XML_KEY = re.compile(r"<arg_key\s*>(.*?)</arg_key\s*>",
                      re.DOTALL | re.IGNORECASE)
_XML_VAL = re.compile(r"<arg_value\s*>(.*?)</arg_value\s*>",
                      re.DOTALL | re.IGNORECASE)
# Tag regions for zero-width normalization (tags are ASCII syntax).
_TAG_RE = re.compile(r"<[^>]*>")


def split(text: str) -> tuple[list, list, str]:
    """(calls, spans, normalized_text).

    Returns the parsed calls, the matched (start, end) spans, and the
    (possibly tag-normalized) text the spans refer to.
    """
    calls: list[dict] = []
    spans: list[tuple] = []

    norm = _TAG_RE.sub(lambda m: _ZW_RE.sub("", m.group(0)), text)

    def handle(inner: str, start: int, end: int) -> None:
        cand = _ZW_RE.sub("", inner).strip()
        if not cand:
            return
        spans.append((start, end))
        got = normalize_calls(json_span(cand))
        if got:
            calls.extend(got)
            return
        keys = _XML_KEY.findall(cand)
        vals = _XML_VAL.findall(cand)
        head = re.sub(r"<[^>]+>", "",
                      _XML_KEY.split(cand, maxsplit=1)[0]).strip()
        if keys and len(keys) == len(vals) and TOOL_NAME_RE.fullmatch(head):
            calls.append({
                "tool": head,
                "parameters": {k.strip(): v.strip() for k, v in zip(keys, vals)
                               if k.strip()},
            })
            return
        m = re.match(r"\s*<([A-Za-z0-9_.-]+)\b[^>]*>(.*?)</\1\s*>\s*$",
                     cand, re.DOTALL)
        if m is None:
            # No inner closing tag (model stopped early): the remainder
            # is the body.
            m = re.match(r"\s*<([A-Za-z0-9_.-]+)\b[^>]*>(.*)$",
                         cand, re.DOTALL)
        if m:
            name, body = m.group(1), m.group(2).strip()
            # A dangling partial close is not body text.
            body = re.sub(r"</[A-Za-z0-9_.-]*\s*$", "", body).strip()
            inner_calls = normalize_calls(json_span(body))
            if inner_calls:
                calls.extend(inner_calls)
                return
            params: dict = {}
            if body:
                try:
                    parsed_body = json.loads(body)
                    params = (parsed_body
                              if isinstance(parsed_body, dict)
                              else {"text": body})
                except (json.JSONDecodeError, ValueError):
                    params = {"text": body}
            calls.append({"tool": name, "parameters": params})
            return
        logger.warning("unparseable <tool_call> span stripped: %r",
                       cand[:200])

    for m in _XML_CLOSED.finditer(norm):
        handle(m.group(1), m.start(), m.end())
    if not spans:
        m = _XML_OPEN.search(norm)
        if m:
            handle(m.group(1), m.start(), len(norm))
    return calls, spans, norm


def parse(text: str) -> CallParse:
    calls, spans, norm = split(text)
    # The spans were matched against `norm`, so they are stripped from
    # `norm` -- not from `text`. Stripping the raw text instead would use
    # offsets that no longer line up wherever a zero-width character was
    # removed, leaving raw XML in the chat window. This is exactly the bug
    # the baseline diff caught during the move.
    cleaned = norm
    if spans:
        for start, end in sorted(spans, reverse=True):
            cleaned = cleaned[:start] + cleaned[end:]
    return CallParse(
        calls=calls,
        text=cleaned,
        found=bool(spans),
        broke=bool(spans) and not calls,
    )


ADAPTER = parse
