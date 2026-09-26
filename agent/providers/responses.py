"""OpenAI Responses API over stdlib urllib (no httpx/h11 on-device).

Same thread+queue streaming idiom as streaming.py. Raises
ResponsesUnsupported on 404/405/501 so callers can fall back to chat
completions (e.g. servers without a /responses route).
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
import urllib.error
import urllib.request
from typing import AsyncIterator


class ResponsesUnsupported(Exception):
    pass


def _input_block(text: str, images: list[str]) -> list[dict] | str:
    if not images:
        return text
    from .media import load_image

    content: list[dict] = [{"type": "input_text", "text": text}]
    for ref in images:
        mime, b64 = load_image(ref)
        content.append({
            "type": "input_image",
            "image_url": f"data:{mime};base64,{b64}",
        })
    return content


def _chunks_from(obj: dict) -> list[str]:
    if obj.get("type") == "response.output_text.delta":
        delta = obj.get("delta", "")
        return [delta] if delta else []
    return []


def _usage_from(obj: dict) -> dict | None:
    """response.completed carries response.usage (input/output/total)."""
    if obj.get("type") != "response.completed":
        return None
    usage = (obj.get("response", {}) or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    from .streaming import normalize_usage
    return normalize_usage(
        usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("total_tokens"),
    )


def _usage_from_body(body: str) -> dict | None:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    direct = data.get("usage")
    if isinstance(direct, dict):
        from .streaming import normalize_usage
        return normalize_usage(
            direct.get("input_tokens", direct.get("prompt_tokens")),
            direct.get("output_tokens", direct.get("completion_tokens")),
            direct.get("total_tokens"),
        )
    return None


def _output_text(body: str) -> str:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return ""
    parts = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for block in item.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                parts.append(block.get("text", ""))
    return "".join(parts)


async def post_responses(
    base_url: str,
    headers: dict,
    model: str,
    text: str,
    images: list[str],
    stream: bool,
    timeout: int = 120,
) -> AsyncIterator[dict]:
    """Yield text_delta dicts. Raises ResponsesUnsupported when unrouted."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()
    unsupported = object()

    def work() -> None:
        payload = {
            "model": model,
            "input": _input_block(text, images),
            "stream": stream,
        }
        try:
            req = urllib.request.Request(
                f"{base_url}/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                if stream:
                    for raw in r:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if obj.get("type") == "response.completed":
                            used = _usage_from(obj)
                            if used:
                                loop.call_soon_threadsafe(
                                    queue.put_nowait,
                                    {"type": "usage", "usage": used},
                                )
                            break
                        for chunk in _chunks_from(obj):
                            loop.call_soon_threadsafe(queue.put_nowait, chunk)
                else:
                    body = r.read().decode("utf-8")
                    full = _output_text(body)
                    if full:
                        loop.call_soon_threadsafe(queue.put_nowait, full)
                    used = _usage_from_body(body)
                    if used:
                        loop.call_soon_threadsafe(
                            queue.put_nowait, {"type": "usage", "usage": used}
                        )
        except urllib.error.HTTPError as e:
            if e.code in (404, 405, 501):
                loop.call_soon_threadsafe(queue.put_nowait, unsupported)
            else:
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "message": str(e)}
                )
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
        if item is unsupported:
            raise ResponsesUnsupported()
        if isinstance(item, dict):
            yield item
        else:
            yield {"type": "text_delta", "content": item}
