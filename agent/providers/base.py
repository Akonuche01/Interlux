"""Provider base -- the contract every adapter must satisfy.

A provider is a transport for tokens. The user and the config choose which
one, and that choice decides *where* the work happens and *whose* key pays
for it. It must never decide what the engine can do.

The rule, stated once and meant literally:

    **No behaviour may depend on the provider name, the API key, or the
    host.**

Swapping one provider for another must change nothing the user can see. If
it does -- a section that stays empty, an event that never arrives, a shape
that differs -- that is a defect in the provider layer, not a property of
the vendor. Vendors genuinely differ in how they speak, and translating
those differences away *before the engine sees them* is the entire job of
an adapter. An adapter that hands a vendor's dialect on to the engine has
not done its job.

`stream_turn` therefore has one vocabulary, and every adapter must emit all
of it that applies:

    {"type": "text_delta",      "content": str}   the answer, as it streams
    {"type": "reasoning_delta", "content": str}   the model's thinking
    {"type": "usage",           "usage": dict}    token accounting
    {"type": "error",           "message": str}   a failure, stated plainly
    {"type": "complete"}                          end of stream

`reasoning_delta` is not optional, and this is the case worth being explicit
about. The client's thought section is built from `reasoning_delta` and from
nothing else, so an adapter that drops it removes a feature from the app --
silently, and only for the users of that one provider. There are two ways to
drop it: not *reading* the vendor's thinking field, and not *asking* the
vendor for it in the first place. Both are the adapter's responsibility.
`contract_fields` below is where the asking happens.

How thinking is asked for and read is the vendor's business and lives in that
vendor's adapter. *Whether* it is wanted is decided here, once, for every
provider alike.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator


# A browser-ish User-Agent on every outbound HTTP call. Python's default
# urllib UA gets a Cloudflare 1010 block from some gateways (measured on
# apinex: 403 for Python-urllib/3.x, clean for a browser UA) — with no UA
# override, turns AND roster fetches die identically on those providers.
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
)


class BaseProvider(ABC):
    """Abstract provider interface -- see the module docstring for the contract."""

    def __init__(self, api_key: str, base_url: str | None = None,
                 contract: dict | None = None):
        self.api_key = api_key
        self.base_url = base_url
        # Supplied by the provider's own config section, never by the
        # adapter and never by the vendor's name. A field a vendor needs in
        # every request is declared in the user's config under `options` --
        # see dialects.py.
        self._contract = dict(contract or {})

    def contract_fields(self) -> dict:
        """Request fields this vendor needs in order to satisfy the contract.

        Taken from the provider's own config section. Empty for any service
        that returns the whole vocabulary unasked; a vendor that returns
        part of the vocabulary *only on request* gets the ask from its
        config, not from the engine knowing which vendor it is. Both
        Anthropic and Inception return none unless asked, and both used to
        return none because nothing asked.

        This is deliberately not a place for vendor behaviour. A field
        belongs here only if the service cannot deliver the vocabulary in
        the module docstring without it. If a field would make this provider
        behave differently from the others from the user's point of view, it
        does not belong here: the contract is the same for everyone, and only
        the dialect differs.
        """
        return dict(self._contract)

    @abstractmethod
    async def stream_turn(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        model: str = "",
        images: list[str] | None = None,
        api: str = "chat",
        stream: bool = True,
    ) -> AsyncIterator[dict]:
        """Yield delta events. images: file paths or data: URLs.
        api: "chat" or "responses". stream: live deltas or one shot.

        Must emit the vocabulary in the module docstring -- in particular
        `reasoning_delta` for the model's thinking, whenever the model
        produces any and the vendor can return it.
        """
        ...
