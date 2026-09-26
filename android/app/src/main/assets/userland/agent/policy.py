"""Epic B: approval scopes + sandbox modes (policy.json).

policy.json lives in ~/.interlux/agent/ (wipe-proof: home survives userland
re-extracts — same rule as providers.json):

    {
      "sandbox": "full",
      "grants": {"<thread>": {"tools": [...], "commands": [...]}}
    }

Sandbox modes per turn:
  full       - today's behavior (default).
  workspace  - fs/image writes confined under one root (turn param
               sandbox_root, default $HOME).
  read-only  - no write-class tool runs at all: the loop blocks them
               BEFORE the approval prompt (asking to approve what is
               forbidden would be theater).

Grant semantics for approve(scope="session"):
  command-class tools (shell/exec/pty_run/pkg/tabs) store the exact
  approval label - "always allow THIS exact command for this thread";
  everything else (fs_write/fs_edit/image_generate/plugins) stores the
  tool name - "always allow THIS tool for this thread".

Revocation: policy method with revoke=<thread>|"*".
"""

from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path

SANDBOX_MODES = ("full", "workspace", "read-only")
GRANT_SCOPES = ("turn", "session")

# Tools whose standing grants are recorded per-exact-command, not per-tool.
COMMAND_TOOLS = {"shell", "exec", "pty_run", "pkg", "tabs"}

sandbox_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "interlux_sandbox", default=None
)


def policy_path() -> Path:
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".interlux/agent/policy.json"


def load_policy() -> dict:
    try:
        path = policy_path()
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                if data.get("sandbox") not in SANDBOX_MODES:
                    data["sandbox"] = "full"
                if not isinstance(data.get("grants"), dict):
                    data["grants"] = {}
                return data
    except Exception:
        pass
    return {"sandbox": "full", "grants": {}}


def save_policy(policy: dict) -> None:
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def thread_grants(policy: dict, thread_id: str) -> dict:
    grants = policy.setdefault("grants", {})
    entry = grants.get(thread_id)
    if not isinstance(entry, dict):
        entry = {"tools": [], "commands": []}
        grants[thread_id] = entry
    for key in ("tools", "commands"):
        if not isinstance(entry.get(key), list):
            entry[key] = []
    return entry


def record_grant(policy: dict, thread_id: str, tool: str, label: str) -> str:
    """Add a standing grant for this thread; returns what was recorded."""
    entry = thread_grants(policy, thread_id)
    if tool in COMMAND_TOOLS:
        what = label
        bucket = entry["commands"]
    else:
        what = tool
        bucket = entry["tools"]
    if what not in bucket:
        bucket.append(what)
    return what


def allows(policy: dict, thread_id: str, tool: str, label: str) -> bool:
    """True when a standing grant covers this exact call."""
    entry = policy.get("grants", {}).get(thread_id)
    if not isinstance(entry, dict):
        return False
    if tool not in COMMAND_TOOLS and tool in (entry.get("tools") or []):
        return True
    if label in (entry.get("commands") or []):
        return True
    return False


def revoke(policy: dict, thread_id: str) -> int:
    """Drop grants for one thread (or '*' for all). Returns entries removed."""
    grants = policy.setdefault("grants", {})
    if thread_id == "*":
        n = len(grants)
        policy["grants"] = {}
        return n
    return 1 if grants.pop(thread_id, None) is not None else 0


def resolve_sandbox(params: dict, policy: dict) -> dict:
    """Turn param wins; policy default otherwise. Returns {mode, root}."""
    mode = params.get("sandbox")
    if mode not in SANDBOX_MODES:
        mode = policy.get("sandbox", "full")
    root = params.get("sandbox_root") or os.environ.get("HOME") or str(Path.home())
    return {"mode": mode, "root": str(Path(root).expanduser())}


def sandbox_adjust(path: Path) -> Path:
    """Workspace mode: re-root relative paths under the sandbox root so the
    confinement check and the actual write agree (relative writes land in
    the workspace, not wherever the daemon's cwd happens to be)."""
    ctx = sandbox_ctx.get()
    if ctx and ctx.get("mode") == "workspace" and not path.is_absolute():
        return Path(ctx.get("root") or ".") / path
    return path


def check_write(path: Path | str) -> str | None:
    """Sandbox gate for file-writing tools. None = allowed."""
    ctx = sandbox_ctx.get()
    if not ctx:
        return None
    mode = ctx.get("mode", "full")
    if mode == "read-only":
        return "sandbox is read-only"
    if mode == "workspace":
        try:
            root = Path(ctx.get("root") or ".").resolve()
            target = Path(path).resolve()
        except Exception:
            return "sandbox: cannot resolve path"
        if not target.is_relative_to(root):
            return f"outside workspace root {root}"
    return None
