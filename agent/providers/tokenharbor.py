"""Token Harbor gateway (OpenAI-compatible chat completions)."""

from typing import AsyncIterator

from .openai import OpenAIProvider


class TokenHarborProvider(OpenAIProvider):
    DEFAULT_BASE = "https://tokenharbor.ai/v1"
    DEFAULT_MODEL = "deepseek-v4-flash:free"

    async def stream_turn(
        self, messages: list[dict], temperature: float = 0.7, model: str = ""
    ) -> AsyncIterator[dict]:
        if not self.base_url:
            self.base_url = self.DEFAULT_BASE
        async for delta in super().stream_turn(
            messages, temperature, model or self.DEFAULT_MODEL
        ):
            yield delta
