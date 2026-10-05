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


def _put(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue,
         item) -> None:
    """Threadsafe queue put that survives loop shutdown.

    call_soon_threadsafe raises RuntimeError once the loop is closed
    (turn cancelled, daemon stopping). Swallowing only that keeps
    shutdown quiet without hiding real errors, which still raise.
    """
    try:
        loop.call_soon_threadsafe(queue.put_nowait, item)
    except RuntimeError:
        pass


def openai_chunks(obj: dict) -> list[str]:
    out = []
    for choice in obj.get("choices", []) or []:
        delta = choice.get("delta", {}) or {}
        if "content" in delta and delta["content"]:
            out.append(delta["content"])
    return out


def openai_reasoning(obj: dict) -> list[str]:
    """DeepSeek-style reasoning_content carried beside content."""
    out = []
    for choice in obj.get("choices", []) or []:
        delta = choice.get("delta", {}) or {}
        text = delta.get("reasoning_content")
        if isinstance(text, str) and text:
            out.append(text)
    return out


def _summary_text(obj: dict) -> str:
    """The reasoning summary carried by one stream object, or "".

    Read from wherever it appears: Inception puts it at the top level of
    the object (a sibling of `choices`), the same shape its non-streaming
    response uses, while an OpenAI-style relay nests it in the delta. The
    value is either the text itself or a `{content, status}` object -- the
    documented ReasoningSummary shape -- so both are accepted.
    """
    sources = [obj]
    for choice in obj.get("choices", []) or []:
        if isinstance(choice, dict):
            delta = choice.get("delta")
            if isinstance(delta, dict):
                sources.append(delta)
    for source in sources:
        raw = source.get("reasoning_summary")
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(raw, dict):
            text = raw.get("content")
            if isinstance(text, str) and text:
                return text
    return ""


def make_openai_reasoning() -> Callable[[dict], list[str]]:
    """Stateful reasoning reader for OpenAI-compatible streams.

    A reader is stateful because the two shapes disagree about what a
    chunk means. `reasoning_content` (DeepSeek) is a fragment: every chunk
    adds to the thought. `reasoning_summary` (Inception Mercury) is the
    whole thought so far, re-sent as it grows. Emitting either verbatim
    would therefore either duplicate the summary on every chunk or drop
    all but the first fragment.

    Reconciling against what has already gone out handles both: a summary
    that extends the last one yields only its new tail, a fragment that
    does not yields itself. Without this the thoughts section stays empty
    on Mercury -- see the reader in openai.py, which asked for no summary
    at all until now.
    """
    seen = {"summary": ""}

    def read(obj: dict) -> list[str]:
        out: list[str] = []
        for choice in obj.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            text = delta.get("reasoning_content")
            if not (isinstance(text, str) and text):
                text = delta.get("reasoning")
            if isinstance(text, str) and text:
                out.append(text)
        summary = _summary_text(obj)
        if summary:
            previous = seen["summary"]
            fresh = summary[len(previous):] if summary.startswith(
                previous) else summary
            if fresh:
                seen["summary"] = summary
                out.append(fresh)
        return out

    return read


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


def anthropic_reasoning(obj: dict) -> list[str]:
    """Claude thinking blocks (only sent when thinking was requested)."""
    if obj.get("type") == "content_block_delta":
        delta = obj.get("delta", {}) or {}
        if delta.get("type") == "thinking_delta":
            text = delta.get("thinking")
            if isinstance(text, str) and text:
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
    usage = data.get("usage")
    if isinstance(usage, dict) and (
        "input_tokens" in usage or "output_tokens" in usage
    ):
        # Anthropic counts. openai_usage reads the SAME dict, finds no
        # prompt_tokens/completion_tokens, and returns a truthy ALL-ZERO
        # dict -- so the old `openai_usage(data) or anthropic_usage(data)`
        # short-circuited and anthropic_usage was never reached, silently
        # reporting zero tokens for every non-streamed Anthropic turn.
        # Detect the shape; simply swapping the order is not enough,
        # because anthropic_usage would then answer all-zero for OpenAI
        # bodies (it reads the same absent keys).
        return anthropic_usage(data)
    return openai_usage(data) or anthropic_usage(data)


async def post_sse(
    url: str,
    headers: dict,
    payload: dict,
    chunks_from: Callable[[dict], list[str]],
    reasoning_from: Callable[[dict], list[str]],
    timeout: int = 120,
    usage_from: Callable[[dict], dict | None] | None = None,
) -> AsyncIterator[dict]:
    """Yield text_delta dicts as SSE chunks arrive, then stop (caller sends complete).

    usage_from maps a parsed SSE object (or full body) to a usage fragment;
    fragments merge into one running total emitted as {"type": "usage"}.

    reasoning_from maps an object to thinking fragments, emitted as
    {"type": "reasoning_delta"} — kept out of the answer stream.

    reasoning_from is required, not optional. While it had a default, an
    adapter that simply forgot to pass one lost the thought section of the
    client's card -- for that provider only, with no error, no log line and
    nothing to notice. Requiring it turns a silent capability loss into a
    failure at load time. See the contract in base.py.
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
            _put(loop, queue,
                 {"type": "usage", "usage": dict(running_usage)})

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
                        _put(loop, queue, chunk)
                    for r in reasoning_from(obj):
                        _put(loop, queue,
                             {"type": "reasoning_delta", "content": r})
            if not got_chunk:
                raw = "".join(raw_parts)
                fallback = _fallback_content(raw)
                if fallback:
                    _put(loop, queue, fallback)
                if usage_from is not None:
                    emit_usage(_fallback_usage(raw))
        except Exception as e:
            # Shutdown-safe put (see _put): the loop may be gone here.
            _put(loop, queue, {"type": "error", "message": str(e)})
        finally:
            _put(loop, queue, done)

    threading.Thread(target=work, daemon=True).start()
    while True:
        item = await queue.get()
        if item is done:
            return
        if isinstance(item, dict):
            yield item
        else:
            yield {"type": "text_delta", "content": item}
