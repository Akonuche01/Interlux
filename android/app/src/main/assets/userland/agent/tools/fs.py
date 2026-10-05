"""Filesystem tools (read-only ops need no approval; writes do)."""

import os
from pathlib import Path

from ..policy import agent_ctx, check_write, sandbox_adjust
from ..threads import fs_scope_error


def _resolve(path: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(path))
    return Path(expanded)


def _scope_error(p: Path) -> str | None:
    """Daemon-internal paths are off-limits to non-owner agents.

    Setup phase (no agent) allows all; the owner may inspect. Read from
    agent_ctx (turn identity), never from tool params.
    """
    try:
        agent = agent_ctx.get("")
    except Exception:
        agent = ""
    if not agent:
        return None
    try:
        from .. import agents as agents_mod
        owner = bool(agents_mod.is_owner(agent))
    except Exception:
        owner = False
    return fs_scope_error(agent, p, is_owner=owner)


async def fs_list(path: str = ".") -> dict:
    """List directory entries."""
    try:
        p = _resolve(path)
        denied = _scope_error(p)
        if denied:
            return {"status": "error", "message": denied}
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
        denied = _scope_error(p)
        if denied:
            return {"status": "error", "message": denied}
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
        p = sandbox_adjust(_resolve(path))
        denied = _scope_error(p)
        if denied:
            return {"status": "error", "message": denied}
        blocked = check_write(p)
        if blocked:
            return {"status": "error", "message": f"fs_write blocked by sandbox: {blocked}"}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"status": "success", "path": str(p), "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def fs_edit(path: str, old: str, new: str) -> dict:
    """Exact-match surgical replace. old must occur exactly once.
    Requires approval. Returns the changed line range."""
    try:
        p = sandbox_adjust(_resolve(path))
        denied = _scope_error(p)
        if denied:
            return {"status": "error", "message": denied}
        if not p.is_file():
            return {"status": "error", "message": f"not a file: {path}"}
        blocked = check_write(p)
        if blocked:
            return {"status": "error", "message": f"fs_edit blocked by sandbox: {blocked}"}
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
        denied = _scope_error(root)
        if denied:
            return {"status": "error", "message": denied}
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
            # Daemon-internal files are pruned silently (permission
            # pruning, like find): their names must not leak either.
            if _scope_error(f):
                continue
            try:
                if f.stat().st_size > 1048576:
                    continue
                # replace, never strict: a single non-UTF-8 byte must
                # not silently drop an otherwise searchable file.
                content = f.read_text(encoding="utf-8", errors="replace")
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
        denied = _scope_error(base)
        if denied:
            return {"status": "error", "message": denied}
        if not base.is_dir():
            return {"status": "error", "message": f"not a directory: {root}"}
        found: list[str] = []
        for p in sorted(base.glob(pattern)):
            if _scope_error(p):
                continue
            found.append(str(p))
            if len(found) >= max_hits:
                break
        return {"status": "success", "paths": found}
    except Exception as e:
        return {"status": "error", "message": str(e)}
