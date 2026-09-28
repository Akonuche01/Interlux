"""Provider base."""

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
    """Abstract provider interface."""

    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = base_url

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
        api: "chat" or "responses". stream: live deltas or one shot."""
        ...
