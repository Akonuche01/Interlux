"""On-device backend: llama-server on loopback (OpenAI-compatible).

A wire format like any other, declared the same way, found the same way. It
is not a vendor: it is the device's own model server, so it needs no key and
no network. Nothing in the engine privileges it -- it is a config section
whose dialect happens to be this one.

All this adapter adds is the key: llama-server ignores the Authorization
header, but an empty one is a shape some clients refuse to send, so a
placeholder is supplied when the config has none.
"""

from typing import AsyncIterator

from .openai import OpenAIProvider

# A module that declares DIALECT is a wire format; the engine finds formats by
# scanning this package. See dialects.py.
DIALECT = "on-device"
# Legacy name this backend was registered under before discovery became
# dialect-based. Kept so existing configs (and the Kotlin side, which still
# calls this the "local" provider) keep resolving without an edit.
DIALECT_ALIASES = ["local"]
CANONICAL_BASE = "http://127.0.0.1:4602/v1"


class OnDeviceProvider(OpenAIProvider):
    DEFAULT_MODEL = "local"

    async def stream_turn(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        model: str = "",
        images: list[str] | None = None,
        api: str = "chat",
        stream: bool = True,
    ) -> AsyncIterator[dict]:
        # llama-server needs no key; send something non-empty regardless.
        if not self.api_key:
            self.api_key = "local"
        async for delta in super().stream_turn(
            messages, temperature, model or self.DEFAULT_MODEL,
            images, api, stream,
        ):
            yield delta


ADAPTER = OnDeviceProvider
