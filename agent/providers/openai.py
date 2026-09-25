"""OpenAI-compatible provider (true SSE streaming over stdlib urllib)."""

import logging
from typing import AsyncIterator

from .base import BaseProvider
from .streaming import openai_chunks, post_sse

logger = logging.getLogger("providers.openai")


class OpenAIProvider(BaseProvider):
    async def stream_turn(
        self, messages: list[dict], temperature: float = 0.7, model: str = ""
    ) -> AsyncIterator[dict]:
        url = f"{self.base_url or 'https://api.openai.com/v1'}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or "gpt-4o",
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        logger.info(f"Connecting to {url}")
        try:
            async for delta in post_sse(url, headers, payload, openai_chunks):
                yield delta
            yield {"type": "complete"}
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            yield {"type": "error", "message": str(e)}
            yield {"type": "complete"}
