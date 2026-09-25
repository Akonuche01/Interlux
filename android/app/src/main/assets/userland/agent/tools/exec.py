"""Run any executable found on PATH with argv (no shell). Needs approval."""

import asyncio
import shutil

from .shell import _resolve_cwd
from .track import track, untrack


async def exec_tool(
    name: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout: float = 60,
) -> dict:
    """Execute a PATH binary directly. stdout/stderr captured, timeout kills."""
    path = shutil.which(name)
    if not path:
        return {"status": "error", "message": f"not on PATH: {name}"}
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            *(args or []),
            cwd=_resolve_cwd(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        key = track(proc)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return {"status": "error", "message": f"timed out after {timeout}s"}
        finally:
            untrack(key)
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": stdout.decode() if stdout else "",
            "stderr": stderr.decode() if stderr else "",
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
