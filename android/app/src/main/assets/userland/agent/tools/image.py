"""Image generation tool (OpenAI-compatible Images API). Needs approval.

Generates into a file so follow-up turns can view it (multimodal roundtrip).
Cost-bearing on metered gateways — the approval prompt shows the prompt.
"""

import asyncio
import base64
import json
import os
import ssl
import time
import urllib.request
from pathlib import Path

from ..home import engine_config_dir
from ..providers.base import BROWSER_UA


def _post_images_sync(url: str, headers: dict, payload: dict) -> dict:
    """Blocking HTTP POST to the Images API.

    Synchronous on purpose, and always driven from asyncio.to_thread below:
    urlopen blocks the calling thread and the daemon runs ONE event loop
    for every turn, approval card, broadcast and cancellation. Up to 180s
    of blocking here used to freeze all of them -- this was the only tool
    that did not hand blocking I/O to a thread (websearch.py and tabs.py
    already do).
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    context = ssl.create_default_context()
    with urllib.request.urlopen(req, context=context, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


async def image_generate(
    prompt: str,
    path: str | None = None,
    provider: str = "",
    model: str = "gpt-image-1",
    size: str = "1024x1024",
) -> dict:
    """Generate an image. Returns the saved file path.

    The provider is never named in code. It is the one the caller chose, or
    the configured default, or simply the only provider configured. A
    vendor hardcoded here would be a vendor the user may hold no key for.
    """
    from ..pconfig import configured, configured_provider_names, resolve_provider
    from ..policy import agent_ctx, load_policy
    from ..providers import base_for

    if not provider:
        provider = str(load_policy().get("default_provider") or "")
    if not provider:
        names = configured_provider_names()
        provider = names[0] if names else ""
    if not provider:
        return {"status": "error", "message": "no provider is configured"}

    # Per-agent credentials (step 3): the key comes from the calling
    # agent's own section, never the shared top level. agent_ctx is
    # set per turn from socket identity, never from wire params.
    cfg = resolve_provider(provider, agent_ctx.get(""))
    if not configured(cfg):
        return {"status": "error", "message": f"unknown provider: {provider}"}
    base = (cfg.get("base_url") or base_for(cfg) or "").rstrip("/")
    api_key = cfg.get("api_key", "")
    if not base or not api_key:
        return {"status": "error", "message": f"no key/base_url for {provider}"}

    url = f"{base}/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": BROWSER_UA,
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": "b64_json",
    }

    try:
        # Off the event loop: see _post_images_sync.
        body = await asyncio.to_thread(_post_images_sync, url, headers, payload)
    except Exception as e:
        return {"status": "error", "message": f"generate failed: {e}"}

    try:
        b64 = body["data"][0]["b64_json"]
        raw = base64.b64decode(b64)
    except (KeyError, IndexError, ValueError) as e:
        revised = ""
        try:
            revised = body["data"][0].get("revised_prompt", "")
        except Exception:
            pass
        return {"status": "error", "message": f"bad image payload: {e}", "revised_prompt": revised}

    if not path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = str(engine_config_dir() / "images" / f"gen-{stamp}.png")
    out = Path(os.path.expandvars(os.path.expanduser(path)))
    try:
        from ..policy import check_write, sandbox_adjust
        from .fs import _scope_error
    except ImportError:
        check_write = lambda _p: None  # noqa: E731
        sandbox_adjust = lambda p: p  # noqa: E731
        # Fail CLOSED, unlike the two above: an absent scope check must not
        # read as "allowed". This boundary is the only thing standing
        # between a non-owner agent and agents.json / policy.json /
        # audit.jsonl, so a broken import refuses rather than permits.
        _scope_error = lambda _p: "sandbox scope check unavailable"
    out = sandbox_adjust(out)
    # The scope boundary fs_write already applies (fs.py). image_generate
    # used to skip it, so a non-owner agent holding an image grant could
    # write over the daemon's own state files -- check_write polices
    # different paths and is not a substitute.
    denied = _scope_error(out)
    if denied:
        return {"status": "error", "message": denied}
    blocked = check_write(out)
    if blocked:
        return {"status": "error", "message": f"image_generate blocked by sandbox: {blocked}"}
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
    except Exception as e:
        return {"status": "error", "message": f"save failed: {e}"}
    return {"status": "success", "path": str(out), "bytes": len(raw)}
