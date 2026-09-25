"""Inline test to verify protocol handshake."""

import asyncio
import json

from agent.serve import handle_request, approvals


async def test_protocol():
    print("Testing capability handshake...")
    response = await handle_request({"id": 1, "method": "capabilities"}, None)
    print(f"Result: {response}")

    assert response.get("result", {}).get("stream") is True
    assert "openai" in response.get("result", {}).get("providers", [])

    print("\nTesting turn request (no provider key)...")
    response = await handle_request(
        {
            "id": 2,
            "method": "turn",
            "params": {
                "user": "whoami",
                "provider": "openai",
                "thread_id": "test-001",
            },
        },
        None,
    )
    print(f"Result: {response}")

    print("\nAll checks passed!")


async def test_approval():
    print("\nTesting approval flow...")
    # First clear any pending
    approvals.pending.clear()

    # Start turn in background
    import threading

    turn_result = [None]
    turn_error = [None]

    def run_turn():
        try:
            import asyncio

            turn = {
                "thread_id": "test-003",
                "tool_calls": [
                    {"tool": "shell", "parameters": {"command": "echo hello"}}
                ],
            }
            from agent.serve import run_tool_loop

            async def _run():
                await run_tool_loop(turn, None)
                turn_result[0] = turn

            asyncio.run(_run())
        except Exception as e:
            turn_error[0] = str(e)

    t = threading.Thread(target=run_turn, daemon=True)
    t.start()

    # Wait briefly for approval to be requested
    import time
    time.sleep(0.5)

    # Check if approval is pending
    if approvals.pending:
        fid = list(approvals.pending.keys())[0]
        print(f"Pending approval fid: {fid}")
        result = await handle_request(
            {"id": 4, "method": "approve", "params": {"id": fid, "decision": "accept"}},
            None,
        )
        print(f"Approval response: {result}")

        # Wait for turn to complete
        t.join(timeout=2)
        print(f"Pending after approve: {list(approvals.pending.keys())}")
        if turn_result[0]:
            print(f"Turn output: {turn_result[0].get('output', [])}")
    else:
        print("No pending approval (tools may be disabled)")


if __name__ == "__main__":
    asyncio.run(test_protocol())
    asyncio.run(test_approval())
