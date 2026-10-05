"""OpenAI-compatible provider: chat completions + Responses API, streamed or not."""

import logging
from typing import AsyncIterator

from .base import BaseProvider, BROWSER_UA
from .media import flatten_messages, openai_blocks
from .responses import ResponsesUnsupported, post_responses
from .streaming import (make_openai_reasoning, openai_chunks, openai_usage,
                        post_sse)

# A module that declares DIALECT is a wire format. The engine finds formats by
# scanning this package and asking each module to describe itself, so nothing
# is listed anywhere centrally -- see dialects.py. Dropping a module in here
# that speaks a new format makes the engine speak it, with no other edit.
DIALECT = "openai"
CANONICAL_BASE = "https://api.openai.com/v1"

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
        # The base URL comes from the provider's config section, or from its
        # format's canonical home when the config names none. Never from a
        # vendor name hardcoded here -- see dialects.py.
        base = (self.base_url or "").rstrip("/")
        use_model = model or "gpt-4o"
        if not base:
            yield {"type": "error",
                   "message": "no base_url configured for this provider"}
            yield {"type": "complete"}
            return
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
        }
        # The WHOLE conversation, not just the last message. Reading
        # `messages[-1]` here discarded the history `build_messages` assembled,
        # so the model started every turn amnesiac.
        text = flatten_messages(messages)
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
        if stream:
            # Ask for the terminal usage chunk (servers that do not
            # understand it ignore it; usage is then simply absent).
            payload["stream_options"] = {"include_usage": True}
        # Whatever this vendor needs in order to deliver the contract --
        # empty for a service that returns the whole vocabulary unasked.
        # See `contract_fields` in base.py for what may and may not go here.
        payload.update(self.contract_fields())

        logger.info(f"Connecting to {url}")
        try:
            async for delta in post_sse(
                url, headers, payload, openai_chunks, usage_from=openai_usage,
                reasoning_from=make_openai_reasoning(),
            ):
                yield delta
            yield {"type": "complete"}
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            yield {"type": "error", "message": str(e)}
            yield {"type": "complete"}


ADAPTER = OpenAIProvider
