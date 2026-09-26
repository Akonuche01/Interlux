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


async def fs_edit(path: str, old: str, new: str) -> dict:
    """Exact-match surgical replace. old must occur exactly once.
    Requires approval. Returns the changed line range."""
    try:
        p = _resolve(path)
        if not p.is_file():
            return {"status": "error", "message": f"not a file: {path}"}
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return {"status": "error", "message": "oldString not found"}
        if count > 1:
            return {
                "status": "error",
                "message": f"oldString matches {count} times; be more specific",
            }
        pre_lines = text[: text.index(old)].count("\n")
        new_lines = new.count("\n")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return {
            "status": "success",
            "path": str(p),
            "first_line": pre_lines + 1,
            "last_line": pre_lines + 1 + new_lines,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def fs_search(path: str = ".", pattern: str = "", max_hits: int = 50) -> dict:
    """Regex search over file contents under path. Read-only."""
    import re

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"status": "error", "message": f"bad pattern: {e}"}
    try:
        root = _resolve(path)
        if not root.exists():
            return {"status": "error", "message": f"no such path: {path}"}
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        hits: list[str] = []
        scanned = 0
        for f in files:
            if len(hits) >= max_hits:
                break
            if not f.is_file() or f.is_symlink():
                continue
            try:
                if f.stat().st_size > 1048576:
                    continue
                content = f.read_text(encoding="utf-8", errors="strict")
            except Exception:
                continue
            scanned += 1
            for n, line in enumerate(content.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{n}:{line.strip()[:200]}")
                    if len(hits) >= max_hits:
                        break
        return {"status": "success", "hits": hits, "scanned": scanned}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def fs_glob(pattern: str = "**/*.py", root: str = ".", max_hits: int = 100) -> dict:
    """Path-pattern listing. Read-only."""
    try:
        base = _resolve(root)
        if not base.is_dir():
            return {"status": "error", "message": f"not a directory: {root}"}
        found: list[str] = []
        for p in sorted(base.glob(pattern)):
            found.append(str(p))
            if len(found) >= max_hits:
                break
        return {"status": "success", "paths": found}
    except Exception as e:
        return {"status": "error", "message": str(e)}
