"""On-device backend: llama-server on loopback (OpenAI-compatible)."""

from typing import AsyncIterator

from .openai import OpenAIProvider


class LocalProvider(OpenAIProvider):
    DEFAULT_BASE = "http://127.0.0.1:4602/v1"
    DEFAULT_MODEL = "local"

    async def stream_turn(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        model: str = "",
        images: list[str] | None = None,
    ) -> AsyncIterator[dict]:
        if not self.base_url:
            self.base_url = self.DEFAULT_BASE
        # llama-server needs no key; send something non-empty regardless.
        if not self.api_key:
            self.api_key = "local"
        async for delta in super().stream_turn(
            messages, temperature, model or self.DEFAULT_MODEL, images
        ):
            yield delta
