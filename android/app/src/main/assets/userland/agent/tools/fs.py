"""Filesystem tools (read-only ops need no approval; writes do)."""

import os
from pathlib import Path


def _resolve(path: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(path))
    return Path(expanded)


async def fs_list(path: str = ".") -> dict:
    """List directory entries."""
    try:
        p = _resolve(path)
        if not p.exists():
            return {"status": "error", "message": f"no such path: {path}"}
        if not p.is_dir():
            return {"status": "error", "message": f"not a directory: {path}"}
        entries = sorted(
            (("dir" if e.is_dir() else "file") + f" {e.name}")
            for e in p.iterdir()
        )
        return {"status": "success", "path": str(p), "entries": entries}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def fs_read(path: str, max_bytes: int = 65536) -> dict:
    """Read a text file (truncated at max_bytes)."""
    try:
        p = _resolve(path)
        if not p.is_file():
            return {"status": "error", "message": f"not a file: {path}"}
        data = p.read_bytes()[:max_bytes]
        return {
            "status": "success",
            "path": str(p),
            "content": data.decode("utf-8", errors="replace"),
            "truncated": p.stat().st_size > max_bytes,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def fs_write(path: str, content: str) -> dict:
    """Write content to a file (creates parents). Requires approval."""
    try:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"status": "success", "path": str(p), "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"status": "error", "message": str(e)}
