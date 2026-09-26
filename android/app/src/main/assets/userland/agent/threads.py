"""Thread session memory — history, resume/fork, per-thread client notes.

State lives wipe-proof under ~/.interlux/agent/threads/:
  <thread>.json          canonical history (base summary + messages + counter)
  <thread>.memories.md   client-writable notes the daemon injects as system msg

`replay_audit` reconstructs history from the JSONL audit trail when a state
file is missing (resume/fork fallback — spec: "replay audit tail").
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("INTERLUX_AGENT_CONFIG", "~/.interlux/agent")
).expanduser()
THREADS_DIR = CONFIG_DIR / "threads"
AUDIT_FILE = CONFIG_DIR / "audit.jsonl"

_ASSISTANT_CAP = 40_000
_TOOL_LINE_CAP = 2_000


def _safe(thread_id: str) -> str:
    # Strict slug: letters/digits/_/- only (no dots, no separators) so a
    # hostile thread_id can never traverse out of threads/.
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(thread_id)) or "thread"


def state_path(thread_id: str) -> Path:
    return THREADS_DIR / f"{_safe(thread_id)}.json"


def memories_path(thread_id: str) -> Path:
    return THREADS_DIR / f"{_safe(thread_id)}.memories.md"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def new_state(thread_id: str, parent: str | None = None) -> dict:
    ts = _now()
    return {
        "thread_id": thread_id,
        "turns": 0,
        "base": "",
        "history": [],
        "parent": parent,
        "created": ts,
        "updated": ts,
    }


def load_state(thread_id: str) -> dict | None:
    path = state_path(thread_id)
    try:
        if not path.exists():
            return None
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            return None
        state.setdefault("history", [])
        state.setdefault("base", "")
        state.setdefault("turns", 0)
        return state
    except Exception:
        return None


def save_state(state: dict) -> Path:
    """Atomic tmp+replace (same discipline as policy.json)."""
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    state["updated"] = _now()
    path = state_path(state["thread_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return path


def read_memories(thread_id: str) -> str:
    try:
        path = memories_path(thread_id)
        return path.read_text().strip() if path.exists() else ""
    except Exception:
        return ""


def write_memories(thread_id: str, content: str) -> Path:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    path = memories_path(thread_id)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(str(content))
    tmp.replace(path)
    return path


def build_messages(state: dict, thread_id: str, user_msg: str) -> list[dict]:
    """base summary -> client notes -> history -> current user message."""
    msgs: list[dict] = []
    base = state.get("base", "")
    if base:
        msgs.append({
            "role": "system",
            "content": "Conversation summary from an earlier session:\n" + base,
        })
    mem = read_memories(thread_id)
    if mem:
        msgs.append({
            "role": "system",
            "content": "Client notes for this thread:\n" + mem,
        })
    msgs.extend(state.get("history", []))
    msgs.append({"role": "user", "content": user_msg})
    return msgs


def fold_output(output: list[dict]) -> str:
    """Turn output (deltas + tool records) -> one assistant message string."""
    parts: list[str] = []
    for item in output:
        if item.get("type") == "text_delta":
            parts.append(item.get("content", ""))
            continue
        tool = item.get("tool")
        if tool:
            if "result" in item and isinstance(item["result"], dict):
                r = item["result"]
                if "stdout" in r:
                    line = str(r.get("stdout", ""))
                    if r.get("stderr"):
                        line += "\n[stderr] " + str(r["stderr"])
                    if r.get("exit_code"):
                        line += f"\n[exit {r['exit_code']}]"
                else:
                    line = json.dumps(r, ensure_ascii=False)
            else:
                line = f"{item.get('status', 'error')}: {item.get('message', '')}"
            parts.append(f"\n[{tool}] {line[:_TOOL_LINE_CAP]}")
        elif item.get("type") == "error":
            parts.append(f"\n[error] {item.get('message', '')}")
    text = "".join(parts)
    if len(text) > _ASSISTANT_CAP:
        text = text[:_ASSISTANT_CAP] + "\n...[truncated]"
    return text


def record_turn(state: dict, user_msg: str, output: list[dict]) -> None:
    state["history"].append({"role": "user", "content": user_msg})
    state["history"].append({"role": "assistant", "content": fold_output(output)})
    state["turns"] += 1
    save_state(state)


def materialize_state(thread_id: str) -> tuple[dict, str]:
    """Load state; rebuild from audit if missing; empty new thread otherwise.

    Returns (state, source) where source is "state" | "audit" | "new".
    A replayed thread restores its turn counter (one pair per audited turn).
    """
    state = load_state(thread_id)
    if state is not None:
        return state, "state"
    history = replay_audit(thread_id)
    state = new_state(thread_id)
    state["history"] = history
    state["turns"] = len(history) // 2
    save_state(state)
    return state, ("audit" if history else "new")


def fork_state(from_id: str, to_id: str) -> dict:
    """Copy history/base into a new thread (parent recorded)."""
    src, source = materialize_state(from_id)
    if source == "new":
        raise FileNotFoundError(f"no state or audit history for {from_id!r}")
    dst = new_state(to_id, parent=from_id)
    dst["base"] = src.get("base", "")
    dst["history"] = [dict(m) for m in src.get("history", [])]
    dst["turns"] = 0
    save_state(dst)
    mem = read_memories(from_id)
    if mem:
        write_memories(to_id, mem)
    return dst


def replay_audit(thread_id: str, limit: int = 200) -> list[dict]:
    """Rebuild [user, assistant, ...] from audit JSONL turns for a thread."""
    if not AUDIT_FILE.exists():
        return []
    msgs: list[dict] = []
    entries: list[dict] = []
    try:
        for line in AUDIT_FILE.read_text().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "turn" and e.get("thread_id") == thread_id:
                entries.append(e)
    except Exception:
        return []
    for e in entries[-limit:]:
        if e.get("cancelled"):
            continue
        user = e.get("user", "")
        if not user:
            continue
        msgs.append({"role": "user", "content": user})
        msgs.append({"role": "assistant", "content": fold_output(e.get("output", []))})
    return msgs


def transcript_text(state: dict) -> str:
    lines = []
    for m in state.get("history", []):
        lines.append(f"{m.get('role', '?')}: {m.get('content', '')}")
    return "\n\n".join(lines)
