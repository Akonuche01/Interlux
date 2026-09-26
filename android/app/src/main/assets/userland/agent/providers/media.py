"""Image attachments for multimodal turns.

A turn may carry `images: [...]` where each entry is either a file path
(PNG/JPEG/WEBP/GIF) or a `data:` URL. Helpers load bytes once here so every
provider adapter shares validation, size limits, and mime detection.
"""

import base64
import mimetypes
import os
from pathlib import Path

MAX_IMAGE_BYTES = 5 * 1024 * 1024

_ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def load_image(ref: str) -> tuple[str, str]:
    """Return (media_type, base64) for a path or data: URL."""
    if ref.startswith("data:"):
        header, _, payload = ref.partition(",")
        if not payload:
            raise ValueError("empty data: URL")
        mime = header[5:].split(";")[0] or "image/png"
        _check_mime(mime)
        raw = base64.b64decode(payload)
        _check_size(raw)
        return mime, payload
    p = Path(os.path.expandvars(os.path.expanduser(ref)))
    if not p.is_file():
        raise ValueError(f"image not found: {ref}")
    if p.suffix.lower() not in _ALLOWED:
        raise ValueError(f"unsupported image type: {p.suffix}")
    raw = p.read_bytes()
    _check_size(raw)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return mime, base64.b64encode(raw).decode("ascii")


def _check_mime(mime: str) -> None:
    if not mime.startswith("image/"):
        raise ValueError(f"not an image mime: {mime}")


def _check_size(raw: bytes) -> None:
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"image too large: {len(raw)} bytes (max {MAX_IMAGE_BYTES})")


def openai_blocks(text: str, images: list[str]) -> list[dict]:
    """OpenAI chat content blocks (also Inception/TokenHarbor/local shape)."""
    if not images:
        return [{"role": "user", "content": text}]
    content: list[dict] = [{"type": "text", "text": text}]
    for ref in images:
        mime, b64 = load_image(ref)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    return [{"role": "user", "content": content}]


def anthropic_blocks(text: str, images: list[str]) -> list[dict]:
    """Anthropic messages content blocks."""
    if not images:
        return [{"role": "user", "content": text}]
    content: list[dict] = [{"type": "text", "text": text}]
    for ref in images:
        mime, b64 = load_image(ref)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
    return [{"role": "user", "content": content}]
