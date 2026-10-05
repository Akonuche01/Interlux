"""Shell execution tool."""

import asyncio
import os
import shutil

from ..home import engine_home
from .track import track, untrack


def _resolve_cwd(cwd: str | None) -> str:
    if cwd:
        return cwd
    return str(engine_home())


def _resolve_shell() -> str:
    """Explicit shell binary. Never rely on /bin/sh (absent on Android)
    or PATH lookups that can resolve to Termux-baked prefixes."""
    for candidate in (
        os.environ.get("SHELL"),
        shutil.which("sh"),
        "/system/bin/sh",
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return "/system/bin/sh"


async def run_shell(command: str, cwd: str | None = None,
                  timeout: float = 60) -> dict:
    """Run shell command and return output.

    Bounded like exec_tool: a hung command (sleep, interactive pager,
    network wait) must fail the call, not the turn. Without this one
    hanging shell held its turn open until the 6h backstop.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            executable=_resolve_shell(),
            cwd=_resolve_cwd(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        key = track(proc)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return {"status": "error",
                        "message": f"timed out after {timeout}s"}
        finally:
            untrack(key)

        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
            "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"status": "error", "stderr": str(e)}
