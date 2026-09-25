"""Shell execution tool."""

import asyncio
import os
import shutil
import subprocess

INTERLUX_HOME = "/data/user/0/com.keneristudios.interlux/files/userland/home"


def _resolve_cwd(cwd: str | None) -> str:
    if cwd:
        return cwd
    return os.environ.get("HOME") or INTERLUX_HOME


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


async def run_shell(command: str, cwd: str | None = None) -> dict:
    """Run shell command and return output."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            executable=_resolve_shell(),
            cwd=_resolve_cwd(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": stdout.decode() if stdout else "",
            "stderr": stderr.decode() if stderr else "",
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"status": "error", "stderr": str(e)}
