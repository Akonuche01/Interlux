"""Read-only git tools. Mutating git goes through the shell tool (approval gate)."""

import asyncio
import os

from .track import track, untrack


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


async def _git(args: list[str], cwd: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=_expand(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        key = track(proc)
        try:
            stdout, stderr = await proc.communicate()
        finally:
            untrack(key)
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": stdout.decode() if stdout else "",
            "stderr": stderr.decode() if stderr else "",
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"status": "error", "stderr": str(e)}


async def git_status(cwd: str = ".") -> dict:
    """git status --short."""
    return await _git(["status", "--short"], cwd)


async def git_log(cwd: str = ".", n: int = 10) -> dict:
    """Last n commits, one line each."""
    return await _git(["log", f"-n{n}", "--oneline"], cwd)


async def git_diff(cwd: str = ".") -> dict:
    """Unstaged diff."""
    return await _git(["diff"], cwd)
