#!/usr/bin/env python3
"""Minimal client to test daemon approval flow."""

import asyncio
import json
import sys
import websockets


async def main():
    uri = "ws://127.0.0.1:4600"
    async with websockets.connect(uri) as ws:
        print(f"Connected to {uri}")

        # 1. Check capabilities
        await ws.send(json.dumps({"id": 1, "method": "capabilities"}))
        resp = await ws.recv()
        print(f"Capabilities: {json.loads(resp)}")

        # 2. Send a turn request (will trigger approval)
        task_id = 2
        payload = {
            "id": task_id,
            "method": "turn",
            "params": {
                "user": "run echo hello",
                "provider": "openai",
                "thread_id": "test-001",
            },
        }
        await ws.send(json.dumps(payload))
        print("Sent turn request (waiting for approval)...")

        # 3. Listen for approval notification
        approval_id = None
        async for msg in ws:
            data = json.loads(msg)
            print(f"< {data}")

            if data.get("type") == "approval_request":
                approval_id = data["id"]
                params = data["params"]
                print(f"> Approval requested: {params}")

                # 4. Approve
                print("Sending approval...")
                await ws.send(json.dumps({
                    "id": task_id + 1,
                    "method": "approve",
                    "params": {"id": approval_id, "decision": "accept"}
                }))

                # 5. Wait for turn completion
                async for msg2 in ws:
                    data2 = json.loads(msg2)
                    print(f"< {data2}")
                    if data2.get("result") and "turn_id" in data2["result"]:
                        print("Turn complete!")
                        return


if __name__ == "__main__":
    asyncio.run(main())
