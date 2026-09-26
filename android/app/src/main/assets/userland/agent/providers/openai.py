"""OpenAI-compatible provider: chat completions + Responses API, streamed or not."""

import logging
from typing import AsyncIterator

from .base import BaseProvider
from .media import openai_blocks
from .responses import ResponsesUnsupported, post_responses
from .streaming import openai_chunks, post_sse

logger = logging.getLogger("providers.openai")


class OpenAIProvider(BaseProvider):
    async def stream_turn(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        model: str = "",
        images: list[str] | None = None,
        api: str = "chat",
        stream: bool = True,
    ) -> AsyncIterator[dict]:
        base = self.base_url or "https://api.openai.com/v1"
        use_model = model or "gpt-4o"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        text = messages[-1].get("content", "") if messages else ""
        if not isinstance(text, str):
            text = ""

        if api == "responses":
            logger.info(f"Connecting (responses) to {base}")
            try:
                async for delta in post_responses(
                    base, headers, use_model, text, images or [], stream
                ):
                    yield delta
                yield {"type": "complete"}
                return
            except ResponsesUnsupported:
                logger.info("No /responses route, falling back to chat")
            except Exception as e:
                logger.error(f"Responses API error: {e}")
                yield {"type": "error", "message": str(e)}
                yield {"type": "complete"}
                return

        url = f"{base}/chat/completions"
        payload = {
            "model": use_model,
            "messages": openai_blocks(text, images or []),
            "temperature": temperature,
            "stream": stream,
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
