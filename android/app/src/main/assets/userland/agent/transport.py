"""WebSocket JSON-RPC transport."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets.asyncio.server
from websockets.asyncio.server import Server

logger = logging.getLogger("transport")

_active_clients: set[websockets.asyncio.server.ServerConnection] = set()
_send_locks: dict[int, asyncio.Lock] = {}

# Per-agent socket sets for targeted broadcasts. Maps agent_id -> set of
# sockets. Maintained by serve.py via _remember_socket/_drop_socket.
_agent_sockets: dict[str, set] = {}


def register_agent_socket(agent_id: str, sock) -> None:
    """Track a socket as belonging to an agent (called from serve.py)."""
    if not agent_id:
        return
    _agent_sockets.setdefault(agent_id, set()).add(sock)


def unregister_agent_socket(agent_id: str, sock) -> None:
    """Remove a socket from an agent's set (called from serve.py)."""
    if not agent_id:
        return
    socks = _agent_sockets.get(agent_id)
    if socks:
        socks.discard(sock)
        if not socks:
            _agent_sockets.pop(agent_id, None)


async def broadcast_to_agent(payload: dict, agent_id: str) -> int:
    """Send a notification to all live sockets of one agent.

    Returns how many sockets the payload actually reached. The caller
    logs that number: a bare "routed" line reads as *delivered* even
    when the agent has no socket at all, so a per-agent event dropped
    on the floor looked exactly like a healthy one in daemon.log. That
    is the difference between "the UI is broken" and "nobody was
    listening", and it cost a day of guessing.
    """
    targets = list(_agent_sockets.get(agent_id, set()))
    if not targets:
        return 0
    data = json.dumps(payload)
    sent = 0
    for ws in targets:
        try:
            async with _lock_for(ws):
                await ws.send(data)
            sent += 1
        except Exception as e:
            logger.exception(f"Failed to send to agent {agent_id}: {e}")
    return sent


async def broadcast(payload: dict) -> None:
    """Send notification to all connected clients.

    When the payload carries a thread_id, it is routed only to the
    owning agent's sockets (per-agent namespacing). Global events
    (no thread_id) go to all connected clients.
    """
    thread_id = payload.get("thread_id")
    if thread_id:
        # Route to the thread owner's agent. The agent_id is embedded
        # in the payload by serve.py when it knows the owner.
        agent_id = payload.get("agent_id", "")
        if agent_id:
            reached = await broadcast_to_agent(payload, agent_id)
            logger.info(
                f"Routed to agent {agent_id} ({reached} socket(s)): "
                f"{payload.get('type')}")
            if not reached:
                # Say it out loud. This is the line that was missing while
                # the UI showed nothing and the log showed a healthy turn.
                logger.warning(
                    f"agent {agent_id} has no live socket — "
                    f"{payload.get('type')} was dropped, not delivered")
            return
    targets = list(_active_clients)
    logger.debug(f"Broadcasting to {len(targets)} clients: {payload}")
    data = json.dumps(payload)
    for ws in targets:
        try:
            async with _lock_for(ws):
                await ws.send(data)
            logger.debug("Broadcast sent")
        except Exception as e:
            logger.exception(f"Failed to send broadcast: {e}")

# Sync callbacks run when a socket dies (client gone). They receive the
# dead socket so per-connection state (agent mapping) can be forgotten;
# hooks that only need the event itself ignore the argument.
on_disconnect: list[Callable[[object], None]] = []

# Async callbacks run when a socket connects, with the new socket.
# Used to re-send state the fresh UI missed (open approval cards).
on_connect: list = []


def _lock_for(ws) -> asyncio.Lock:
    key = id(ws)
    lock = _send_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _send_locks[key] = lock
    return lock


class Transport:
    def __init__(self, handler: Callable[[dict, object], Awaitable[dict]], port: int = 4600):
        self.handler = handler
        self.port = port
        self.server: Server | None = None

    async def start(self) -> None:
        async with websockets.serve(self._on_message, "127.0.0.1", self.port):
            logger.info(f"Transport listening on :{self.port}")
            await asyncio.Future()

    async def _handle_one(self, websocket, message: str) -> None:
        """Handle a single frame concurrently so approve can land while turn runs."""
        try:
            payload = json.loads(message)
            logger.debug(f"Calling handler with {payload}")
            if isinstance(payload, dict) and "id" not in payload:
                # JSON-RPC notification (e.g. `initialized`): execute for
                # side effects, send no reply.
                await self.handler(payload, websocket)
                return
            response = await self.handler(payload, websocket)
            if response is None:
                # Handler consumed a frame that needs no reply (e.g. the
                # client's answer to a daemon-initiated tool/call).
                return
            logger.debug(f"Sending response: {response}")
            async with _lock_for(websocket):
                await websocket.send(json.dumps(response))
            logger.debug("Response sent successfully")
        except Exception as e:
            logger.exception(f"Handler error: {e}")
            try:
                async with _lock_for(websocket):
                    await websocket.send(json.dumps({"error": {"code": -32700, "message": str(e)}}))
            except Exception as e2:
                logger.exception(f"Failed to send error: {e2}")

    async def _on_message(self, websocket) -> None:
        """Handle JSON-RPC frames; each frame in its own task (no head-of-line block)."""
        logger.info("New websocket connection")
        _active_clients.add(websocket)
        _lock_for(websocket)
        logger.info(f"Active clients: {len(_active_clients)}")
        for hook in list(on_connect):
            try:
                result = hook(websocket)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("connect hook failed")
        tasks: set[asyncio.Task] = set()
        try:
            async for message in websocket:
                logger.debug(f"Received message: {message[:100]}")
                t = asyncio.create_task(self._handle_one(websocket, message))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        finally:
            _active_clients.discard(websocket)
            _send_locks.pop(id(websocket), None)
            for hook in list(on_disconnect):
                try:
                    hook(websocket)
                except Exception:
                    logger.exception("disconnect hook failed")
            for t in list(tasks):
                t.cancel()


async def send_to(sock, payload: dict) -> None:
    """Directed send (daemon-initiated requests like tool/call)."""
    async with _lock_for(sock):
        await sock.send(json.dumps(payload))
