from __future__ import annotations

import argparse
import asyncio
import json
import os
import logging
from pathlib import Path
from typing import AsyncIterator

from .audit import AuditLog
from .providers import PROVIDERS, BaseProvider
from .tools import EXTRA_WRITE, TOOLS, approval_label, needs_approval
from .tools.plugins import scan_plugins
from .transport import Transport, broadcast

PLUGIN_DIR = Path(__file__).parent / "plugins"
scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("iagent")


class Session:
    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.turns: list[dict] = []
        self.cancel_requested = False


audit = AuditLog()


class ApprovalManager:
    def __init__(self):
        self.pending: dict[str, asyncio.Future] = {}

    def request_approval(self, thread_id: str, params: dict) -> asyncio.Future:
        fid = params["id"]
        future = asyncio.Future()
        self.pending[fid] = future
        return future

    def approve(self, fid: str, decision: str) -> bool:
        if fid in self.pending:
            granted = str(decision or "").strip().lower() in (
                "approve", "accept", "allow", "yes", "always", "true",
            )
            self.pending[fid].set_result(granted)
            del self.pending[fid]
            return True
        return False


approvals = ApprovalManager()


def load_provider(name: str, config: dict) -> BaseProvider | None:
    if name not in PROVIDERS:
        return None
    if not isinstance(config, dict):
        config = {}
    return PROVIDERS[name](
        config.get("api_key", ""),
        config.get("base_url"),
    )


async def run_tool_loop(turn: dict, client_socket) -> None:
    """Execute tool calls with approval flow."""
    tool_calls = turn.get("tool_calls", [])
    logger.info(f"Processing {len(tool_calls)} tool call(s)")

    for call in tool_calls:
        tool_name = call.get("tool", "shell")
        params = call.get("parameters", {})
        logger.info(f"Tool: {tool_name}, params: {params}")

        if needs_approval(tool_name, params):
            thread_id = turn.get("thread_id", "default")
            fid = f"{thread_id}:{len(approvals.pending)}"
            approval_params = {"command": approval_label(tool_name, params), "id": fid}

            approval_future = approvals.request_approval(thread_id, approval_params)
            logger.info(f"Requesting approval: {fid}")
            await broadcast({
                "type": "approval_request",
                "id": fid,
                "params": approval_params,
            })

            logger.info("Waiting for client decision...")
            granted = await approval_future
            if not granted:
                logger.info(f"Denied by client: {fid} — skipping")
                denied = {"tool": tool_name, "status": "denied", "message": "denied by client"}
                turn["output"].append(denied)
                await broadcast({"type": "tool_result", **denied})
                continue
            logger.info("Approval received!")

        logger.info(f"Executing tool: {tool_name}")
        if tool_name in TOOLS:
            try:
                result = await TOOLS[tool_name](**params)
                logger.info(f"Tool result: {result}")
                tool_output = {"tool": tool_name, "result": result}
                turn["output"].append(tool_output)
                await broadcast({"type": "tool_result", **tool_output})
            except Exception as e:
                logger.error(f"Tool execution failed: {e}")
                tool_output = {"tool": tool_name, "status": "error", "message": str(e)}
                turn["output"].append(tool_output)
                await broadcast({"type": "tool_result", **tool_output})
        else:
            logger.error(f"Unknown tool: {tool_name}")
            tool_output = {"tool": tool_name, "status": "error", "message": f"unknown tool: {tool_name}"}
            turn["output"].append(tool_output)
            await broadcast({"type": "tool_result", **tool_output})


async def handle_turn(payload: dict, client_socket) -> dict:
    logger.info(f"handle_turn called with payload: {payload}")
    params = payload.get("params", {})
    user_msg = params.get("user", "")
    provider_name = params.get("provider", "openai")
    model = params.get("model", "gpt-4o")

    thread_id = params.get("thread_id", f"thread-{len(audit.read_last())}")
    session = Session(thread_id)
    turn = {"id": f"{thread_id}:{len(session.turns)}", "user": user_msg, "thread_id": thread_id}
    session.turns.append(turn)

    config = json.loads(os.environ.get("INTERLUX_PROVIDERS", "{}"))
    if not config:
        try:
            config_file = Path(__file__).parent / "providers.json"
            if config_file.exists():
                config = json.loads(config_file.read_text())
        except Exception:
            pass
    provider = load_provider(provider_name, config.get(provider_name, {}))

    messages = [{"role": "user", "content": user_msg}]

    turn["output"] = []
    has_error = False

    async for delta in stream_turn(provider, messages, model):
        turn["output"].append(delta)
        if delta.get("type") == "error":
            has_error = True
        await _send_delta(delta)

    # Parse tool calls from provider response
    content = ""
    for item in turn["output"]:
        if item.get("type") == "text_delta":
            content += item.get("content", "")

    if content:
        logger.info(f"Content from provider: {content[:200]}")

    if has_error:
        turn["complete"] = True
        try:
            audit.append({"type": "turn", "thread_id": thread_id, **turn})
        except Exception:
            pass
        return {"id": payload.get("id"), "result": {"turn_id": turn["id"]}}

    # Parse tool calls from content
    if content:
        import re
        # Look for JSON blocks like ```json {...} ```
        json_blocks = re.findall(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_blocks:
            try:
                tool_calls = json.loads(json_blocks[0])
                if isinstance(tool_calls, list):
                    turn["tool_calls"] = tool_calls
                elif "tool_calls" in tool_calls:
                    turn["tool_calls"] = tool_calls["tool_calls"]
            except json.JSONDecodeError:
                pass

    # Fallback: Generate tool call from user message if it starts with "run "
    if not turn.get("tool_calls") and user_msg.strip().lower().startswith("run "):
        command = user_msg.strip()[4:].strip()
        turn["tool_calls"] = [{
            "tool": "shell",
            "parameters": {"command": command}
        }]

    await run_tool_loop(turn, client_socket)

    turn["complete"] = True
    try:
        audit.append({"type": "turn", "thread_id": thread_id, **turn})
    except Exception:
        pass
    await broadcast({"type": "complete", "turn_id": turn["id"]})

    return {"id": payload.get("id"), "result": {"turn_id": turn["id"]}}


async def handle_request(payload: dict, client_socket) -> dict:
    """Process JSON-RPC request."""
    try:
        method = payload.get("method")
        params = payload.get("params", {})
        request_id = payload.get("id")

        if method == "capabilities":
            return {
                "id": request_id,
                "result": {
                    "protocol": 1,
                    "methods": ["turn", "cancel", "capabilities", "approve", "tools_refresh"],
                    "stream": True,
                    "providers": list(PROVIDERS.keys()),
                    "tools": sorted(TOOLS.keys()),
                },
            }

        if method == "tools_refresh":
            loaded = scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)
            return {
                "id": request_id,
                "result": {"loaded": loaded, "tools": sorted(TOOLS.keys())},
            }

        if method == "turn":
            return await handle_turn(payload, client_socket)

        if method == "approve":
            fid = params.get("id")
            decision = params.get("decision")
            if approvals.approve(fid, decision):
                return {"id": request_id, "result": True}
            return {"id": request_id, "error": {"code": -32602, "message": "unknown fid"}}

        if method == "cancel":
            return {"id": request_id, "result": True}

        return {
            "id": request_id,
            "error": {"code": -32601, "message": f"unsupported method: {method}"},
        }

    except Exception as e:
        logger.exception("Request handling failed")
        return {
            "id": payload.get("id"),
            "error": {"code": -32700, "message": str(e)},
        }


async def stream_turn(
    provider: BaseProvider | None, messages: list[dict], model: str
) -> AsyncIterator[dict]:
    logger.info(f"stream_turn called with provider={provider}, model={model}")
    if not provider:
        logger.info("No provider found, yielding error message")
        yield {"type": "text_delta", "content": f"No provider found: {model}"}
        yield {"type": "complete"}
        return

    try:
        async for delta in provider.stream_turn(messages, model=model):
            yield delta
    except Exception as e:
        yield {"type": "error", "message": str(e)}
        yield {"type": "complete"}


async def _send_delta(delta: dict) -> None:
    logger.info(f"_send_delta called with delta={delta}")
    content = delta.get("content", delta.get("message", ""))
    logger.info(f"Broadcasting: type={delta.get('type')}, content={content}")
    await broadcast({"type": delta.get("type"), "content": content})


def main() -> int:
    parser = argparse.ArgumentParser(description="Interlux agent daemon")
    parser.add_argument("-p", "--port", type=int, default=4600)
    args = parser.parse_args()

    logger.info(f"Starting iagent on port {args.port}")

    transport = Transport(handle_request, args.port)
    try:
        asyncio.run(transport.start())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
