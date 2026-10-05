"""Thread session memory — history, resume/fork, per-thread client notes.

State lives wipe-proof under ~/.interlux/agent/threads/:
  <thread>.json          canonical history (base summary + messages + counter)
  <thread>.memories.md   client-writable notes the daemon injects as system msg

`replay_audit` reconstructs history from the JSONL audit trail when a state
file is missing (resume/fork fallback — spec: "replay audit tail").
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .home import engine_config_dir

CONFIG_DIR = engine_config_dir()
THREADS_DIR = CONFIG_DIR / "threads"
ARCHIVE_DIR = THREADS_DIR / ".archived"
AUDIT_FILE = CONFIG_DIR / "audit.jsonl"

_ASSISTANT_CAP = 40_000
_TOOL_LINE_CAP = 2_000

# Fallback identity injected as the first system message when a thread
# carries no client developer instructions. Without any identity prompt the
# model answers "who are you" from its own weights (Qwen says Qwen,
# DeepSeek says DeepSeek). Paired clients set their own persona per
# thread via developer instructions; this default stays neutral.
# Where she actually is. Without this she has to guess, and guessing is
# expensive: the bots live under two trees with two different spellings, and a
# model told only "you have this device's tools" starts with fs_list and keeps
# searching -- hunting resolves into narration, and narration is what lands in
# the answer bubble.
ENVIRONMENT_ORIENTATION = (
    "You run on Android, inside this app's own Linux userland. Your home "
    "directory is the app's `home`; shared phone storage is mounted at "
    "`/sdcard`.\n"
    "There is a proot root under `home/.rootfs`, so a path beginning `/root/` "
    "belongs to that root, not to the Android filesystem.\n"
    "The user's trading bots run from `/root/bots/` inside that root: "
    "`forexmind`, `marketmind`, `degentrader`. A second, separate copy sits at "
    "`/sdcard/TermuxProjects/projects/`, where the third is spelled "
    "`degen_trader`. When the user says 'the bots' without qualifying it, they "
    "mean `/root/bots/` -- the ones actually running. Always say which tree you "
    "used.\n"
    "Tool calls have an output ceiling. Never put a whole file's contents in "
    "one call: a long file is cut off mid-call, the call never closes, it does "
    "not parse, and nothing is written -- the raw call then lands in your reply "
    "as text. Write large files in pieces: create the file with the first part, "
    "then append the rest with further calls. One file per call, and keep each "
    "call small enough to close."
)

DEFAULT_IDENTITY = (
    "You are the on-device AI agent. When the user speaks to you, "
    "answer as their assistant."
)


def _safe(thread_id: str) -> str:
    # Strict slug: letters/digits/_/- only (no dots, no separators) so a
    # hostile thread_id can never traverse out of threads/.
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(thread_id)) or "thread"


def state_path(thread_id: str) -> Path:
    return THREADS_DIR / f"{_safe(thread_id)}.json"


def memories_path(thread_id: str) -> Path:
    return THREADS_DIR / f"{_safe(thread_id)}.memories.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def new_state(thread_id: str, parent: str | None = None,
              agent: str = "") -> dict:
    ts = _now()
    return {
        "thread_id": thread_id,
        "turns": 0,
        "base": "",
        "name": "",
        "developer": "",
        "history": [],
        "parent": parent,
        "agent": agent,
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
        state.setdefault("name", "")
        state.setdefault("developer", "")
        state.setdefault("turns", 0)
        state.setdefault("agent", "")
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


_TOOL_BLOCK = re.compile(r"\[([a-z_][a-z0-9_.-]*)\]\s*(.*?)\s*\[/\1\]", re.S)
_STRAY_MARKER = re.compile(r"\[/?[a-z_][a-z0-9_.-]*\]")


def split_tool_sections(text: str) -> tuple[str, str]:
    """One folded assistant turn -> (what the model wrote, what the tools said).

    Tool sections are wrapped in `[name] … [/name]` so the client's rebuild can
    split them back out into activity rows. Feeding those markers into the
    model's own conversation taught it to write *its* tool calls in that shape:
    the live case emitted

        [shell {"tool": "shell", "parameters": {"command": "ps"}}]

    which is not the format the parser accepts — so no tool ran, and the raw
    call was broadcast to the user as the answer. The model needs the facts,
    not the scaffolding.

    Rewording was not enough, because the fault was never the wording — it was
    the *speaker*. The first fix rewrote the markers into prose and left it in
    the assistant's own message:

        (Tool shell returned: …)

    and the model did what any model does with its own last message: continued
    it. Live, that produced an endless stream of invented `(Tool fs_read
    returned: …)` text — over 2,000 deltas in one turn, no tool call, no end —
    because the pattern being copied was the model's own supposed voice.

    So the two halves are separated here and given different speakers by the
    caller. What the model wrote stays `assistant`; what the tools returned is
    handed back as a replay, exactly as `serve.py` already does for the fold
    *within* a turn. That block exists for the same reason and carries the same
    instruction not to repeat it.
    """
    if not text:
        return "", ""
    blocks = list(_TOOL_BLOCK.finditer(text))
    if not blocks:
        # A marker the model has already learnt to imitate, with no closer.
        return _STRAY_MARKER.sub("", text).strip(), ""
    prose = text
    for m in reversed(blocks):
        prose = prose[:m.start()] + prose[m.end():]
    results = "\n".join(m.group(0) for m in blocks)
    return _STRAY_MARKER.sub("", prose).strip(), results.strip()


def build_messages(
    state: dict,
    thread_id: str,
    user_msg: str,
    extra_system: list[dict] | None = None,
) -> list[dict]:
    """base summary -> client notes -> extra system (skills) -> history -> user."""
    msgs: list[dict] = []
    # Identity first: the thread's developer instructions (e.g. the
    # client's persona from thread/start), else the daemon default. Without this the
    # model self-identifies from its weights on "who are you".
    developer = (state.get("developer") or "").strip()
    msgs.append({
        "role": "system",
        "content": developer or DEFAULT_IDENTITY,
    })
    msgs.append({"role": "system", "content": ENVIRONMENT_ORIENTATION})
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
    msgs.extend(extra_system or [])
    for entry in state.get("history", []):
        # An assistant turn carries the folded work, markers and all. The two
        # halves of it are different speakers, and they travel as such: what the
        # model wrote stays `assistant`, and what the tools returned is replayed
        # in a message of its own. Merging them into one assistant message is
        # what let a tool's stdout read as the model's own words, and a model
        # reading its own last message continues it — see [split_tool_sections]
        # for the live failure that produced.
        #
        # The replay is fenced and named for the same reason `serve.py` fences
        # the in-turn fold: the bracket labels would otherwise read as something
        # the user said, and a model that quotes its own context back parrots
        # them as fresh output.
        if isinstance(entry, dict) and entry.get("role") == "assistant":
            prose, results = split_tool_sections(str(entry.get("content", "")))
            if prose:
                msgs.append({**entry, "content": prose})
            if results:
                msgs.append({
                    "role": "user",
                    "content": (
                        "<assistant_work>\n"
                        " This is a replay of your own earlier work in this "
                        "thread, shown for your reference. It is not a user "
                        "message. Do not repeat it back verbatim.\n"
                        f"{results}\n"
                        "</assistant_work>"
                    ),
                })
        else:
            msgs.append(entry)
    msgs.append({"role": "user", "content": user_msg})
    return msgs


def tool_failed(result: dict) -> bool:
    """Whether one tool result means the action did not happen.

    The single definition of failure in this engine, so the fold and the round
    loop cannot disagree about it. A non-success status, or any non-zero exit,
    counts; everything else is a success.

    Two readers depend on this and they have to agree: `fold_output` states the
    failure in words for the model, and the round loop records it as an
    outstanding failure (`_note_tool_outcome`) so a turn cannot close as a
    finished job while one stands. A model reading "FAILED" while the loop
    believed the work had succeeded is precisely the disagreement that
    produced a cheerful summary over a command that never ran.
    """
    status = str(result.get("status") or "")
    code = result.get("exit_code")
    return (status not in ("", "success", "ok", "done")
            or (isinstance(code, int) and code != 0))


def fold_output(output: list[dict]) -> str:
    """Turn output (deltas + tool records) -> one assistant message string.

    Tool sections are wrapped in explicit [tool]...[/tool] markers.
    The rebuild path splits on them deterministically (narration to the
    bubble, sections to activity rows); without closers a multi-line
    tool dump is indistinguishable from chat text on reload. The model
    also reads these back as data, not conversation. Error entries use
    [error]...[/error] the same way.
    """
    parts: list[str] = []
    for item in output:
        if item.get("type") == "text_delta":
            parts.append(item.get("content", ""))
            continue
        tool = item.get("tool")
        if tool:
            if "result" in item and isinstance(item["result"], dict):
                r = item["result"]
                status = str(r.get("status") or "")
                # A non-success status, or any non-zero exit, means the
                # action did not happen. The stdout branch below used to
                # build its line from stdout/stderr/exit_code alone and
                # never mention status at all -- so a failed call folded
                # into something that read like a normal run with a bit of
                # noise on stderr, and a model summarising it reported work
                # that had not been done. Failure is therefore stated
                # first, in words, where truncation cannot reach it.
                #
                # The test itself lives in `tool_failed`: the round loop reads
                # the same fact to decide whether the turn may close as
                # finished, and the two must not be able to disagree.
                failed = tool_failed(r)
                if "stdout" in r:
                    line = str(r.get("stdout", ""))
                    if r.get("stderr"):
                        line += "\n[stderr] " + str(r["stderr"])
                    if r.get("exit_code"):
                        line += f"\n[exit {r['exit_code']}]"
                else:
                    line = json.dumps(r, ensure_ascii=False)
                if failed:
                    line = (
                        "FAILED - this tool did not do what was asked "
                        f"(status={status or 'error'}). Do not describe it as "
                        "done and do not invent the output it would have "
                        "produced. Work out why from this result, then take a "
                        "different approach.\n" + line)
            else:
                line = f"{item.get('status', 'error')}: {item.get('message', '')}"
            parts.append(f"\n[{tool}] {line[:_TOOL_LINE_CAP]}\n[/{tool}]")
        elif item.get("type") == "error":
            parts.append(f"\n[error] {item.get('message', '')}\n[/error]")
    text = "".join(parts)
    if len(text) > _ASSISTANT_CAP:
        # Back off so we never sever a [name]...[/name] block: cut at the
        # last marker closer at or before the cap (or the last newline if the
        # tail is all openers). A block cut mid-body loses its closer, which is
        # what made reopened chats leak tool output into the chat bubble
        # and drop the answer that followed.
        head = text[:_ASSISTANT_CAP]
        last_close = -1
        for m in re.finditer(r"\[/[a-z_][a-z0-9_.-]*\]", head):
            last_close = m.end()
        cut = last_close if last_close > 0 else head.rfind("\n")
        if cut < 0:
            cut = _ASSISTANT_CAP
        text = text[:cut].rstrip() + "\n...[truncated]"
    return text


def record_turn(state: dict, user_msg: str, output: list[dict]) -> None:
    state["history"].append({"role": "user", "content": user_msg})
    state["history"].append({"role": "assistant", "content": fold_output(output)})
    state["turns"] += 1
    save_state(state)


def materialize_state(thread_id: str, agent: str = "") -> tuple[dict, str]:
    """Load state; rebuild from audit if missing; empty new thread otherwise.

    Returns (state, source) where source is "state" | "audit" | "new".
    A replayed thread restores its turn counter (one pair per audited turn).
    """
    state = load_state(thread_id)
    if state is not None:
        return state, "state"
    history = replay_audit(thread_id, agent=agent)
    state = new_state(thread_id, agent=agent)
    state["history"] = history
    state["turns"] = len(history) // 2
    if history:
        save_state(state)
    # Note: empty "new" states are NOT persisted — persisting them would
    # turn one-shot "unknown thread" errors (steer/fork/compact) into
    # successes on retry, and clutter thread/list with hollow entries.
    return state, ("audit" if history else "new")


def fork_state(from_id: str, to_id: str, agent: str = "") -> dict:
    """Copy history/base into a new thread (parent recorded)."""
    src, source = materialize_state(from_id, agent=agent)
    if not src.get("history") and not src.get("base"):
        # Nothing to fork (fresh/empty thread) — same error whether the
        # state file exists or not, so retries behave identically.
        raise FileNotFoundError(f"no state or audit history for {from_id!r}")
    dst = new_state(to_id, parent=from_id, agent=agent)
    dst["base"] = src.get("base", "")
    dst["developer"] = src.get("developer", "")
    dst["history"] = [dict(m) for m in src.get("history", [])]
    dst["turns"] = 0
    save_state(dst)
    mem = read_memories(from_id)
    if mem:
        write_memories(to_id, mem)
    return dst


def replay_audit(thread_id: str, limit: int = 200,
                 agent: str = "") -> list[dict]:
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
                # Namespacing: drop turns owned by another agent. Legacy
                # entries (no agent_id) predate ownership and replay for
                # any caller — the state files (primary path) are
                # correctly owned via boot migration, so this fallback
                # only ever exposes pre-step-2 history.
                owner = e.get("agent_id", "")
                if agent and owner and owner != agent:
                    continue
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


def migrate_threads_to_agent(agent_id: str) -> int:
    """Stamp all existing threads with the given agent_id (one-time migration).

    Returns the number of threads stamped. Only threads without an agent
    field are stamped; already-stamped threads are left alone.
    """
    if not THREADS_DIR.is_dir():
        return 0
    count = 0
    for path in THREADS_DIR.glob("*.json"):
        if path.suffixes and path.suffix == ".tmp":
            continue
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        if state.get("agent"):
            continue
        state["agent"] = agent_id
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        tmp.replace(path)
        count += 1
    return count


def transcript_text(state: dict) -> str:
    lines = []
    for m in state.get("history", []):
        lines.append(f"{m.get('role', '?')}: {m.get('content', '')}")
    return "\n\n".join(lines)


def list_threads(limit: int = 50, agent: str = "") -> list[dict]:
    """Newest-first thread summaries for history drawers.

    Pure read: corrupt files are skipped, never repaired here.
    When agent is non-empty, only that agent's threads are returned.
    """
    try:
        limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    if not THREADS_DIR.is_dir():
        return []
    summaries = []
    for path in sorted(THREADS_DIR.glob("*.json")):
        if path.suffixes and path.suffix == ".tmp":
            continue
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        # Filter by agent ownership when specified.
        if agent and state.get("agent", "") != agent:
            continue
        thread_id = state.get("thread_id") or path.stem
        history = state.get("history", []) or []
        preview = ""
        for m in reversed(history):
            if isinstance(m, dict) and m.get("role") == "user":
                preview = str(m.get("content", ""))[:120]
                break
        try:
            mem = memories_path(thread_id)
            has_mem = mem.exists() and mem.stat().st_size > 0
        except OSError:
            has_mem = False
        summaries.append({
            "thread_id": thread_id,
            "name": state.get("name", ""),
            "turns": state.get("turns", 0),
            "messages": len(history),
            "created": state.get("created", ""),
            "updated": state.get("updated", ""),
            "parent": state.get("parent"),
            "has_base": bool(state.get("base")),
            "has_memories": has_mem,
            "preview": preview,
        })
    summaries.sort(key=lambda s: s.get("updated", ""), reverse=True)
    return summaries[:limit]


def read_thread(thread_id: str, limit: int = 100,
                agent: str = "") -> dict | None:
    """Pure read of a thread (never creates state, unlike resume).

    Returns the tail of history plus base/memories, or None when the
    thread exists neither as state nor in the audit trail.
    When agent is non-empty, cross-agent reads return None.
    """
    try:
        limit = max(1, min(int(limit or 100), 2000))
    except (TypeError, ValueError):
        limit = 100
    state = load_state(thread_id)
    source = "state"
    if state is None:
        history = replay_audit(thread_id)
        if not history:
            return None
        state = {
            "thread_id": thread_id,
            "turns": len(history) // 2,
            "base": "",
            "history": history,
        }
        source = "audit"
    # Cross-agent check: refuse to read another agent's thread.
    if agent and state.get("agent", "") != agent:
        return None
    history = state.get("history", []) or []
    return {
        "thread_id": thread_id,
        "source": source,
        "turns": state.get("turns", 0),
        "base": state.get("base", ""),
        "name": state.get("name", ""),
        "memories": read_memories(thread_id),
        "messages": history[-limit:],
        "total": len(history),
    }


IMPORT_ENTRY_CAP = 200000
IMPORT_MAX_MESSAGES = 5000


def import_history(thread_id: str, messages: list, base: str = "",
                   name: str = "", mode: str = "fail",
                   agent: str = "") -> dict:
    """Bulk-load history (migration path, e.g. imported client threads).

    mode: "fail" (refuse when the thread exists), "overwrite", "append".
    Entries must be {role: user|assistant, content: str}; oversized content
    is truncated with a marker. Returns the saved state.
    """
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    if len(messages) > IMPORT_MAX_MESSAGES:
        raise ValueError(f"too many messages (max {IMPORT_MAX_MESSAGES})")
    cleaned: list[dict] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise ValueError("each message needs role user|assistant")
        content = m.get("content", "")
        if not isinstance(content, str):
            raise ValueError("message content must be a string")
        if len(content) > IMPORT_ENTRY_CAP:
            content = content[:IMPORT_ENTRY_CAP] + "\n...[truncated on import]"
        cleaned.append({"role": m["role"], "content": content})
    existing = load_state(thread_id)
    if existing is not None:
        if mode == "fail":
            raise FileExistsError(f"thread exists: {thread_id}")
        if mode == "append":
            existing["history"].extend(cleaned)
            existing["turns"] = int(existing.get("turns", 0)) + len(cleaned) // 2
            if base:
                existing["base"] = str(base)[:40000]
            if name:
                existing["name"] = str(name)[:200]
            if agent:
                existing["agent"] = agent
            save_state(existing)
            return existing
    state = new_state(thread_id, agent=agent)
    state["history"] = cleaned
    state["turns"] = len(cleaned) // 2
    if base:
        state["base"] = str(base)[:40000]
    if name:
        state["name"] = str(name)[:200]
    save_state(state)
    return state


def archive_thread(thread_id: str, agent: str = "") -> bool:
    """Move state + memories to .archived/ (hidden from list, restorable).

    Audit history is untouched, so an archived thread stays readable via
    audit replay until unarchived.
    """
    src_state, src_mem = state_path(thread_id), memories_path(thread_id)
    if not src_state.exists():
        return False
    # Cross-agent check: refuse to archive another agent's thread.
    if agent:
        state = load_state(thread_id)
        if state and state.get("agent", "") != agent:
            return False
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        src_state.replace(ARCHIVE_DIR / src_state.name)
        if src_mem.exists():
            src_mem.replace(ARCHIVE_DIR / src_mem.name)
    except OSError:
        return False
    return True


def unarchive_thread(thread_id: str, agent: str = "") -> bool:
    """Restore an archived thread. Refuses to overwrite a live state."""
    dst_state, dst_mem = state_path(thread_id), memories_path(thread_id)
    if dst_state.exists():
        return False
    src_state = ARCHIVE_DIR / (state_path(thread_id).name)
    if not src_state.exists():
        return False
    # Cross-agent check: refuse to unarchive another agent's thread.
    if agent:
        try:
            state = json.loads(src_state.read_text())
            if state.get("agent", "") != agent:
                return False
        except Exception:
            return False
    try:
        src_state.replace(dst_state)
        src_mem = ARCHIVE_DIR / (memories_path(thread_id).name)
        if src_mem.exists():
            src_mem.replace(dst_mem)
    except OSError:
        return False
    return True


# Daemon-internal files no agent may touch through fs_* tools, except
# the owner (and setup phase, which has no agent yet). Thread state,
# memories and archives live under THREADS_DIR / ARCHIVE_DIR and are
# denied wholesale -- the daemon owns them, and thread data flows
# through the thread/* RPCs with ownership checks instead.
# Config secrets are denied by filename under CONFIG_DIR.
_FS_DENY_NAMES = frozenset({
    "providers.json", "policy.json", "agents.json", "audit.jsonl",
    "bots.autostart",
})


def fs_scope_error(agent_id: str, path, is_owner: bool = False) -> str | None:
    """Refusal message when agent_id may not touch path via fs tools.

    None means allowed (normal sandbox/approval rules still apply).
    Symlinks are resolved first so a link inside an allowed tree cannot
    smuggle a denied file past the check.
    """
    if not agent_id:
        return None
    try:
        resolved = Path(path).expanduser()
        try:
            resolved = resolved.resolve()
        except Exception:
            resolved = resolved.absolute()
    except Exception:
        return None
    try:
        threads_root = THREADS_DIR.resolve()
    except Exception:
        threads_root = THREADS_DIR
    try:
        archive_root = ARCHIVE_DIR.resolve()
    except Exception:
        archive_root = ARCHIVE_DIR
    try:
        if resolved == threads_root or threads_root in resolved.parents:
            return None if is_owner else "not permitted"
        if resolved == archive_root or archive_root in resolved.parents:
            return None if is_owner else "not permitted"
        try:
            config_root = CONFIG_DIR.resolve()
        except Exception:
            config_root = CONFIG_DIR
        if resolved.parent == config_root and \
                resolved.name in _FS_DENY_NAMES:
            return None if is_owner else "not permitted"
        bots_dir = config_root / "bots"
        if resolved.parent == bots_dir and resolved.suffix == ".pid":
            return None if is_owner else "not permitted"
    except Exception:
        return None
    return None
