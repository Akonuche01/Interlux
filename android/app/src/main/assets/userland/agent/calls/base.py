"""The tool-call contract: what a call language must produce.

The model does not call tools. It writes text, and the engine parses a name
and a parameter object out of that text. Which *shape* of text is not the
engine's business and must never be fixed to one spelling: free-tier models
ignore the documented convention and write XML, some write the OpenAI wire
naming, some write a bare object, some write a fence and forget to close it.
A new provider whose model speaks a shape the engine has not met is not a
provider problem -- it is a missing dialect, and adding one must be dropping
a module in beside the others.

So a module in this package that declares `DIALECT` is a call language, and
the engine asks each one to describe itself:

    DIALECT    a name, for diagnostics and tests
    PRIORITY   the order it is tried in; lower first
    RUN_WHEN   "always", or "no-calls" for a language whose calls must not
               execute once another has already parsed one -- which is what
               stops a round carrying both a fence and XML from running
               twice. Its matched syntax is still stripped either way: raw
               syntax must never reach the chat window, and stripping is
               not executing.
    parse(text) -> CallParse

`parse` never raises for text that is simply not its language: it returns a
`CallParse` with `found=False` and the text untouched, and the next dialect
gets a turn. That is the whole contract, and it is deliberately small so
that adding a language is a small thing to do.

Nothing is enumerated centrally. `discover()` walks this package and
collects the dialects, exactly as `providers/dialects.py` collects wire
formats -- one pattern for both, because they are the same problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A tool name the engine will accept from a model. The dispatcher's own
# name check, kept here so every dialect validates against one rule.
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@dataclass
class CallParse:
    """What one dialect made of one round of model text.

    calls   the calls it parsed, in the documented
            [{"tool": ..., "parameters": {...}}] shape
    text    the text with the syntax it matched removed
    found   call-shaped text was seen, so it must be stripped even when
            nothing parsed -- raw syntax must never reach the chat window
    broke   narrower than `found`: the text was an *attempt* that failed
            (a fence whose content is not even JSON, an XML span that
            yielded no call). Not an answer that happens to contain a code
            sample -- valid JSON with no call shape stays an answer. The
            caller spends one round asking for a clean re-emit on `broke`.
    """

    calls: list = field(default_factory=list)
    text: str = ""
    found: bool = False
    broke: bool = False


@dataclass(frozen=True)
class CallDialect:
    """One call language, as it describes itself. Data only."""

    name: str
    parse: object
    priority: int = 100
    run_when: str = "always"
    module: str = ""
