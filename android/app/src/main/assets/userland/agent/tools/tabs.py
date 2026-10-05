"""Attach to live terminal tabs via the app TabBridge (127.0.0.1:4601).

Actions: list | read | send. list/read run free; send types into a live
shell and needs approval.

Every request carries a per-install token read from the app's private
filesDir, which TabBridge.kt now requires. The socket is on loopback, but on
Android EVERY app shares the loopback interface, so the port alone was never
an access control -- see TabBridge.currentToken for why this is needed.
"""

import asyncio
import base64
import json
import socket
from pathlib import Path

BRIDGE_PORT = 4601

READ_ACTIONS = {"list", "read"}


def _token_path() -> Path:
    """Where the app keeps the bridge's per-install secret.

    This module ships at <filesDir>/userland/agent/tools/tabs.py, so three
    parents up is the app's private filesDir -- the exact file TabBridge.kt
    reads. No other UID can read it, and that is what makes it a secret.
    """
    return Path(__file__).resolve().parents[3] / "tabbridge.token"


def _bridge_token() -> str:
    """Current bridge token, or "" if it cannot be read."""
    try:
        return _token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _rpc_sync(payload: dict, timeout: float = 10) -> dict:
    token = _bridge_token()
    if not token:
        return {"error": "bridge token unavailable"}
    payload = {**payload, "token": token}
    with socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=timeout) as s:
        # The connect timeout above does not bound the read: without it
        # a silent bridge hangs the turn forever.
        s.settimeout(timeout)
        f = s.makefile("rw", encoding="utf-8")
        f.write(json.dumps(payload) + "\n")
        f.flush()
        line = f.readline()
    if not line:
        return {"status": "error", "message": "empty bridge response"}
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError) as e:
        return {"status": "error", "message": f"bad bridge response: {e}"}


async def _rpc(payload: dict, timeout: float = 10) -> dict:
    # Off the event loop: blocking sockets freeze every concurrent
    # turn, approval card and cancellation meanwhile.
    return await asyncio.to_thread(_rpc_sync, payload, timeout)


async def tabs(action: str = "list", id: int | None = None, data: str = "", max_bytes: int = 8192) -> dict:
    """List tabs, read recent output, or send keystrokes to a live tab."""
    action = (action or "list").lower()
    try:
        if action == "list":
            resp = await _rpc({"op": "tabs"})
            if "error" in resp:
                return {"status": "error", "message": resp["error"]}
            return {"status": "success", "tabs": resp.get("tabs", [])}
        if action == "read":
            if id is None:
                return {"status": "error", "message": "read needs id"}
            resp = await _rpc({"op": "read", "id": id, "max_bytes": max_bytes})
            if "error" in resp:
                return {"status": "error", "message": resp["error"]}
            raw = base64.b64decode(resp.get("data", ""))
            return {
                "status": "success",
                "id": id,
                "alive": resp.get("alive"),
                "output": raw.decode("utf-8", errors="replace"),
            }
        if action == "send":
            if id is None or not data:
                return {"status": "error", "message": "send needs id and data"}
            payload = base64.b64encode(data.encode("utf-8")).decode("ascii")
            resp = await _rpc({"op": "send", "id": id, "data": payload})
            if "error" in resp:
                return {"status": "error", "message": resp["error"]}
            return {"status": "success", "id": id}
        return {"status": "error", "message": f"unknown action: {action}"}
    except (OSError, TimeoutError) as e:
        return {"status": "error", "message": f"bridge unreachable: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
