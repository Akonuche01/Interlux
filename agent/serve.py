from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator

from .audit import AuditLog
from . import mcp as mcp_mod
from .pconfig import (
    CONFIG_SECTIONS,
    PROVIDER_NAME_RE,
    WEB_SEARCH_MODES,
    config_section,
    env_pinned,
    list_models,
    make_provider,
    provider_config,
    public_config_section,
    public_section,
    read_providers_file,
    write_providers_file,
)
from .policy import (
    GRANT_SCOPES,
    SANDBOX_MODES,
    allows,
    load_policy,
    policy_path,
    record_grant,
    resolve_sandbox,
    revoke,
    save_policy,
    sandbox_ctx,
)
from .providers import PROVIDERS, BaseProvider
from .skills import (
    catalog_message as skills_catalog,
    get_skill as skills_get,
    list_skills as skills_list,
    refresh as skills_refresh,
    register_skill_tool,
    scan_skill_plugins,
    skill_messages,
    USER_DIR as SKILLS_USER_DIR,
)
from .threads import (
    DEFAULT_IDENTITY,
    archive_thread,
    build_messages,
    fold_output,
    fork_state,
    import_history,
    list_threads,
    load_state,
    materialize_state,
    memories_path,
    new_state,
    read_memories,
    read_thread,
    record_turn,
    save_state,
    state_path,
    transcript_text,
    unarchive_thread,
    write_memories,
)
from .tools import EXTRA_WRITE, TOOLS, approval_label, needs_approval
from .tools.plugins import scan_plugins
from .tools.track import current_turn as turn_ctx
from .tools.track import kill_turn
from .transport import Transport, broadcast, on_disconnect, send_to

PLUGIN_DIR = Path(__file__).parent / "plugins"
scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)

# Epic D: skills load at boot; skill tool plugins use the same machinery.
register_skill_tool(TOOLS)
skills_refresh()
scan_skill_plugins(TOOLS, EXTRA_WRITE)


# Epic E: MCP proxy tools join the same registry (approval + audit via
# EXTRA_WRITE: every MCP call asks first, session-grantable like the rest).
def _mcp_register(add: list[str], remove: list[str]) -> list[str]:
    for key in remove:
        TOOLS.pop(key, None)
        EXTRA_WRITE.discard(key)
    added = []
    for key in add:
        if key in TOOLS:
            continue

        async def _proxy(_key: str = key, **kwargs):
            return await mcp_mod.call(_key, kwargs)

        TOOLS[key] = _proxy
        EXTRA_WRITE.add(key)
        added.append(key)
    if added:
        logger.info(f"mcp tools registered: {added}")
    if remove:
        logger.info(f"mcp tools removed: {remove}")
    return added


mcp_mod.set_registration_hook(_mcp_register)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("iagent")


audit = AuditLog()


_APPROVE_ALLOW_WORDS = {
    "approve", "accept", "allow", "yes", "always", "true",
    "acceptforsession",
}
_APPROVE_SESSION_WORDS = {"session", "always", "acceptforsession"}
_APPROVE_DENY_WORDS = {"deny", "decline", "refuse", "no", "false"}


def _parse_approval_decision(decision, scope: str = "turn") -> tuple[bool, str]:
    """(granted, grant_scope) from any client decision shape.

    Understands our plain words, our {decision, scope} RPC params, and
    Kara/Codex decision literals: accept (once), acceptForSession (always),
    decline, plus amendment objects (one-shot allow — persistent policy
    amendments have no home here, so they degrade loudly to a single allow
    in the daemon log, never silently).
    """
    if isinstance(decision, dict):
        text = json.dumps(decision).lower()
        if "decline" in text or "deny" in text:
            return False, "turn"
        if "acceptforsession" in text:
            return True, "session"
        if "amendment" in text:
            logger.info("policy-amendment accept degraded to one-shot allow")
        return True, "turn"
    word = str(decision or "").strip().lower()
    if word in _APPROVE_DENY_WORDS or "decline" in word or "deny" in word:
        return False, "turn"
    if word in _APPROVE_ALLOW_WORDS or scope == "session":
        # "always"/acceptForSession mean session scope even when the
        # caller left scope at its default (matches Kara's Always allow).
        if word in _APPROVE_SESSION_WORDS or scope == "session" \
                or "session" in word:
            return True, "session"
        return True, "turn"
    return False, "turn"


class ApprovalManager:
    def __init__(self):
        # fid -> {"future", "thread", "tool", "label"}
        self.pending: dict[str, dict] = {}

    def request_approval(
        self, thread_id: str, params: dict, tool: str = "", label: str = ""
    ) -> asyncio.Future:
        fid = params["id"]
        entry: dict = {
            "future": asyncio.Future(),
            "thread": thread_id,
            "tool": tool,
            "label": label,
        }
        self.pending[fid] = entry
        return entry["future"]

    def approve(self, fid: str, decision, scope: str = "turn") -> bool:
        entry = self.pending.pop(fid, None)
        if entry is None:
            return False
        granted, grant_scope = _parse_approval_decision(decision, scope)
        entry["future"].set_result(granted)
        if granted and grant_scope == "session" and entry.get("tool"):
            try:
                policy = load_policy()
                what = record_grant(
                    policy, entry["thread"], entry["tool"], entry.get("label", "")
                )
                save_policy(policy)
                audit.append({
                    "type": "grant",
                    "thread_id": entry["thread"],
                    "tool": entry["tool"],
                    "granted": what,
                    "scope": "session",
                })
                logger.info(
                    f"Session grant recorded: {entry['thread']} -> {what!r}"
                )
            except Exception:
                logger.exception("failed to record session grant")
        return True

    def drop_thread(self, thread_id: str) -> int:
        """Cancel pending approvals for a thread (turn cancelled)."""
        doomed = [
            fid for fid, entry in self.pending.items()
            if entry.get("thread") == thread_id
        ]
        for fid in doomed:
            try:
                self.pending[fid]["future"].cancel()
            except Exception:
                pass
            del self.pending[fid]
        return len(doomed)

    def drop_all(self) -> int:
        """Resolve every pending approval as denied (client gone).

        Deny, don't cancel: the turn then records the denial, completes,
        and persists — so reopening the app shows what happened instead
        of a hole where a message was.
        """
        n = 0
        for fid, entry in list(self.pending.items()):
            try:
                fut = entry.get("future")
                if fut is not None and not fut.done():
                    fut.set_result(False)
                    n += 1
            except Exception:
                pass
            self.pending.pop(fid, None)
        if n:
            logger.info(f"dropped {n} orphaned approval(s) as denied")
        return n


approvals = ApprovalManager()

RUNNING: dict[str, asyncio.Task] = {}

# Messages the user sent at a turn that is already running, keyed by turn id.
#
# This is what makes `turn/steer` mean what its name says. The old
# implementation killed the running turn and started a new one from the thread's
# last *recorded* state, so "add this to what you're doing" destroyed the work
# in progress and restarted from a stale point -- and because it then awaited
# that new turn before replying, the send button stayed disabled for the whole
# of it. A message here is picked up at the next round boundary instead: the
# current round finishes, and the agent continues with the new instruction in
# hand. Nothing is lost and nothing has to be re-run.
_pending_steer: dict[str, list[str]] = {}

# How a mid-turn message is presented to the model. Explicit about being a new
# instruction from the user, and explicit that it does not cancel the work in
# flight -- otherwise a model reads "also check X" as "stop and do X instead"
# and abandons the task it was halfway through.
_steer_prompt = (
    "\n\n[user, sent while you were working] The user added this to your "
    "current task. Continue the work in progress and take this into account; "
    "do not discard what you have already done:\n"
)

# Epic P: subagents — background turns on child threads for parallel
# fan-out. Registry is in-memory (results persist in thread history);
# a daemon restart ends running subagents (documented, never silent:
# completed work is already in history + audit).
_subagents: dict[str, dict] = {}
_sub_counters: dict[str, int] = {}
MAX_SUBAGENTS = 8
SUBAGENT_DEFAULT_ROUNDS = 3

# Runaway backstop for the agentic loop, in rounds.
#
# NOT a task budget. The loop normally ends when the model stops asking for
# tools, which is the only thing that means the work is done. This number only
# bounds a model that never stops -- a loop that cannot be trusted to terminate
# still has to be stoppable, and a runaway turn bills real tokens on every
# pass.
#
# Deliberately enormous, and paired with a wall-clock limit (TURN_TIME_LIMIT_S)
# as the limit that actually matters. A round-count cap is the wrong unit for
# real work: the old cap of 8 cut a genuine task off mid-flight, and even 400
# is only ~45 minutes at a typical 7s round -- so a 30-minute build or training
# run could still be severed by a number that has nothing to do with how long
# the work actually takes. Time is the honest unit; rounds exist only to stop a
# pathological model that returns instantly and would otherwise never reach it.
#
# When either limit IS hit the turn is reported as truncated, never finished.
ROUND_BACKSTOP = 100_000

# Wall-clock ceiling on one turn, in seconds. ~6 hours: long enough that no real
# task hits it, short enough that a stuck loop is not billed indefinitely. The
# user can always stop a turn from the UI; this only catches what nobody is
# watching.
TURN_TIME_LIMIT_S = 6 * 60 * 60


def _child_thread(parent: str) -> str:
    n = _sub_counters.get(parent, 0) + 1
    while state_path(f"{parent}-sub-{n}").exists():
        n += 1
    _sub_counters[parent] = n
    return f"{parent}-sub-{n}"


async def _run_subagent(entry: dict, payload: dict) -> None:
    """Background turn driver: records outcome, announces completion."""
    tid = entry["thread_id"]
    try:
        res = await handle_turn(payload, None)
        result = res.get("result", {}) if isinstance(res, dict) else {}
        if result.get("cancelled"):
            entry["status"] = "cancelled"
        else:
            entry["status"] = "done"
        entry["result"] = result
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        raise
    except Exception as e:
        logger.exception(f"subagent {tid} failed")
        entry["status"] = "error"
        entry["result"] = {"error": str(e)[:500]}
    finally:
        entry["task"] = None
        try:
            audit.append({"type": "subagent", "action": "completed",
                          "thread_id": tid, "parent": entry.get("parent"),
                          "status": entry["status"]})
        except Exception:
            pass
        try:
            await broadcast({"type": "subagent/completed", "id": tid,
                             "thread_id": tid, "status": entry["status"]})
        except Exception:
            logger.exception(f"subagent {tid} completion broadcast failed")

# Epic I.3 (M5): client-registered tools. The model can call out to a tool
# the CLIENT implements (e.g. Kara's ask_provider): the daemon sends
# tool/call to the owning socket and awaits its answer. Ask-first approval
# (EXTRA_WRITE), audit like everything else. Registrations live until
# unregister/restart, or until the owner proves dead on first use.
_client_tools: dict[str, dict] = {}
_tool_call_pending: dict[int, asyncio.Future] = {}
_tool_call_seq = 0
CLIENT_TOOL_TIMEOUT = 30.0
_CLIENT_TOOL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# Codex wire name so Kara's existing dispatcher fires unchanged.
CLIENT_TOOL_CALL_METHOD = "item/tool/call"


def drop_client_waits() -> None:
    """Disconnect hook: no orphaned wait may outlive its client.

    Approvals resolve as denied so the turn completes and persists;
    client-tool calls fail fast so the turn records the error instead
    of hanging on an answer that can never arrive.
    """
    try:
        approvals.drop_all()
    except Exception:
        logger.exception("drop_all approvals failed")
    for fid, fut in list(_tool_call_pending.items()):
        try:
            if fut is not None and not fut.done():
                fut.set_exception(RuntimeError("client disconnected"))
        except Exception:
            pass
        _tool_call_pending.pop(fid, None)


def _normalize_client_result(res, name: str) -> dict:
    """Client tool answer -> daemon tool result.

    Understands Codex DynamicToolCallResponse ({contentItems, success})
    and passes any other dict shape through untouched.
    """
    if isinstance(res, dict) and isinstance(res.get("contentItems"), list) \
            and isinstance(res.get("success"), bool):
        texts = []
        for item in res["contentItems"]:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        text = "\n".join(texts) or "(empty tool result)"
        if res["success"]:
            return {"status": "success", "tool": name,
                    "output": text[:20000]}
        return {"status": "error", "tool": name,
                "message": text[:4000]}
    if isinstance(res, dict):
        return res
    return {"status": "success", "output": res}


async def _call_client_tool(name: str, arguments: dict, thread_id: str = "",
                         turn_id: str = "", call_id: str = "") -> dict:
    """Execute a client tool via its owner socket. Loud on every failure.

    The request frame carries the full Codex DynamicToolCallParams shape
    (tool, arguments, callId, threadId, turnId) so Kara's dispatcher fires
    unchanged.
    """
    global _tool_call_seq
    entry = _client_tools.get(name)
    if entry is None:
        TOOLS.pop(name, None)
        EXTRA_WRITE.discard(name)
        return {"status": "error",
                "message": f"client tool unregistered: {name}"}
    _tool_call_seq += 1
    fid = _tool_call_seq
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _tool_call_pending[fid] = fut
    try:
        await send_to(entry["owner"], {
            "id": fid, "method": CLIENT_TOOL_CALL_METHOD,
            "params": {"tool": name, "arguments": arguments or {},
                       "callId": call_id or str(fid),
                       "threadId": thread_id, "turnId": turn_id},
        })
    except Exception as e:
        _client_tools.pop(name, None)
        TOOLS.pop(name, None)
        EXTRA_WRITE.discard(name)
        _tool_call_pending.pop(fid, None)
        logger.error(f"client tool {name} unreachable ({e}); unregistered")
        return {"status": "error",
                "message": f"client tool {name} unreachable: {e}"[:500]}
    try:
        res = await asyncio.wait_for(fut, timeout=CLIENT_TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        _client_tools.pop(name, None)
        TOOLS.pop(name, None)
        EXTRA_WRITE.discard(name)
        logger.error(f"client tool {name} timed out; unregistered")
        return {"status": "error",
                "message": f"client tool {name} timed out (unregistered)"}
    except Exception as e:
        # The owner answered with an error: a live tool reporting failure.
        return {"status": "error", "message": str(e)[:500]}
    finally:
        _tool_call_pending.pop(fid, None)
    return _normalize_client_result(res, name)


def _tool_output_status(output: dict) -> str:
    """Outcome status of one recorded tool entry (for items arrays)."""
    if "result" in output and isinstance(output["result"], dict):
        return str(output["result"].get("status", "success"))
    return str(output.get("status", "error"))


def agency_messages() -> list[dict]:
    """System prompt that makes the model agentic: tool catalog + calling
    convention. Without this the model never emits tool calls (it was never
    told the tools exist), which is exactly how Kara lost her agency on the
    iagent move — Codex shipped this prompt engine-side, iagent did not.

    NOTE: OpenAI-compatible providers here are single-message (only the
    last user message goes on the wire), so this must ALSO ride inside
    the user content via preamble_text() — system roles alone never
    reach the model.
    """
    return [{"role": "system", "content": agency_text()}]


def agency_text() -> str:
    from .tools import WRITE_TOOLS
    lines = [
        "You are an AGENT with tools. To act, emit a fenced json block:",
        '```json [{"tool": "<name>", "parameters": {...}}] ```',
        "Rules:",
        "- You may call several tools in one block; they run in order.",
        "- Tool results return to you next round — use them, then answer.",
        "- NEVER claim a tool ran unless you received its tool_result.",
        "- For live/external facts use web_search; your weights may be stale.",
        "- Read a file before editing it; prefer exact-match fs_edit.",
        "- Write tools (marked APPROVAL) pause for the user's approval — "
        "propose them, do not work around the pause.",
        "Your tools:",
    ]
    for name in sorted(TOOLS):
        if name in _client_tools:
            spec = _client_tools[name]
            desc = str(spec.get("description", ""))
            schema = spec.get("inputSchema") or {}
            props = schema.get("properties") if isinstance(schema, dict) else None
            params = ", ".join(sorted(props)) if isinstance(props, dict) else "..."
            lines.append(f"- {name}({params}) [app tool, APPROVAL]: {desc[:160]}")
            continue
        fn = TOOLS[name]
        try:
            sig = inspect.signature(fn)
            params = ", ".join(
                p for p in sig.parameters if p not in ("self", "cls")
            )
        except (TypeError, ValueError):
            params = "..."
        doc = (inspect.getdoc(fn) or "").splitlines()
        brief = doc[0] if doc else ""
        tag = " [APPROVAL]" if name in WRITE_TOOLS else ""
        lines.append(f"- {name}({params}){tag}: {brief[:160]}")
    try:
        mcp_names = mcp_mod.describe().get("registered_tools", []) or []
    except Exception:
        mcp_names = []
    if mcp_names:
        lines.append("MCP tools: " + ", ".join(sorted(str(t) for t in mcp_names)))
    # Skill catalog rides the preamble too: single-message providers drop
    # system roles, so the skill_messages system injection never reaches
    # the wire - without this the model cannot use any skill.
    try:
        catalog = skills_catalog()
    except Exception:
        catalog = ''
    if catalog:
        lines.append(catalog)
    lines.append(
        "Answer in the user's language, concisely. Plain text plus tool "
        "blocks only. Never echo a tool block back: after tools run, "
        "answer with the RESULT in your own words."
    )
    return "\n".join(lines)


def preamble_text(state: dict) -> str:
    """Identity + agency prompt as plain text, prepended to the user's
    message. Providers here are single-message (system roles never reach
    the wire), so without this the model would never see who it is or
    what tools it has.
    """
    developer = (state.get("developer") or "").strip()
    return f"{developer or DEFAULT_IDENTITY}\n\n{agency_text()}"


def _result_output_text(result) -> str:
    """A tool's result as the text a person would want to read.

    Tools return a dict with their own shape -- shell returns
    stdout/stderr/exit_code, others return a status and one payload field --
    so this pulls out whatever is worth showing rather than dumping JSON, which
    is what put `[fs_read]: {"ok": true}` into the transcript before.
    """
    if not isinstance(result, dict):
        return str(result) if result is not None else ""
    parts: list[str] = []
    for key in ("stdout", "output", "text", "content", "diff", "summary",
                "message", "data"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.rstrip())
    if not parts:
        return ""
    out = "\n".join(parts)
    code = result.get("exit_code")
    if isinstance(code, int) and code != 0:
        out += f"\n[exit {code}]"
    return out


def _drain_steer(turn_id: str) -> str:
    """Take the messages queued for this turn and return them as one block.

    Returns "" when there were none. Called at the round boundary. Draining
    (rather than peeking) is deliberate: a message is consumed exactly once, so
    a fold can never apply the same instruction twice.
    """
    queued = _pending_steer.get(turn_id)
    if not queued:
        return ""
    _pending_steer.pop(turn_id, None)
    text = "\n".join(queued)
    audit.append({
        "type": "steer_applied",
        "turn_id": turn_id,
        "messages": [m[:200] for m in queued],
    })
    logger.info(f"steer applied to {turn_id}: {text[:200]}")
    return text


async def run_tool_loop(turn: dict, client_socket) -> None:
    """Execute tool calls with approval flow."""
    tool_calls = turn.get("tool_calls", [])
    logger.info(f"Processing {len(tool_calls)} tool call(s)")

    sandbox_mode = turn.get("sandbox", "full") or "full"
    turn_mode = turn.get("mode", "exec") or "exec"
    thread_id = turn.get("thread_id", "default")
    # Standing grants live on disk and survive turns; load once per loop.
    policy = load_policy()

    async def complete_item(item_id: str, tool_name: str, status: str) -> None:
        # Epic I.2: item-granular timeline event for every tool outcome.
        await broadcast({
            "type": "item/completed",
            "item_id": item_id,
            "turn_id": turn.get("id"),
            "thread_id": thread_id,
            "tool": tool_name,
            "status": status,
        })

    item_idx = turn.get("_item_next", 0)
    for call in tool_calls:
        tool_name = call.get("tool", "shell")
        params = call.get("parameters", {})
        logger.info(f"Tool: {tool_name}, params: {params}")
        item_id = f"{turn.get('id')}:item:{item_idx}"
        item_idx += 1
        turn["_item_next"] = item_idx
        await broadcast({
            "type": "item/started",
            "item_id": item_id,
            "turn_id": turn.get("id"),
            "thread_id": thread_id,
            "tool": tool_name,
            "label": approval_label(tool_name, params),
        })

        if needs_approval(tool_name, params):
            # Epic G: plan mode blocks writes explicitly, like read-only
            # sandbox but as a declared intent ("look, don't touch").
            reason = ""
            if sandbox_mode == "read-only":
                reason = "sandbox is read-only"
            elif turn_mode == "plan":
                reason = "turn is plan mode (no writes)"
            if reason:
                logger.info(f"Blocked ({reason}): {tool_name}")
                blocked = {
                    "tool": tool_name,
                    "status": "error",
                    "message": f"write tool {tool_name!r} blocked: {reason}",
                }
                turn["output"].append(blocked)
                await broadcast({"type": "tool_result", **blocked})
                await complete_item(item_id, tool_name, "blocked")
                continue

            label = approval_label(tool_name, params)
            if allows(policy, thread_id, tool_name, label):
                logger.info(
                    f"Standing session grant covers {tool_name} ({label!r}) "
                    f"— skipping approval"
                )
            else:
                fid = f"{thread_id}:{len(approvals.pending)}"
                approval_params = {"command": label, "id": fid,
                                   "tool": tool_name,
                                   "thread_id": thread_id,
                                   "turn_id": str(turn.get("id", ""))}
                if isinstance(params, dict) and params.get("path"):
                    approval_params["path"] = str(params["path"])[:500]

                approval_future = approvals.request_approval(
                    thread_id, approval_params, tool=tool_name, label=label
                )
                logger.info(f"Requesting approval: {fid}")
                await broadcast({
                    "type": "approval_request",
                    "id": fid,
                    "params": approval_params,
                })

                logger.info("Waiting for client decision...")
                granted = await approval_future
                if not granted:
                    logger.info(f"Denied by client: {fid} — skipping")
                    denied = {"tool": tool_name, "status": "denied", "message": "denied by client"}
                    turn["output"].append(denied)
                    await broadcast({"type": "tool_result", **denied})
                    await complete_item(item_id, tool_name, "denied")
                    continue
                logger.info("Approval received!")

        logger.info(f"Executing tool: {tool_name}")
        if tool_name in TOOLS:
            try:
                if tool_name in _client_tools:
                    result = await _call_client_tool(
                        tool_name, params, thread_id=thread_id,
                        turn_id=str(turn.get("id", "")), call_id=item_id)
                else:
                    result = await TOOLS[tool_name](**params)
                logger.info(f"Tool result: {result}")
                tool_output = {"tool": tool_name, "result": result}
                turn["output"].append(tool_output)
                # Broadcast the output as a delta so the activity card can fill
                # in as the command runs.
                #
                # Without this the card knew only *that* a tool was called and
                # never *what it printed*: `item/started` carries the label and
                # `item/completed` carries only a status, so the row stayed
                # empty for the whole call and the command's output appeared
                # nowhere on screen. The tools capture stdout/stderr already
                # (see tools/shell.py); this is what puts it on the wire.
                #
                # Sent on completion rather than streamed live — these tools
                # return one dict when the process exits, so there is nothing
                # to stream. What this fixes is the empty card, and it arrives
                # the moment the command finishes instead of never.
                _out = _result_output_text(result)
                if _out:
                    await broadcast({
                        "type": "item/output",
                        "item_id": item_id,
                        "turn_id": turn.get("id"),
                        "thread_id": thread_id,
                        "output": _out,
                    })
                await broadcast({"type": "tool_result", **tool_output})
                status = result.get("status", "success") if isinstance(
                    result, dict) else "success"
                await complete_item(item_id, tool_name, str(status))
                if (tool_name in ("fs_write", "fs_edit", "image_generate")
                        and isinstance(result, dict)
                        and result.get("status") == "success"
                        and result.get("path")):
                    await broadcast({
                        "type": "fs/changed",
                        "op": tool_name,
                        "path": str(result["path"]),
                        "turn_id": turn.get("id"),
                        "thread_id": thread_id,
                    })
                if (tool_name == "review" and isinstance(result, dict)
                        and result.get("status") == "success"):
                    await broadcast({
                        "type": "diff/updated",
                        "turn_id": turn.get("id"),
                        "thread_id": thread_id,
                        "repo": str(params.get("cwd", ".")),
                        "diff_stat": str(result.get("diff_stat", ""))[:2000],
                        "summary": str(result.get("summary", ""))[:500],
                    })
            except Exception as e:
                logger.error(f"Tool execution failed: {e}")
                tool_output = {"tool": tool_name, "status": "error", "message": str(e)}
                turn["output"].append(tool_output)
                await broadcast({"type": "tool_result", **tool_output})
                await complete_item(item_id, tool_name, "error")
        else:
            logger.error(f"Unknown tool: {tool_name}")
            tool_output = {"tool": tool_name, "status": "error", "message": f"unknown tool: {tool_name}"}
            turn["output"].append(tool_output)
            await broadcast({"type": "tool_result", **tool_output})
            await complete_item(item_id, tool_name, "error")


async def _execute_turn(
    payload: dict,
    params: dict,
    user_msg: str,
    provider: BaseProvider | None,
    model: str,
    thread_id: str,
    turn: dict,
    client_socket,
    state: dict,
    extra_system: list[dict],
) -> dict:
    images = params.get("images") or []
    api = params.get("api", "chat")
    if api not in ("chat", "responses"):
        api = "chat"
    stream = params.get("stream", True)
    if not isinstance(stream, bool):
        stream = True

    turn["output"] = []
    turn["rounds"] = 0
    turn["truncated"] = False
    # Wall-clock budget for this turn, in seconds. Time is the honest unit for
    # "how long may this run" -- a round count cannot tell a 30-minute training
    # run from a 30-second one, and a cap in the wrong unit is what severed real
    # work before. Checked at the round boundary, so a long tool call is never
    # cut off mid-flight; only the loop continuing past it can trip this.
    _deadline = time.monotonic() + TURN_TIME_LIMIT_S
    # The agentic loop is no longer round-capped. It used to be: 8 from Kara,
    # clamped to 10 here, and it cut turns off mid-task -- 17 tool calls'
    # worth of results never made it into an answer, and because the last round
    # was stamped `final`, the client reported a truncated turn as a finished
    # one. That is the "says Done while still working" and "ends half" report.
    #
    # The loop now ends when the model stops asking for tools, which is the
    # only condition that actually means the work is done. The bound below is a
    # runaway backstop, not a budget: it exists so a model that never stops
    # calling tools cannot spin forever, and it is set far above any real task.
    try:
        max_rounds = int(params.get("max_rounds", 0) or 0)
    except (TypeError, ValueError):
        max_rounds = 0
    if max_rounds <= 0:
        max_rounds = ROUND_BACKSTOP
    else:
        max_rounds = max(1, min(max_rounds, ROUND_BACKSTOP))

    # Single-message providers only ever see the last user message, so the
    # identity + tool prompt must ride INSIDE it (system roles are built
    # too, for providers that honor them, but they never reach this wire).
    working_content = preamble_text(state) + "\n\n---\n\n" + user_msg
    while True:
        turn["rounds"] += 1
        rnd = turn["rounds"]
        round_messages = build_messages(state, thread_id, working_content,
                                          extra_system)
        has_error = False
        round_text_parts: list[str] = []
        # Output index where this round's deltas start (used to re-record
        # cleaned text after tool-call scrubbing below).
        round_mark = len(turn["output"])

        async for delta in stream_turn(provider, round_messages, model,
                                       images, api, stream):
            if delta.get("type") == "usage" and isinstance(delta.get("usage"), dict):
                # Epic F: cost visibility. Live broadcast; kept out of output
                # so it never pollutes history or the model context.
                turn["usage"] = delta["usage"]
                await broadcast({
                    "type": "usage", "turn_id": turn["id"],
                    "usage": delta["usage"],
                })
                continue
            turn["output"].append(delta)
            if delta.get("type") == "text_delta":
                round_text_parts.append(delta.get("content", ""))
            if delta.get("type") == "error":
                has_error = True
            await _send_delta(delta, turn.get("id", ""), thread_id,
                              round_no=rnd)

        # This round's model text (history/context folding happens below).
        round_text = "".join(round_text_parts)
        if round_text:
            logger.info(f"Round {rnd} content: {round_text[:200]}")

        if has_error:
            turn["complete"] = True
            try:
                audit.append({"type": "turn", "thread_id": thread_id, **turn})
            except Exception:
                pass
            errmsg = next((
                str(d.get("message") or d.get("content", ""))
                for d in turn["output"]
                if isinstance(d, dict) and d.get("type") == "error"
            ), "turn failed")
            await broadcast({"type": "error", "turn_id": turn["id"],
                             "thread_id": thread_id,
                             "message": errmsg[:1000]})
            result = {"turn_id": turn["id"], "rounds": turn["rounds"]}
            if turn.get("usage"):
                result["usage"] = turn["usage"]
            return {"id": payload.get("id"), "result": result}

        # Parse tool calls from this round's content only.
        tool_calls: list = []
        if round_text:
            import re
            # Look for JSON blocks like ```json {...} ```
            json_blocks = re.findall(r'```json\s*(.*?)\s*```', round_text, re.DOTALL)
            if json_blocks:
                try:
                    tool_calls = json.loads(json_blocks[0])
                    if isinstance(tool_calls, list):
                        pass
                    elif "tool_calls" in tool_calls:
                        tool_calls = tool_calls["tool_calls"]
                    else:
                        tool_calls = []
                except json.JSONDecodeError:
                    tool_calls = []
                if not isinstance(tool_calls, list):
                    tool_calls = []
        turn["tool_calls"] = tool_calls
        if tool_calls:
            # Hygiene: an executed call block is machine traffic, not chat.
            # Scrub it from the JOINED round text (live chunks fragment the
            # block, so per-entry regex never matches), then re-record this
            # round's text as one clean entry. History, resume, and later
            # folds stay clean. (Live deltas already streamed; clients
            # render those as activity.)
            block_re = re.compile(r'```json\s*.*?\s*```', re.DOTALL)
            stripped, n = block_re.subn("", round_text, count=1)
            if n:
                round_text = stripped.strip()
                turn["output"] = [
                    e for i, e in enumerate(turn["output"])
                    if i < round_mark or not (
                        isinstance(e, dict)
                        and e.get("type") == "text_delta")
                ]
                if round_text:
                    turn["output"].append(
                        {"type": "text_delta", "content": round_text})

        # Fallback, round 1 only: tool call from a "run " user message.
        if rnd == 1 and not tool_calls and user_msg.strip().lower().startswith("run "):
            command = user_msg.strip()[4:].strip()
            turn["tool_calls"] = [{
                "tool": "shell",
                "parameters": {"command": command}
            }]

        # Announce this round before acting on it. The client cannot classify
        # model text on its own -- narration and the final answer are the same
        # shape -- so the daemon, which knows whether it is about to loop again,
        # says so. `final` is computed from the same two conditions the loop
        # below exits on, so it cannot disagree with what actually happens: no
        # tool calls means this round was the answer, and reaching max_rounds
        # means no further round will revise it.
        will_loop = bool(turn["tool_calls"]) and rnd < max_rounds
        if round_text:
            await broadcast({
                "type": "round/completed",
                "turn_id": turn["id"],
                "thread_id": thread_id,
                "round": rnd,
                "text": round_text,
                # Not `final` when the backstop is what stopped the loop: the
                # model still wanted to keep going, so this round is working
                # narration and claiming otherwise is what told the client a
                # half-finished turn was complete.
                "final": not will_loop and not turn.get("truncated"),
                "truncated": bool(turn.get("truncated")),
            })

        # Wall-clock ceiling. Checked at the round boundary, so a long tool call
        # is never severed mid-flight -- only the loop choosing to continue past
        # it can trip this. `time` is the honest unit for "how long may this
        # run"; a round count cannot tell a 30-minute training run from a
        # 30-second one, and that wrong unit is what cut real work short.
        if time.monotonic() > _deadline:
            logger.info(
                f"turn time limit ({TURN_TIME_LIMIT_S}s) reached — TRUNCATED "
                f"after {rnd} rounds"
            )
            turn["truncated"] = True
            break

        if not turn["tool_calls"]:
            # The model asked for nothing more. If the user spoke up while this
            # round was running, that message is the answer to a question they
            # asked mid-task, so it earns a round of its own rather than being
            # dropped -- an instruction the user typed must never be silently
            # discarded. No sentinel tool call: the fold is what actually
            # carries the message into the next round.
            _steered = _drain_steer(turn["id"])
            if _steered:
                working_content += _steer_prompt + _steered
                continue
            break  # pure answer — done

        # A message sent while this round was running joins the next one. The
        # current round is left alone: it is real work, and restarting it would
        # throw away whatever the agent just did.
        _steered = _drain_steer(turn["id"])
        if _steered:
            working_content += _steer_prompt + _steered
            logger.info(f"steer folded into round {rnd + 1}")

        mark = len(turn["output"])
        await run_tool_loop(turn, client_socket)

        if rnd >= max_rounds:
            # The backstop, not a budget. Mark the turn truncated so the client
            # stops calling it finished -- the old code just broke here and the
            # last round was stamped `final`, which is how a cut-off turn
            # rendered as a completed one.
            logger.info(f"round backstop ({max_rounds}) reached — TRUNCATED")
            turn["truncated"] = True
            break
        # Fold this round (model text + tool outcomes) into next round's
        # context. Providers stay single-message; the fold carries history.
        working_content += (
            f"\n\n[assistant round {rnd}]: {round_text}"
            + fold_output(turn["output"][mark:])
        )

    turn["complete"] = True
    # Epic C: persist history so later turns / resume see this conversation.
    try:
        record_turn(state, user_msg, turn["output"])
    except Exception:
        logger.exception("failed to persist thread state")
    try:
        audit.append({"type": "turn", "thread_id": thread_id, **turn})
    except Exception:
        pass
    # Epic I.2: one rich terminal event; legacy `complete` kept as alias.
    # The items array is the reconcile authority for rich timelines.
    tool_entries = [
        o for o in turn["output"]
        if isinstance(o, dict) and o.get("tool")
    ]
    tools_run = [o.get("tool") for o in tool_entries]
    items = [{
        "id": f"{turn['id']}:item:{i}",
        "tool": o.get("tool"),
        "status": _tool_output_status(o),
    } for i, o in enumerate(tool_entries)]
    completed = {
        "type": "turn/completed",
        "turn_id": turn["id"],
        "thread_id": thread_id,
        "mode": turn.get("mode", "exec"),
        "tools": tools_run,
        "items": items,
        # The turn stopped because the backstop was hit, not because the work
        # was done. The client needs this to stop reporting an unfinished turn
        # as a finished one.
        "truncated": bool(turn.get("truncated")),
        "rounds": turn["rounds"],
    }
    if turn.get("usage"):
        completed["usage"] = turn["usage"]
    await broadcast(completed | {"type": "complete"})
    await broadcast(completed)

    result = {"turn_id": turn["id"], "rounds": turn["rounds"]}
    if turn.get("usage"):
        result["usage"] = turn["usage"]
    return {"id": payload.get("id"), "result": result}


async def handle_turn(payload: dict, client_socket) -> dict:
    logger.info(f"handle_turn called with payload: {payload}")
    params = payload.get("params", {})
    user_msg = params.get("user", "")

    thread_id = params.get("thread_id", f"thread-{len(audit.read_last())}")
    # Epic C: thread state = history + base + turn counter (wipe-proof home).
    state = load_state(thread_id) or new_state(thread_id)
    # Provider + model: turn param wins, then policy default, then built-in.
    policy = load_policy()
    provider_name = (params.get("provider")
                     or policy.get("default_provider") or "openai")
    model = (params.get("model")
             or policy.get("default_model") or "gpt-4o")
    # Epic B: turn param wins; policy.json default otherwise.
    sandbox = resolve_sandbox(params, policy)
    # Epic G: turn mode — "exec" (default) or "plan" (no writes, explicit).
    mode = params.get("mode", "exec")
    if mode not in ("exec", "plan"):
        mode = "exec"
    turn = {
        "id": f"{thread_id}:{state['turns']}",
        "user": user_msg,
        "thread_id": thread_id,
        "sandbox": sandbox["mode"],
        "mode": mode,
    }

    provider = make_provider(provider_name)

    # Epic I.2: lifecycle vocabulary for rich timelines.
    is_new_thread = (
        state["turns"] == 0 and not state.get("history")
        and not state.get("base")
    )
    if is_new_thread:
        await broadcast({"type": "thread/started", "thread_id": thread_id})
    await broadcast({
        "type": "turn/started",
        "turn_id": turn["id"],
        "thread_id": thread_id,
        "mode": mode,
        "sandbox": sandbox["mode"],
        "provider": provider_name,
    })

    RUNNING[turn["id"]] = asyncio.current_task()
    token = turn_ctx.set(thread_id)
    sandbox_token = sandbox_ctx.set(sandbox)
    try:
        # Epic D: skill catalog (+ requested bodies) ride as system messages
        # inside build_messages, rebuilt every Epic J round. The agency
        # prompt (tools + calling convention) rides in front of them.
        return await _execute_turn(
            payload, params, user_msg, provider, model,
            thread_id, turn, client_socket, state,
            agency_messages() + skill_messages(params.get("skills")),
        )
    except asyncio.CancelledError:
        kill_turn(turn["id"])
        approvals.drop_thread(thread_id)
        turn["cancelled"] = True
        try:
            audit.append({"type": "turn", "thread_id": thread_id, **turn})
        except Exception:
            pass
        await broadcast({"type": "cancelled", "turn_id": turn["id"]})
        await broadcast({"type": "turn/completed", "turn_id": turn["id"],
                         "thread_id": thread_id, "cancelled": True})
        return {
            "id": payload.get("id"),
            "result": {"turn_id": turn["id"], "cancelled": True},
        }
    finally:
        RUNNING.pop(turn["id"], None)
        sandbox_ctx.reset(sandbox_token)
        turn_ctx.reset(token)


async def handle_request(payload: dict, client_socket) -> dict | None:
    """Process JSON-RPC request."""
    try:
        method = payload.get("method")
        params = payload.get("params", {})
        request_id = payload.get("id")

        if method is None and ("result" in payload or "error" in payload):
            # Answer to a daemon-initiated tool/call (M5). Nothing to send
            # back — transport skips None responses.
            fut = _tool_call_pending.pop(payload.get("id"), None)
            if fut is not None and not fut.done():
                if "error" in payload:
                    fut.set_exception(
                        RuntimeError(str(payload["error"])[:500]))
                else:
                    fut.set_result(payload.get("result"))
                return None
            # Kara move: her app answers approval cards with a method-less
            # frame (Codex wire shape) carrying the fid as id. Route those
            # to the approval table instead of dropping them — a dropped
            # answer stalls the turn forever with no error surfaced, which
            # reads as "answers halfway then nothing lands".
            fid = payload.get("id")
            if isinstance(fid, str) and fid in approvals.pending:
                if "error" in payload:
                    approvals.approve(fid, "decline")
                else:
                    approvals.approve(fid, payload.get("result"))
                logger.info(f"approval answered method-less: {fid}")
            return None

        if method == "capabilities":
            return {
                "id": request_id,
                "result": {
                    "protocol": 1,
                    "methods": [
                        "turn", "cancel", "capabilities", "approve",
                        "initialize", "command/exec",
                        "tools_refresh", "tools", "tools/register",
                        "tools/unregister", "policy", "skills", "mcp",
                        "providers", "config/section", "turn/steer",
                        "model/list",
                        "thread/resume", "thread/fork", "thread/compact",
                        "thread/list", "thread/read", "thread/archive",
                        "thread/unarchive", "thread/unsubscribe",
                        "thread/name/set", "thread/import", "memories",
                        "subagent/spawn", "subagent/status",
                        "subagent/result", "subagent/list",
                        "subagent/cancel",
                    ],
                    "stream": True,
                    "providers": list(PROVIDERS.keys()),
                    "tools": sorted(TOOLS.keys()),
                    "media": ["image"],
                    "apis": ["chat", "responses"],
                    "modes": ["exec", "plan"],
                    "sandboxes": list(SANDBOX_MODES),
                    "approve_scopes": list(GRANT_SCOPES),
                },
            }

        if method == "tools_refresh":
            # Plugins, skills (incl. their tool dirs), AND MCP servers.
            loaded = scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)
            skills_refresh()
            loaded += scan_skill_plugins(TOOLS, EXTRA_WRITE)
            loaded += await mcp_mod.start_all()
            await broadcast({
                "type": "skills/changed",
                "skills": [s["name"] for s in skills_list()],
            })
            return {
                "id": request_id,
                "result": {"loaded": loaded, "tools": sorted(TOOLS.keys())},
            }

        if method == "mcp":
            # Server status; {"restart": true} tears down and re-spawns all.
            if params.get("restart"):
                await mcp_mod.stop_all()
                await mcp_mod.start_all()
            return {"id": request_id, "result": mcp_mod.describe()}

        if method == "skills":
            # List loaded skills, or load one body; refresh re-reads disk.
            if params.get("refresh"):
                skills_refresh()
                scan_skill_plugins(TOOLS, EXTRA_WRITE)
                await broadcast({
                    "type": "skills/changed",
                    "skills": [s["name"] for s in skills_list()],
                })
            if params.get("load"):
                skill = skills_get(str(params["load"]))
                if skill is None:
                    return {
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": f"unknown skill: {params['load']}",
                        },
                    }
                return {
                    "id": request_id,
                    "result": {
                        k: skill[k]
                        for k in ("name", "description", "tools", "body", "path", "source")
                    },
                }
            return {
                "id": request_id,
                "result": {
                    "skills": skills_list(),
                    "user_dir": str(SKILLS_USER_DIR),
                },
            }

        if method == "turn":
            return await handle_turn(payload, client_socket)

        if method == "approve":
            fid = params.get("id")
            decision = params.get("decision")
            scope = params.get("scope", "turn")
            if scope not in GRANT_SCOPES:
                scope = "turn"
            if approvals.approve(fid, decision, scope):
                return {"id": request_id, "result": True}
            return {"id": request_id, "error": {"code": -32602, "message": "unknown fid"}}

        if method == "policy":
            # Get/set sandbox + provider defaults, revoke standing grants.
            policy = load_policy()
            revoked = None
            changed = False
            if params.get("sandbox") in SANDBOX_MODES:
                policy["sandbox"] = params["sandbox"]
                changed = True
            if isinstance(params.get("provider"), str) and params["provider"].strip():
                policy["default_provider"] = params["provider"].strip()
                changed = True
            if isinstance(params.get("model"), str) and params["model"].strip():
                policy["default_model"] = params["model"].strip()
                changed = True
            # Kara move: her approval_policy ("never" default) maps here so
            # the daemon auto-grants instead of stalling on cards nobody
            # answers. Anything else clears back to ask-everything.
            if "approval_mode" in params:
                mode = str(params.get("approval_mode") or "").strip()
                if mode == "never":
                    policy["approval_mode"] = "never"
                else:
                    policy.pop("approval_mode", None)
                changed = True
            if "revoke" in params:
                revoked = revoke(policy, str(params["revoke"]))
                changed = True
            if changed:
                save_policy(policy)
            result = {"policy": policy, "path": str(policy_path())}
            if revoked is not None:
                result["revoked"] = revoked
            return {"id": request_id, "result": result}

        if method == "providers":
            # Key management over the wire (M3). Secrets travel inbound
            # only; reads return masked shapes, the audit never sees a key.
            name = str(params.get("provider") or "")
            if "api_key" in params or "base_url" in params or params.get("delete"):
                if not PROVIDER_NAME_RE.fullmatch(name):
                    return {
                        "id": request_id,
                        "error": {"code": -32602, "message": "bad provider name"},
                    }
                if env_pinned():
                    return {
                        "id": request_id,
                        "error": {"code": -32602, "message": (
                            "config is env-pinned (INTERLUX_PROVIDERS); "
                            "file write refused")},
                    }
                data = read_providers_file()
                if params.get("delete"):
                    removed = data.pop(name, None) is not None
                    if removed:
                        write_providers_file(data)
                        audit.append({"type": "provider_config",
                                      "provider": name, "deleted": True})
                    return {"id": request_id, "result": {
                        "provider": name, "deleted": removed}}
                section = data.setdefault(name, {})
                if not isinstance(section, dict):
                    section = data[name] = {}
                fields = []
                if "api_key" in params:
                    section["api_key"] = str(params["api_key"] or "")
                    fields.append("api_key")
                if "base_url" in params:
                    section["base_url"] = str(params["base_url"] or "")
                    fields.append("base_url")
                write_providers_file(data)
                audit.append({"type": "provider_config", "provider": name,
                              "fields": fields})
                return {"id": request_id, "result": {
                    **public_section(name, section), "updated": fields}}
            if name:
                return {"id": request_id, "result": public_section(
                    name, config_section(name))}
            # Union of known adapters + configured sections: every provider
            # the daemon can serve gets a block (keyless ones show
            # has_key=false with the default base_url) so clients can
            # display, save keys for, and switch to all of them. Kara's
            # settings + model picker broke on iagent because only
            # configured sections were listed (just tokenharbor).
            from .pconfig import DEFAULT_BASES
            seen = provider_config()
            names = sorted(
                set(PROVIDERS)
                | {k for k, v in seen.items() if isinstance(v, dict)}
            )
            out = []
            for n in names:
                section = seen.get(n)
                if not isinstance(section, dict):
                    section = {}
                else:
                    section = dict(section)
                section.setdefault("base_url", DEFAULT_BASES.get(n, ""))
                out.append(public_section(n, section))
            return {"id": request_id, "result": {"providers": out}}

        if method == "config/section":
            # One non-provider section of the provider config -- today only
            # `web_search`. There is no `config/read` on this daemon, so the
            # section is addressed on its own terms; wrapping it in a method
            # name that implies a general config system would be the lie.
            # Writes copy `providers` exactly: refuse when env-pinned, audit
            # what changed, atomic replace.
            name = str(params.get("name") or "")
            if name not in CONFIG_SECTIONS:
                return {"id": request_id, "error": {"code": -32602,
                        "message": (f"unknown config section: "
                                    f"{name or '(empty)'} (known: "
                                    f"{', '.join(CONFIG_SECTIONS)})")}}
            if "set" in params:
                if env_pinned():
                    return {"id": request_id, "error": {"code": -32602, "message": (
                            "config is env-pinned (INTERLUX_PROVIDERS); "
                            "file write refused")}}
                patch = params["set"]
                if not isinstance(patch, dict):
                    return {"id": request_id, "error": {"code": -32602,
                            "message": "set must be an object"}}
                unknown = sorted(k for k in patch if k != "mode")
                if unknown:
                    return {"id": request_id, "error": {"code": -32602,
                            "message": f"unmapped config key: {unknown[0]}"}}
                mode = str(patch.get("mode", "live") or "live").strip().lower()
                if mode not in WEB_SEARCH_MODES:
                    return {"id": request_id, "error": {"code": -32602,
                            "message": (f"bad web_search mode: {mode!r} "
                                        f"(one of {', '.join(WEB_SEARCH_MODES)})")}}
                data = read_providers_file()
                section = data.setdefault("web_search", {})
                if not isinstance(section, dict):
                    section = data["web_search"] = {}
                section["mode"] = mode
                write_providers_file(data)
                audit.append({"type": "config_section",
                              "section": "web_search", "mode": mode})
                logger.info(f"web_search section mode set to {mode}")
            return {"id": request_id, "result": public_config_section(
                "web_search", config_section("web_search"))}

        if method == "model/list":
            # Per-provider model enumeration: config override > live
            # /models > curated fallback. Never carries secrets.
            name = str(params.get("provider") or "")
            if name:
                if name not in PROVIDERS:
                    return {
                        "id": request_id,
                        "error": {"code": -32602,
                                 "message": f"unknown provider: {name}"},
                    }
                return {"id": request_id,
                        "result": await list_models(name)}
            results = await asyncio.gather(*[
                list_models(p) for p in PROVIDERS
            ])
            return {"id": request_id, "result": {
                "providers": {r["provider"]: r for r in results}}}

        if method == "tools/register":
            # M5: a client offers a tool the daemon calls back out to.
            name = str(params.get("name") or "")
            if not _CLIENT_TOOL_RE.fullmatch(name):
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "bad tool name"},
                }
            if name in TOOLS and name not in _client_tools:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"name taken: {name}"},
                }
            description = str(params.get("description") or "")[:500]
            spec = params.get("inputSchema")
            if not isinstance(spec, dict):
                spec = {"type": "object"}
            _client_tools[name] = {"description": description,
                                   "inputSchema": spec,
                                   "owner": client_socket}

            async def _client_proxy(_name: str = name, **kwargs):
                return await _call_client_tool(_name, kwargs)

            TOOLS[name] = _client_proxy
            EXTRA_WRITE.add(name)
            audit.append({"type": "client_tool", "action": "register",
                          "name": name})
            return {"id": request_id, "result": {
                "name": name, "description": description,
                "inputSchema": spec}}

        if method == "tools/unregister":
            name = str(params.get("name") or "")
            entry = _client_tools.get(name)
            if entry is None:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown client tool: {name}"},
                }
            if entry["owner"] is not client_socket:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "not the owning connection"},
                }
            _client_tools.pop(name, None)
            TOOLS.pop(name, None)
            EXTRA_WRITE.discard(name)
            audit.append({"type": "client_tool", "action": "unregister",
                          "name": name})
            return {"id": request_id, "result": {"name": name,
                                                 "deleted": True}}

        if method == "tools":
            return {"id": request_id, "result": {
                "tools": sorted(TOOLS.keys()),
                "client_tools": sorted(_client_tools),
                "client_specs": {
                    n: {"description": e.get("description", ""),
                        "inputSchema": e.get("inputSchema", {})}
                    for n, e in sorted(_client_tools.items())
                },
                "mcp_tools": sorted(
                    mcp_mod.describe().get("registered_tools", [])),
            }}

        if method == "thread/list":
            # Newest-first summaries for history drawers. Pure read.
            try:
                limit = int(params.get("limit", 50))
            except (TypeError, ValueError):
                limit = 50
            return {
                "id": request_id,
                "result": {"threads": list_threads(limit)},
            }

        if method == "thread/read":
            # Pure read of one thread (never creates state, unlike resume).
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            try:
                limit = int(params.get("limit", 100))
            except (TypeError, ValueError):
                limit = 100
            data = read_thread(tid, limit)
            if data is None:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": f"unknown thread: {tid}"},
                }
            return {"id": request_id, "result": data}

        if method == "initialize":
            # Kara-compat handshake (her client sends this first). Benign
            # server description; per-turn auth is not required on loopback.
            return {"id": request_id, "result": {
                "server": "iagent", "protocol": 1,
                "client": params.get("clientInfo", {}),
            }}

        if method == "thread/archive":
            # Hide from thread/list (restorable). Audit history untouched.
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            if not archive_thread(tid):
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            audit.append({"type": "thread", "action": "archive",
                          "thread_id": tid})
            return {"id": request_id, "result": {"thread_id": tid,
                                                 "archived": True}}

        if method == "thread/unarchive":
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            if not unarchive_thread(tid):
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": (
                        f"cannot unarchive: {tid}")},
                }
            audit.append({"type": "thread", "action": "unarchive",
                          "thread_id": tid})
            return {"id": request_id, "result": {"thread_id": tid,
                                                 "archived": False}}

        if method == "thread/unsubscribe":
            # Hygiene no-op (like Kara's): nothing server-pushed to stop.
            return {"id": request_id, "result": True}

        if method == "thread/import":
            # Migration path (Kara move): bulk-load history from another
            # system, e.g. codex thread/read output. Validated + audited.
            tid = str(params.get("thread_id") or "").strip()
            mode = str(params.get("mode", "fail") or "fail").lower()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            if mode not in ("fail", "overwrite", "append"):
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "mode must be fail|overwrite|append"},
                }
            try:
                state = import_history(
                    tid, params.get("messages"),
                    base=params.get("base", ""),
                    name=params.get("name", ""), mode=mode)
            except FileExistsError as e:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            except ValueError as e:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            audit.append({"type": "thread", "action": "import",
                          "thread_id": tid, "turns": state["turns"],
                          "mode": mode,
                          "source": str(params.get("source", "migration"))[:100]})
            return {"id": request_id, "result": {
                "thread_id": tid, "turns": state["turns"],
                "messages": len(state["history"]), "mode": mode}}

        if method == "thread/name/set":
            tid = str(params.get("thread_id") or "").strip()
            name = str(params.get("name") or "").strip()[:200]
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            state = load_state(tid)
            if state is None:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            state["name"] = name
            save_state(state)
            audit.append({"type": "thread", "action": "name",
                          "thread_id": tid, "name": name})
            await broadcast({"type": "thread/name/updated",
                             "thread_id": tid, "name": name})
            return {"id": request_id, "result": {"thread_id": tid,
                                                 "name": name}}

        if method == "thread/resume":
            # Open a thread: state file first, audit replay fallback,
            # brand-new empty thread otherwise (idempotent).
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            state, source = materialize_state(tid)
            # Kara move: thread/start carries developerInstructions (persona)
            # and the adapter forwards them here; persist so every later
            # turn (and resume/fork) injects them as system messages.
            dev = params.get("developer_instructions")
            if isinstance(dev, str) and dev.strip():
                dev = dev.strip()[:4000]
                if state.get("developer") != dev:
                    state["developer"] = dev
                    try:
                        save_state(state)
                    except Exception:
                        logger.exception("failed to persist developer prompt")
            return {
                "id": request_id,
                "result": {
                    "thread_id": tid,
                    "source": source,
                    "turns": state["turns"],
                    "base": state.get("base", ""),
                    "history": state["history"],
                    "memories": read_memories(tid),
                    "path": str(state_path(tid)),
                },
            }

        if method == "thread/fork":
            src = str(params.get("thread_id") or "").strip()
            if not src:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            dst = str(params.get("to") or params.get("new_thread_id") or "").strip()
            if not dst:
                dst, n = f"{src}-fork", 2
                while state_path(dst).exists():
                    dst, n = f"{src}-fork-{n}", n + 1
            elif state_path(dst).exists():
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": f"target thread exists: {dst}"},
                }
            try:
                forked = fork_state(src, dst)
            except FileNotFoundError as e:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            audit.append({"type": "fork", "from": src, "to": dst, "thread_id": dst})
            return {
                "id": request_id,
                "result": {
                    "thread_id": dst,
                    "from": src,
                    "turns": forked["turns"],
                    "history_len": len(forked["history"]),
                    "base": forked.get("base", ""),
                    "path": str(state_path(dst)),
                },
            }

        if method == "thread/compact":
            # Model summarizes the transcript into a persistent base message;
            # history resets; audit keeps a compact marker.
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            state, _source = materialize_state(tid)
            if not state["history"]:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "nothing to compact"},
                }
            provider = make_provider(str(params.get("provider", "openai")))
            if provider is None:
                return {
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": f"provider not available: {params.get('provider', 'openai')}",
                    },
                }
            prompt = (
                "Summarize this terminal-agent conversation for continuity. "
                "Keep: decisions made, file paths, commands run, results that "
                "matter, and open tasks. Drop chatter. Be dense and factual."
                "\n\n" + transcript_text(state)
            )
            summary, err = "", None
            async for delta in provider.stream_turn(
                [{"role": "user", "content": prompt}],
                model=str(params.get("model", "gpt-4o")),
                images=None,
                api="chat",
                stream=False,
            ):
                if delta.get("type") == "text_delta":
                    summary += delta.get("content", "")
                elif delta.get("type") == "error":
                    err = delta.get("message", "provider error")
            if err or not summary.strip():
                return {
                    "id": request_id,
                    "error": {"code": -32700, "message": f"compact failed: {err or 'empty summary'}"},
                }
            prior_turns = state["turns"]
            state["base"] = summary.strip()
            state["history"] = []
            save_state(state)
            audit.append({
                "type": "compact",
                "thread_id": tid,
                "summary": state["base"],
                "turns_before": prior_turns,
            })
            await broadcast({"type": "thread/compacted", "thread_id": tid,
                             "turns_before": prior_turns})
            return {
                "id": request_id,
                "result": {
                    "thread_id": tid,
                    "summary": state["base"],
                    "turns_before": prior_turns,
                    "path": str(state_path(tid)),
                },
            }

        if method == "memories":
            # Client-writable per-thread notes the daemon injects as system msg.
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            if "content" in params:
                write_memories(tid, str(params.get("content") or ""))
            return {
                "id": request_id,
                "result": {
                    "thread_id": tid,
                    "memories": read_memories(tid),
                    "path": str(memories_path(tid)),
                },
            }

        if method == "turn/steer":
            # Redirect a thread mid-flight, WITHOUT destroying the work in
            # progress.
            #
            # This used to kill the running turn and start a new one from the
            # thread's last *recorded* state, then await that new turn before
            # replying. Three consequences, all of them the bug: the work
            # already done was thrown away, the agent restarted from a stale
            # point, and -- because the reply did not arrive until the new turn
            # finished -- the client's send button stayed disabled for the whole
            # of it, so steering was unreachable in practice.
            #
            # Now: a running turn picks the message up at its next round
            # boundary and carries on. If nothing is running, this is an ordinary
            # first message and starts a turn as before. Either way the reply is
            # immediate.
            tid = str(params.get("thread_id") or "").strip()
            message = str(params.get("message") or "").strip()
            if not tid or not message:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "thread_id and message required"},
                }
            start = params.get("start", True)
            if isinstance(start, str):
                start = start.lower() not in ("false", "0", "no")
            else:
                start = bool(start)
            prefix = f"{tid}:"
            hit = next(
                (key for key in RUNNING if key.startswith(prefix)), None,
            )
            running = hit is not None and not RUNNING[hit].done()
            if running and not start:
                # The common case: the agent is working and the user has typed
                # something. Hand it over and return. Nothing is cancelled.
                _pending_steer.setdefault(hit, []).append(message)
                audit.append({"type": "steer", "thread_id": tid,
                              "turn_id": hit, "cancelled": False,
                              "applied": "queued", "message": message[:500]})
                logger.info(f"steer queued for running turn {hit}")
                return {"id": request_id, "result": {
                    "thread_id": tid,
                    "turn_id": hit,
                    "turnId": hit,
                    "cancelled": False,
                    "applied": "queued",
                    "steered": True,
                }}
            cancelled = False
            if running:
                # Explicit replacement was asked for: stop the old turn first.
                task = RUNNING[hit]
                kill_turn(hit)
                approvals.drop_thread(tid)
                task.cancel()
                cancelled = True
                try:
                    await asyncio.wait_for(task, timeout=10)
                except (asyncio.CancelledError, asyncio.TimeoutError,
                        Exception):
                    pass
                _pending_steer.pop(hit, None)
            state, source = materialize_state(tid)
            if source == "new" and not cancelled:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            audit.append({"type": "steer", "thread_id": tid,
                          "cancelled": cancelled, "started": start,
                          "message": message[:500]})
            if not start:
                state["history"].append({"role": "user", "content": message})
                save_state(state)
                return {"id": request_id, "result": {
                    "thread_id": tid, "cancelled": cancelled,
                    "started": False,
                    "history": state["history"][-2:],
                }}
            passthrough = {
                k: params[k] for k in (
                    "provider", "model", "sandbox", "sandbox_root", "mode",
                    "api", "stream", "images", "skills",
                ) if k in params
            }
            return await handle_turn(
                {"id": request_id, "method": "turn",
                 "params": {"user": message, "thread_id": tid, **passthrough}},
                client_socket,
            )

        if method == "subagent/spawn":
            # Epic P: fan out a background turn on a child thread. Returns
            # immediately; poll subagent/status|result or watch broadcasts.
            parent = str(params.get("thread_id") or "").strip()
            message = str(params.get("message") or "").strip()
            if not parent or not message:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "thread_id and message required"},
                }
            running = [e for e in _subagents.values()
                       if e["status"] == "running"]
            if len(running) >= MAX_SUBAGENTS:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": (
                        f"too many running subagents ({MAX_SUBAGENTS})")},
                }
            child = _child_thread(parent)
            if params.get("context") == "fork":
                try:
                    fork_state(parent, child)
                except FileNotFoundError as e:
                    return {
                        "id": request_id,
                        "error": {"code": -32602, "message": str(e)},
                    }
            turn_params = {"user": message, "thread_id": child}
            for key in ("provider", "model", "sandbox", "sandbox_root",
                        "mode", "api", "stream", "images", "skills",
                        "max_rounds"):
                if key in params:
                    turn_params[key] = params[key]
            turn_params.setdefault("max_rounds", SUBAGENT_DEFAULT_ROUNDS)
            entry = {"id": child, "parent": parent, "thread_id": child,
                     "status": "running", "result": None, "task": None}
            _subagents[child] = entry
            entry["task"] = asyncio.create_task(_run_subagent(
                entry, {"id": request_id, "method": "turn",
                        "params": turn_params}))
            audit.append({"type": "subagent", "action": "spawn",
                          "thread_id": child, "parent": parent})
            return {"id": request_id, "result": {
                "id": child, "thread_id": child, "parent": parent,
                "status": "running"}}

        def _subagent_entry(sid: str):
            entry = _subagents.get(str(sid or ""))
            if entry is None:
                return None, {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown subagent: {sid}"},
                }
            return entry, None

        if method == "subagent/status":
            entry, err = _subagent_entry(params.get("id"))
            if err:
                return err
            return {"id": request_id, "result": {
                "id": entry["id"], "status": entry["status"],
                "thread_id": entry["thread_id"], "parent": entry["parent"]}}

        if method == "subagent/result":
            entry, err = _subagent_entry(params.get("id"))
            if err:
                return err
            return {"id": request_id, "result": {
                "id": entry["id"], "status": entry["status"],
                "thread_id": entry["thread_id"], "parent": entry["parent"],
                "result": entry["result"]}}

        if method == "subagent/list":
            parent = str(params.get("thread_id") or "")
            return {"id": request_id, "result": {"subagents": [
                {"id": e["id"], "status": e["status"],
                 "thread_id": e["thread_id"], "parent": e["parent"]}
                for e in _subagents.values()
                if not parent or e["parent"] == parent
            ]}}

        if method == "subagent/cancel":
            entry, err = _subagent_entry(params.get("id"))
            if err:
                return err
            if entry["status"] != "running":
                return {"id": request_id, "result": {
                    "id": entry["id"], "cancelled": False,
                    "status": entry["status"]}}
            child = entry["thread_id"]
            cancelled = False
            hit = next((k for k in RUNNING if k.startswith(f"{child}:")),
                       None)
            if hit is not None:
                task = RUNNING[hit]
                if not task.done():
                    kill_turn(hit)
                    approvals.drop_thread(child)
                    task.cancel()
                    cancelled = True
            if not cancelled:
                task = entry.get("task")
                if task is not None and not task.done():
                    task.cancel()
                    cancelled = True
            return {"id": request_id, "result": {
                "id": entry["id"], "cancelled": cancelled,
                "status": entry["status"]}}

        if method == "command/exec":
            # Out-of-band shell for the owning client (Kara move): quota-free,
            # model-free device commands (attachment pulls, provider model
            # probes, wake-lock tolerance). No turn, no approval, no quota —
            # the caller is trusted UI acting on user taps, same loopback
            # trust as the rest of this daemon. Every call IS audited, with
            # argv redacted past argv[0] (callers pass keys in argv).
            cmd = params.get("command", [])
            if (not isinstance(cmd, list) or not cmd
                    or not all(isinstance(x, str) for x in cmd)):
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "command must be a string array"},
                }
            home = os.environ.get("HOME") or str(Path.home())
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=home,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=60)
                    rc = proc.returncode
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    stdout, stderr, rc = b"", b"timeout after 60s", 124
            except FileNotFoundError:
                stdout, stderr, rc = (
                    b"", f"not on PATH: {cmd[0]}".encode(), 127)
            except Exception as e:
                stdout, stderr, rc = b"", str(e).encode(), 126
            out = stdout.decode(errors="replace")[:65536]
            err = stderr.decode(errors="replace")[:65536]
            try:
                audit.append({"type": "exec", "argv0": cmd[0],
                              "argc": len(cmd), "exit_code": rc})
            except Exception:
                pass
            return {"id": request_id, "result": {
                "exitCode": rc, "stdout": out, "stderr": err}}

        if method == "cancel":
            tid = params.get("turn_id") or params.get("id") or ""
            task = RUNNING.get(tid)
            if task is None and params.get("thread_id"):
                prefix = f"{params['thread_id']}:"
                hit = next(
                    (key for key in RUNNING if key.startswith(prefix)),
                    None,
                )
                if hit is not None:
                    tid, task = hit, RUNNING[hit]
            if task is None:
                return {"id": request_id, "result": False}
            # Kill child processes BEFORE cancelling: the turn's finally
            # blocks untrack first, which would hide them from kill_turn.
            kill_turn(tid)
            task.cancel()
            return {"id": request_id, "result": True}

        return {
            "id": request_id,
            "error": {"code": -32601, "message": f"unsupported method: {method}"},
        }

    except Exception as e:
        logger.exception("Request handling failed")
        return {
            "id": payload.get("id"),
            "error": {"code": -32700, "message": str(e)},
        }


async def stream_turn(
    provider: BaseProvider | None,
    messages: list[dict],
    model: str,
    images: list[str] | None = None,
    api: str = "chat",
    stream: bool = True,
) -> AsyncIterator[dict]:
    logger.info(f"stream_turn called with provider={provider}, model={model}")
    if not provider:
        logger.info("No provider found, yielding error message")
        yield {"type": "text_delta", "content": f"No provider found: {model}"}
        yield {"type": "complete"}
        return

    try:
        async for delta in provider.stream_turn(
            messages, model=model, images=images, api=api, stream=stream
        ):
            yield delta
    except Exception as e:
        yield {"type": "error", "message": str(e)}
        yield {"type": "complete"}


async def _send_delta(delta: dict, turn_id: str = "",
                      thread_id: str = "", round_no: int = 0) -> None:
    logger.info(f"_send_delta called with delta={delta}")
    content = delta.get("content", delta.get("message", ""))
    logger.info(f"Broadcasting: type={delta.get('type')}, content={content}")
    payload = {"type": delta.get("type"), "content": content}
    if turn_id:
        payload["turn_id"] = turn_id
    if thread_id:
        payload["thread_id"] = thread_id
    # Which pass of the agentic loop produced this text. Round 1 narrates
    # ("let me look at X"), the last round answers. A client cannot tell those
    # apart from the text alone, and guessing wrong either buries the answer
    # in a log or floods the chat with narration -- so the daemon, which knows
    # whether it will loop again, says so. See `_round_item_key` in Kara's
    # iagent_client.dart, which keys one message item per round.
    payload["round"] = round_no
    await broadcast(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Interlux agent daemon")
    parser.add_argument("-p", "--port", type=int, default=4600)
    args = parser.parse_args()

    logger.info(f"Starting iagent on port {args.port}")

    async def _run() -> None:
        try:
            # Epic E: spawn configured MCP servers before accepting requests.
            started = await mcp_mod.start_all()
            if started:
                logger.info(f"MCP tools ready: {started}")
            on_disconnect.append(drop_client_waits)
            transport = Transport(handle_request, args.port)
            await transport.start()
        finally:
            try:
                await mcp_mod.stop_all()
            except Exception:
                logger.exception("mcp shutdown failed")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
