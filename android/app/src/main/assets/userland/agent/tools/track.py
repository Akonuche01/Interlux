"""Per-turn subprocess tracking so `cancel` kills what it started.

Tools register spawned processes under the current turn id (via ContextVar,
which propagates through awaits and asyncio.to_thread). Cancel kills every
tracked process and schedules reaping; tools always untrack in a finally.
"""

import asyncio
import contextvars
import logging

logger = logging.getLogger("tools.track")

current_turn: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "turn_id", default=None
)

TRACKED: dict[str, set] = {}


def track(proc) -> tuple[str | None, object]:
    """Register proc under the current turn. Returns key for untrack()."""
    tid = current_turn.get()
    if tid is None:
        return None, proc
    TRACKED.setdefault(tid, set()).add(proc)
    return tid, proc


def untrack(key: tuple[str | None, object]) -> None:
    tid, proc = key
    if tid is None:
        return
    bucket = TRACKED.get(tid)
    if bucket is not None:
        bucket.discard(proc)
        if not bucket:
            TRACKED.pop(tid, None)


def _reap_async(proc) -> None:
    async def _wait() -> None:
        try:
            await proc.wait()
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_wait())
    except Exception:
        pass


def kill_turn(turn_id: str) -> int:
    """Kill all processes tracked under turn_id. Returns kill count."""
    bucket = TRACKED.pop(turn_id, set())
    n = 0
    for proc in list(bucket):
        try:
            proc.kill()
            n += 1
        except Exception:
            pass
        try:
            # asyncio processes need an explicit wait to reap the zombie;
            # threading.Popen owners reap in their own flow.
            if type(proc).__module__.startswith("asyncio"):
                _reap_async(proc)
        except Exception:
            pass
    if n:
        logger.info(f"killed {n} process(es) for {turn_id}")
    return n
