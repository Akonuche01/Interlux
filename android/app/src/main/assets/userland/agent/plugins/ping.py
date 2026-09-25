"""Example bundled plugin: read-only pong. Proves the loader path on every boot."""

TOOLS = {}


async def _ping(message: str = "ping") -> dict:
    return {"status": "success", "message": f"pong: {message}"}


TOOLS["ping"] = _ping
WRITE_TOOLS: set = set()
