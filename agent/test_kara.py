#!/usr/bin/env python3
"""Kara-compatible client test."""

import asyncio
import json
import websockets


async def main():
    uri = "ws://127.0.0.1:4600"
    async with websockets.connect(uri) as ws:
        print(f"Connected: {uri}")

        # 1. Get capabilities
        await ws.send(json.dumps({"id": 1, "method": "capabilities"}))
        resp = json.loads(await ws.recv())
        print(f"Capabilities: {resp['result']}")

        # 2. Send turn request
        print("\nSending turn request...")
        await ws.send(json.dumps({
            "id": 2,
            "method": "turn",
            "params": {
                "user": "echo hello",
                "provider": "openai",
                "thread_id": "test-001",
            }
        }))

        # 3. Listen for approval + completion
        async for msg in ws:
            data = json.loads(msg)
            print(f"< {data}")

            if data.get("type") == "approval_request":
                print(f"> Approving...")
                await ws.send(json.dumps({
                    "id": 3,
                    "method": "approve",
                    "params": {"id": data["id"], "decision": "accept"}
                }))

            if data.get("result") and "turn_id" in data["result"]:
                print("Turn complete!")
                break


if __name__ == "__main__":
    asyncio.run(main())
