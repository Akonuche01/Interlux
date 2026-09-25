"""Provider base."""

from abc import ABC, abstractmethod
from typing import AsyncIterator


class BaseProvider(ABC):
    """Abstract provider interface."""

    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def stream_turn(
        self, messages: list[dict], temperature: float = 0.7, model: str = ""
    ) -> AsyncIterator[dict]:
        """Yield delta events."""
        ...
