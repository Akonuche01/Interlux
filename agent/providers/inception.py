"""Inception Labs (Mercury) provider."""

import json
import logging
import urllib.request
import ssl
from typing import AsyncIterator

from .base import BaseProvider

logger = logging.getLogger("providers.inception")


class InceptionProvider(BaseProvider):
    async def stream_turn(
        self, messages: list[dict], temperature: float = 0.7
    ) -> AsyncIterator[dict]:
        url = f"{self.base_url or 'https://api.inceptionlabs.ai/v1'}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "mercury-2.5",
            "reasoning_effort": "low",
            "messages": messages,
            "temperature": temperature,
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
                if "choices" in data and data["choices"]:
                    content = data["choices"][0].get("message", {}).get("content", "")
                    if content:
                        yield {"type": "text_delta", "content": content}
                yield {"type": "complete"}
        except Exception as e:
            logger.error(f"Inception API error: {e}")
            yield {"type": "error", "message": str(e)}
            yield {"type": "complete"}