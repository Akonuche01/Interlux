"""Bionic package tool (pkg.sh). Read-only `list` runs free; everything else needs approval."""

import asyncio
from pathlib import Path

from ..home import engine_home
from .track import track, untrack

READ_ACTIONS = {"list"}


def _pkg_sh() -> Path:
    return engine_home().parent / "pkg.sh"


async def pkg(action: str = "list", packages: list[str] | None = None) -> dict:
    """Run pkg.sh. action: update|install|remove|upgrade|list."""
    script = _pkg_sh()
    if not script.is_file():
        return {"status": "error", "message": f"pkg.sh not found at {script}"}
    args = [str(script), action, *(packages or [])]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        key = track(proc)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), 300)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return {"status": "error",
                        "message": "timed out after 300s"}
        finally:
            untrack(key)
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
            "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
