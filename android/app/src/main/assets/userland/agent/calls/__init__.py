"""Tool-call languages -- discovered, never listed.

The model writes text; the engine parses a call out of it. Which spelling of
text is not something the engine may fix to one shape, because the model
behind any given provider is free to write another: XML instead of JSON, the
OpenAI wire naming instead of ours, a fence without its closing fence, a bare
object. A provider that changes how its model writes calls must not be able
to break the engine -- and it must not require an engine change either.

So a module in this package that declares `DIALECT` is a call language, and
`discover()` collects them by asking, exactly as `providers/dialects.py`
collects wire formats. Adding a language is dropping a module in beside
these. Nothing is enumerated centrally, in either place, because it is the
same problem twice and it deserves the same answer twice.

`extract()` runs them in priority order. Each dialect sees the text the
previous ones left; a dialect declared `RUN_WHEN = "no-calls"` is skipped
once a call has been parsed, which is what stops a round carrying both a
fence and XML from executing twice.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import TOOL_NAME_RE, CallDialect, CallParse


def discover(package: str | None = None) -> list[CallDialect]:
    """Every call language declared in `package`, in the order to try them.

    `package` is a parameter so a test can point the scan at a throwaway
    package and prove that a language added later is picked up without the
    engine being edited.
    """
    package = package or __package__
    root = Path(importlib.import_module(package).__file__ or "").parent
    found: list[CallDialect] = []
    for info in sorted(pkgutil.iter_modules([str(root)]),
                       key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        dialect = getattr(module, "DIALECT", None)
        if not isinstance(dialect, str) or not dialect:
            continue                      # a helper, not a language
        parser = getattr(module, "ADAPTER", None)
        if not callable(parser):
            raise ValueError(
                f"calls/{info.name}.py declares DIALECT '{dialect}' but no "
                f"ADAPTER; a language must say what parses it"
            )
        found.append(CallDialect(
            name=dialect,
            parse=parser,
            priority=int(getattr(module, "PRIORITY", 100)),
            run_when=str(getattr(module, "RUN_WHEN", "always")),
            module=info.name,
        ))
    return sorted(found, key=lambda d: (d.priority, d.name))


DIALECTS: list[CallDialect] = discover()


def extract(text: str) -> tuple[list, str, bool, bool]:
    """(calls, cleaned_text, found_syntax, broke_syntax) for one round.

    `found_syntax` means call-shaped text was seen, so it must be stripped
    even when nothing parsed. `broke_syntax` is narrower: an attempt that
    failed rather than an answer that happens to contain a code sample. The
    caller spends one round asking for a clean re-emit on `broke`.

    Dialects run in priority order, each seeing what the last one left. A
    dialect that does not recognise the text returns it untouched, so the
    next one gets a fair look -- which is what makes a language the engine
    has never met a missing module rather than a broken turn.
    """
    if not text:
        return [], text, False, False
    calls: list[dict] = []
    cleaned = text
    found = False
    broke = False
    for dialect in DIALECTS:
        result = dialect.parse(cleaned)
        if result is None:
            continue
        # Stripping always applies, whichever language owns the round.
        cleaned = result.text
        found = found or result.found
        if dialect.run_when == "no-calls" and calls:
            # Another language already parsed a call this round. This one's
            # matched syntax still comes out of the text -- raw syntax must
            # never reach the chat window -- but it does not execute, which
            # is what stops a round carrying two languages from running
            # twice. It is not a failed attempt either, so `broke` stays
            # clear: the round did produce a call.
            continue
        calls.extend(result.calls)
        broke = broke or result.broke
    return calls, cleaned.strip(), found, broke


__all__ = ["DIALECTS", "CallDialect", "CallParse", "TOOL_NAME_RE", "discover",
           "extract"]
