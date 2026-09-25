"""Run a command under a pty (for terminal-aware programs). Needs approval."""

import asyncio
import os
import select
import subprocess
import time

from .shell import _resolve_cwd, _resolve_shell
from .track import track, untrack


def _run_blocking(command: str, cwd: str, shell: str, timeout: float) -> dict:
    try:
        import pty
    except ImportError:
        return {"status": "error", "message": "pty unavailable on this platform"}
    try:
        controller, sub = pty.openpty()
    except Exception as e:
        return {"status": "error", "message": f"openpty failed: {e}"}
    try:
        proc = subprocess.Popen(
            [shell, "-c", command],
            stdin=sub,
            stdout=sub,
            stderr=sub,
            cwd=cwd,
            close_fds=True,
        )
    except Exception as e:
        os.close(sub)
        os.close(controller)
        return {"status": "error", "message": str(e)}
    os.close(sub)
    key = track(proc)
    out = b""
    deadline = time.time() + timeout
    timed_out = False
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            timed_out = True
            break
        try:
            ready, _, _ = select.select([controller], [], [], remaining)
        except Exception:
            break
        if not ready:
            timed_out = True
            break
        try:
            chunk = os.read(controller, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if proc.poll() is not None:
            # Drain anything left, then stop.
            try:
                while True:
                    ready, _, _ = select.select([controller], [], [], 0.2)
                    if not ready:
                        break
                    chunk = os.read(controller, 65536)
                    if not chunk:
                        break
                    out += chunk
            except OSError:
                pass
            break
    if timed_out:
        try:
            proc.kill()
        except Exception:
            pass
        out += b"\n[TIMEOUT]"
    try:
        code = proc.wait(timeout=5)
    except Exception:
        code = proc.poll()
    untrack(key)
    try:
        os.close(controller)
    except Exception:
        pass
    return {
        "status": "success" if code == 0 and not timed_out else "error",
        "output": out.decode("utf-8", errors="replace"),
        "exit_code": code,
        "timed_out": timed_out,
    }


async def pty_run(command: str, cwd: str | None = None, timeout: float = 30) -> dict:
    """Execute command under a pty, capturing terminal output."""
    return await asyncio.to_thread(
        _run_blocking, command, _resolve_cwd(cwd), _resolve_shell(), timeout
    )
