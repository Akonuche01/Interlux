"""Anthropic provider (true SSE streaming over stdlib urllib)."""

import logging
from typing import AsyncIterator

from .base import BaseProvider, BROWSER_UA
from .media import anthropic_blocks, flatten_messages
from .streaming import anthropic_chunks, anthropic_reasoning, anthropic_usage, post_sse

# A module that declares DIALECT is a wire format; the engine finds formats by
# scanning this package. See dialects.py.
DIALECT = "anthropic"
CANONICAL_BASE = "https://api.anthropic.com/v1"

logger = logging.getLogger("providers.anthropic")

# Anthropic's extended-thinking budget. Must be at least 1024 and strictly
# less than `max_tokens`, which is 4096 below. The value lives here because
# it is a fact about this vendor's API; it reaches the request through the
# provider's own `options` block in the user's config.
THINKING_BUDGET = 2048


class AnthropicProvider(BaseProvider):
    # Thinking is enabled by the provider's own `options` in the user's
    # config, e.g. {"thinking": {"type": "enabled", "budget_tokens": 2048}}.
    # `anthropic_reasoning` has always been able to read Claude's thinking
    # blocks; nothing ever asked for them, so the thought section of the
    # client's card stayed empty here exactly as it did on Mercury. Same
    # defect, same class -- and it is fixed in the same place for both,
    # which is the point of having one place.

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
        # The base URL comes from the provider's config section, or from its
        # format's canonical home when the config names none. Never from a
        # vendor name hardcoded here -- see dialects.py.
        base = (self.base_url or "").rstrip("/")
        if not base:
            yield {"type": "error",
                   "message": "no base_url configured for this provider"}
            yield {"type": "complete"}
            return
        url = f"{base}/messages"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": BROWSER_UA,
        }
        # The whole conversation, not just the last message -- see the note in
        # openai.py. Reading `messages[-1]` discarded the thread's history.
        text = flatten_messages(messages)
        if not isinstance(text, str):
            text = ""
        payload = {
            "model": model or "claude-3-5-sonnet-20241022",
            "messages": anthropic_blocks(text, images or []),
            "max_tokens": 4096,
            "stream": stream,
        }
        payload.update(self.contract_fields())
        if "thinking" not in payload:
            payload["temperature"] = temperature
        # else: Anthropic refuses a modified temperature alongside extended
        # thinking, so the default is dropped rather than the turn failing.
        # A vendor constraint, handled where the vendor is known; the engine
        # never sees it and no other provider is affected.

        logger.info(f"Connecting to {url}")
        try:
            async for delta in post_sse(
                url, headers, payload, anthropic_chunks,
                usage_from=anthropic_usage,
                reasoning_from=anthropic_reasoning,
            ):
                yield delta
            yield {"type": "complete"}
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            yield {"type": "error", "message": str(e)}
            yield {"type": "complete"}


ADAPTER = AnthropicProvider
