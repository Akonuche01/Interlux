"""Image generation tool (OpenAI-compatible Images API). Needs approval.

Generates into a file so follow-up turns can view it (multimodal roundtrip).
Cost-bearing on metered gateways — the approval prompt shows the prompt.
"""

import base64
import json
import os
import ssl
import time
import urllib.request
from pathlib import Path

INTERLUX_HOME = "/data/user/0/com.keneristudios.interlux/files/userland/home"


def _config() -> dict:
    raw = os.environ.get("INTERLUX_PROVIDERS", "")
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    home = os.environ.get("HOME") or str(Path.home())
    for f in (
        Path(home) / ".interlux/agent/providers.json",
        Path(__file__).parent.parent / "providers.json",
    ):
        try:
            if f.exists():
                return json.loads(f.read_text())
        except Exception:
            pass
    return {}


async def image_generate(
    prompt: str,
    path: str | None = None,
    provider: str = "tokenharbor",
    model: str = "gpt-image-1",
    size: str = "1024x1024",
) -> dict:
    """Generate an image. Returns the saved file path."""
    try:
        from ..providers import PROVIDERS
    except ImportError:
        return {"status": "error", "message": "provider registry unavailable"}

    if provider not in PROVIDERS:
        return {"status": "error", "message": f"unknown provider: {provider}"}
    cfg = _config().get(provider, {})
    cls = PROVIDERS[provider]
    base = (cfg.get("base_url") or getattr(cls, "DEFAULT_BASE", "") or "").rstrip("/")
    api_key = cfg.get("api_key", "")
    if not base or not api_key:
        return {"status": "error", "message": f"no key/base_url for {provider}"}

    url = f"{base}/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": "b64_json",
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        context = ssl.create_default_context()
        with urllib.request.urlopen(req, context=context, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8"))
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

    home = os.environ.get("HOME") or INTERLUX_HOME
    if not path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = str(Path(home) / ".interlux/agent/images" / f"gen-{stamp}.png")
    out = Path(os.path.expandvars(os.path.expanduser(path)))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
    except Exception as e:
        return {"status": "error", "message": f"save failed: {e}"}
    return {"status": "success", "path": str(out), "bytes": len(raw)}
