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


def flatten_messages(messages: list[dict] | None) -> str:
    """Flatten a full message list into the single string these providers send.

    Every provider here is single-message: the wire payload carries one `user`
    message, and `stream_turn` was reading `messages[-1]` alone. That silently
    discarded the system prompts *and the entire conversation history* that
    `build_messages` had carefully assembled.

    The consequence was not cosmetic. The daemon persisted every turn to
    `threads/<id>.json` and read it back on the next turn, so the history was
    on disk, in the RPC payload, in the audit trail — and then thrown away one
    line later. The model began every turn having never seen any prior turn.
    That is why Kara could not answer a question she had asked herself
    moments earlier: within a turn the loop's own `fold` carried context
    (so tool use worked), but across turns there was nothing.

    Roles are labelled so the model can still tell who said what. The final
    user message is emitted bare, so an instruction that ends in "now do X"
    still reads as the live instruction rather than as quoted history.
    """
    if not messages:
        return ""
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if not content.strip():
            continue
        role = str(m.get("role", "user")).lower()
        if role == "system":
            parts.append(f"[system]\n{content}")
        elif role == "assistant":
            parts.append(f"[assistant]\n{content}")
        else:
            parts.append(f"[user]\n{content}")
    if not parts:
        return ""
    # The last user turn is the live instruction: strip its label so the model
    # reads it as something to do now, not as a quotation.
    last = messages[-1]
    if isinstance(last, dict) and str(last.get("role", "")).lower() == "user":
        tail = parts[-1]
        if tail.startswith("[user]\n"):
            parts[-1] = tail[len("[user]\n"):]
    return "\n\n".join(parts)


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
