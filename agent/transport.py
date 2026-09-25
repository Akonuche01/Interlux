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
            logger.info(f"Calling handler with {payload}")
            response = await self.handler(payload, websocket)
            logger.info(f"Sending response: {response}")
            async with _lock_for(websocket):
                await websocket.send(json.dumps(response))
            logger.info("Response sent successfully")
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
        tasks: set[asyncio.Task] = set()
        try:
            async for message in websocket:
                logger.info(f"Received message: {message[:100]}")
                t = asyncio.create_task(self._handle_one(websocket, message))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        finally:
            _active_clients.discard(websocket)
            _send_locks.pop(id(websocket), None)
            for t in list(tasks):
                t.cancel()


async def broadcast(payload: dict) -> None:
    """Send notification to all connected clients."""
    targets = list(_active_clients)
    logger.info(f"Broadcasting to {len(targets)} clients: {payload}")
    data = json.dumps(payload)
    for ws in targets:
        try:
            async with _lock_for(ws):
                await ws.send(data)
            logger.info("Broadcast sent")
        except Exception as e:
            logger.exception(f"Failed to send broadcast: {e}")
