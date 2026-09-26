"""Anthropic provider (true SSE streaming over stdlib urllib)."""

import logging
from typing import AsyncIterator

from .base import BaseProvider
from .media import anthropic_blocks
from .streaming import anthropic_chunks, post_sse

logger = logging.getLogger("providers.anthropic")


class AnthropicProvider(BaseProvider):
    # Anthropic Messages IS the respond-style API; api= is accepted for a
    # uniform interface and ignored.
    async def stream_turn(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        model: str = "",
        images: list[str] | None = None,
        api: str = "chat",
        stream: bool = True,
    ) -> AsyncIterator[dict]:
        url = f"{self.base_url or 'https://api.anthropic.com/v1'}/messages"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        text = messages[-1].get("content", "") if messages else ""
        if not isinstance(text, str):
            text = ""
        payload = {
            "model": model or "claude-3-5-sonnet-20241022",
            "messages": anthropic_blocks(text, images or []),
            "temperature": temperature,
            "max_tokens": 4096,
            "stream": stream,
        }

        logger.info(f"Connecting to {url}")
        try:
            async for delta in post_sse(url, headers, payload, anthropic_chunks):
                yield delta
            yield {"type": "complete"}
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            yield {"type": "error", "message": str(e)}
            yield {"type": "complete"}
