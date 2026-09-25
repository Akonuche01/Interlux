"""Bionic package tool (pkg.sh). Read-only `list` runs free; everything else needs approval."""

import asyncio
import os
from pathlib import Path

INTERLUX_HOME = "/data/user/0/com.keneristudios.interlux/files/userland/home"

READ_ACTIONS = {"list"}


def _pkg_sh() -> Path:
    home = Path(os.environ.get("HOME") or INTERLUX_HOME)
    return home.parent / "pkg.sh"


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
        stdout, stderr = await proc.communicate()
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": stdout.decode() if stdout else "",
            "stderr": stderr.decode() if stderr else "",
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
