"""Attach to live terminal tabs via the app TabBridge (127.0.0.1:4601).

Actions: list | read | send. list/read run free; send types into a live
shell and needs approval.
"""

import base64
import json
import socket

BRIDGE_PORT = 4601

READ_ACTIONS = {"list", "read"}


def _rpc(payload: dict, timeout: float = 10) -> dict:
    with socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=timeout) as s:
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


async def tabs(action: str = "list", id: int | None = None, data: str = "", max_bytes: int = 8192) -> dict:
    """List tabs, read recent output, or send keystrokes to a live tab."""
    action = (action or "list").lower()
    try:
        if action == "list":
            resp = _rpc({"op": "tabs"})
            if "error" in resp:
                return {"status": "error", "message": resp["error"]}
            return {"status": "success", "tabs": resp.get("tabs", [])}
        if action == "read":
            if id is None:
                return {"status": "error", "message": "read needs id"}
            resp = _rpc({"op": "read", "id": id, "max_bytes": max_bytes})
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
            resp = _rpc({"op": "send", "id": id, "data": payload})
            if "error" in resp:
                return {"status": "error", "message": resp["error"]}
            return {"status": "success", "id": id}
        return {"status": "error", "message": f"unknown action: {action}"}
    except (OSError, TimeoutError) as e:
        return {"status": "error", "message": f"bridge unreachable: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
