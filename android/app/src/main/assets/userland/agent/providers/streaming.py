"""SSE streaming over stdlib urllib (no httpx/h11 on-device).

Runs the blocking HTTP read in a daemon thread, pushes parsed text chunks
into an asyncio.Queue. Falls back to full-body parse when the server ignores
stream:true and returns one JSON document.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
import urllib.request
from typing import AsyncIterator, Callable


def openai_chunks(obj: dict) -> list[str]:
    out = []
    for choice in obj.get("choices", []) or []:
        delta = choice.get("delta", {}) or {}
        if "content" in delta and delta["content"]:
            out.append(delta["content"])
    return out


def anthropic_chunks(obj: dict) -> list[str]:
    if obj.get("type") == "content_block_delta":
        text = (obj.get("delta", {}) or {}).get("text")
        if text:
            return [text]
    return []


def _fallback_content(body: str) -> str:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return ""
    for choice in data.get("choices", []) or []:
        content = (choice.get("message", {}) or {}).get("content")
        if content:
            return content
    parts = []
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "".join(parts)


async def post_sse(
    url: str,
    headers: dict,
    payload: dict,
    chunks_from: Callable[[dict], list[str]],
    timeout: int = 120,
) -> AsyncIterator[dict]:
    """Yield text_delta dicts as SSE chunks arrive, then stop (caller sends complete)."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    def work() -> None:
        raw_parts: list[str] = []
        got_chunk = False
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                for raw in r:
                    text = raw.decode("utf-8", errors="replace")
                    raw_parts.append(text)
                    line = text.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    for chunk in chunks_from(obj):
                        got_chunk = True
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
            if not got_chunk:
                fallback = _fallback_content("".join(raw_parts))
                if fallback:
                    loop.call_soon_threadsafe(queue.put_nowait, fallback)
        except Exception as e:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "error", "message": str(e)}
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, done)

    threading.Thread(target=work, daemon=True).start()
    while True:
        item = await queue.get()
        if item is done:
            return
        if isinstance(item, dict):
            yield item
        else:
            yield {"type": "text_delta", "content": item}
