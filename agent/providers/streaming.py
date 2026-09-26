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


def _num(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(prompt=0, completion=0, total=0) -> dict:
    """One usage shape everywhere: OpenAI-style token names, ints, total filled."""
    prompt, completion, total = _num(prompt), _num(completion), _num(total)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or (prompt + completion),
    }


def openai_usage(obj: dict) -> dict | None:
    """Final stream chunk (with stream_options.include_usage) or full body."""
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    return normalize_usage(
        usage.get("prompt_tokens"), usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )


def anthropic_usage(obj: dict) -> dict | None:
    """message_start carries input+output; message_delta tops up output."""
    kind = obj.get("type")
    if kind == "message_start":
        usage = (obj.get("message", {}) or {}).get("usage", {}) or {}
        if not isinstance(usage, dict):
            return None
        return normalize_usage(usage.get("input_tokens"), usage.get("output_tokens"))
    if kind == "message_delta":
        usage = obj.get("usage", {}) or {}
        if not isinstance(usage, dict) or "output_tokens" not in usage:
            return None
        # Cumulative output total per the API; merged over the start fragment.
        return {"completion_tokens": _num(usage.get("output_tokens"))}
    if isinstance(obj.get("usage"), dict):
        # Non-streamed full body.
        usage = obj["usage"]
        return normalize_usage(usage.get("input_tokens"), usage.get("output_tokens"))
    return None


def merge_usage(base: dict, fragment: dict) -> dict:
    """Fold a (possibly partial) usage fragment into the running total.

    An explicit total in the fragment wins; otherwise the total is
    recomputed whenever prompt/completion counts move.
    """
    merged = dict(base or {})
    fragment = fragment or {}
    touched = False
    for key in ("prompt_tokens", "completion_tokens"):
        if key in fragment:
            merged[key] = _num(fragment[key])
            touched = True
    if "total_tokens" in fragment:
        merged["total_tokens"] = _num(fragment["total_tokens"])
    elif touched or "total_tokens" not in merged:
        merged["total_tokens"] = (
            merged.get("prompt_tokens", 0) + merged.get("completion_tokens", 0)
        )
    return merged


def anthropic_chunks(obj: dict) -> list[str]:
    if obj.get("type") == "content_block_delta":
        text = (obj.get("delta", {}) or {}).get("text")
        if text:
            return [text]
    return []


def _fallback_body(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _fallback_content(body: str) -> str:
    data = _fallback_body(body)
    for choice in data.get("choices", []) or []:
        content = (choice.get("message", {}) or {}).get("content")
        if content:
            return content
    parts = []
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "".join(parts)


def _fallback_usage(body: str) -> dict | None:
    data = _fallback_body(body)
    if not data:
        return None
    return openai_usage(data) or anthropic_usage(data)


async def post_sse(
    url: str,
    headers: dict,
    payload: dict,
    chunks_from: Callable[[dict], list[str]],
    timeout: int = 120,
    usage_from: Callable[[dict], dict | None] | None = None,
) -> AsyncIterator[dict]:
    """Yield text_delta dicts as SSE chunks arrive, then stop (caller sends complete).

    usage_from maps a parsed SSE object (or full body) to a usage fragment;
    fragments merge into one running total emitted as {"type": "usage"}.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    def work() -> None:
        raw_parts: list[str] = []
        got_chunk = False
        running_usage: dict = {}

        def emit_usage(fragment: dict | None) -> None:
            nonlocal running_usage
            if not fragment:
                return
            running_usage = merge_usage(running_usage, fragment)
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "usage", "usage": dict(running_usage)}
            )

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
                    if usage_from is not None:
                        emit_usage(usage_from(obj))
                    for chunk in chunks_from(obj):
                        got_chunk = True
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
            if not got_chunk:
                raw = "".join(raw_parts)
                fallback = _fallback_content(raw)
                if fallback:
                    loop.call_soon_threadsafe(queue.put_nowait, fallback)
                if usage_from is not None:
                    emit_usage(_fallback_usage(raw))
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
