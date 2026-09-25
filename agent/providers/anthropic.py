"""Anthropic provider."""

import json
import logging
import urllib.request
import ssl
from typing import AsyncIterator

from .base import BaseProvider

logger = logging.getLogger("providers.anthropic")


class AnthropicProvider(BaseProvider):
    async def stream_turn(
        self, messages: list[dict], temperature: float = 0.7
    ) -> AsyncIterator[dict]:
        url = f"{self.base_url or 'https://api.anthropic.com/v1'}/messages"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": "claude-3-5-sonnet-20241022",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 4096,
            "stream": False,
        }

        logger.info(f"Connecting to {url}")
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            context = ssl.create_default_context()
            with urllib.request.urlopen(req, context=context, timeout=60) as response:
                logger.info(f"Response status: {response.status}")
                body = response.read().decode("utf-8")
                logger.info(f"Response text: {body[:500]}")
                data = json.loads(body)
                if "content" in data:
                    for block in data["content"]:
                        if block.get("type") == "text" and block.get("text"):
                            yield {"type": "text_delta", "content": block["text"]}
                yield {"type": "complete"}
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            yield {"type": "error", "message": str(e)}
            yield {"type": "complete"}
