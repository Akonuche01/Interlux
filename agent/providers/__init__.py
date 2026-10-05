"""Provider adapters.

There is no registry of providers. A provider is whatever the user
configured -- a name, a base URL, a key -- and the engine serves it, so a
vendor the code has never heard of works the moment it is configured, and
a vendor the user has no key for is never offered.

What lives here is the **wire format**: how a vendor's bytes become the
vocabulary in `base.py`. `dialects.py` finds the formats by asking each
module in this package to describe itself, and turns a config section into
an adapter. Adding a provider is adding a config section -- no code at all.
Adding a *format* is dropping a self-describing module in beside these.

The vocabulary is the same for everyone, and only the dialect differs:

    text_delta      the answer, as it streams
    reasoning_delta the model's thinking
    usage           token accounting
    error           a failure, stated plainly
    complete        end of stream

`reasoning_delta` is not optional. The client's thought section is built
from it and from nothing else, so an adapter that drops it removes a
feature from the app -- silently, and only for the users of that one
provider. `post_sse` therefore takes its reader as a required parameter:
forgetting one fails at import instead of quietly emptying a card.
"""

from .base import BaseProvider
from .dialects import (CANONICAL_BASE, DEFAULT_DIALECT, DIALECTS, DialectSpec,
                       UnknownDialect, base_for, build_provider, dialect_of,
                       discover)

__all__ = [
    "BaseProvider",
    "CANONICAL_BASE",
    "DEFAULT_DIALECT",
    "DIALECTS",
    "DialectSpec",
    "UnknownDialect",
    "base_for",
    "build_provider",
    "dialect_of",
    "discover",
]
