from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import AsyncIterator

from .audit import AuditLog
from . import mcp as mcp_mod
from .pconfig import (
    PROVIDER_NAME_RE,
    config_section,
    env_pinned,
    make_provider,
    provider_config,
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
    get_skill as skills_get,
    list_skills as skills_list,
    refresh as skills_refresh,
    register_skill_tool,
    scan_skill_plugins,
    skill_messages,
    USER_DIR as SKILLS_USER_DIR,
)
from .threads import (
    build_messages,
    fold_output,
    fork_state,
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
    write_memories,
)
from .tools import EXTRA_WRITE, TOOLS, approval_label, needs_approval
from .tools.plugins import scan_plugins
from .tools.track import current_turn as turn_ctx
from .tools.track import kill_turn
from .transport import Transport, broadcast, send_to

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

    def approve(self, fid: str, decision: str, scope: str = "turn") -> bool:
        entry = self.pending.pop(fid, None)
        if entry is None:
            return False
        granted = str(decision or "").strip().lower() in (
            "approve", "accept", "allow", "yes", "always", "true",
        )
        entry["future"].set_result(granted)
        if granted and scope == "session" and entry.get("tool"):
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


approvals = ApprovalManager()

RUNNING: dict[str, asyncio.Task] = {}

# Epic P: subagents — background turns on child threads for parallel
# fan-out. Registry is in-memory (results persist in thread history);
# a daemon restart ends running subagents (documented, never silent:
# completed work is already in history + audit).
_subagents: dict[str, dict] = {}
_sub_counters: dict[str, int] = {}
MAX_SUBAGENTS = 8
SUBAGENT_DEFAULT_ROUNDS = 3


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


async def _call_client_tool(name: str, arguments: dict) -> dict:
    """Execute a client tool via its owner socket. Loud on every failure."""
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
            "id": fid, "method": "tool/call",
            "params": {"name": name, "arguments": arguments or {}},
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
    if isinstance(res, dict):
        return res
    return {"status": "success", "output": res}


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
                approval_params = {"command": label, "id": fid}

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
                result = await TOOLS[tool_name](**params)
                logger.info(f"Tool result: {result}")
                tool_output = {"tool": tool_name, "result": result}
                turn["output"].append(tool_output)
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
    # Epic J: bounded agentic loop. max_rounds=1 (default) is today's exact
    # behavior; higher lets the model see tool results and continue.
    try:
        max_rounds = int(params.get("max_rounds", 1) or 1)
    except (TypeError, ValueError):
        max_rounds = 1
    max_rounds = max(1, min(max_rounds, 10))

    working_content = user_msg
    while True:
        turn["rounds"] += 1
        rnd = turn["rounds"]
        round_messages = build_messages(state, thread_id, working_content,
                                          extra_system)
        has_error = False
        round_text_parts: list[str] = []

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
            await _send_delta(delta)

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

        # Fallback, round 1 only: tool call from a "run " user message.
        if rnd == 1 and not tool_calls and user_msg.strip().lower().startswith("run "):
            command = user_msg.strip()[4:].strip()
            turn["tool_calls"] = [{
                "tool": "shell",
                "parameters": {"command": command}
            }]

        if not turn["tool_calls"]:
            break  # pure answer — done

        mark = len(turn["output"])
        await run_tool_loop(turn, client_socket)

        if rnd >= max_rounds:
            logger.info(f"max_rounds ({max_rounds}) reached — stopping")
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
    tools_run = [
        o.get("tool") for o in turn["output"]
        if isinstance(o, dict) and o.get("tool")
    ]
    completed = {
        "type": "turn/completed",
        "turn_id": turn["id"],
        "thread_id": thread_id,
        "mode": turn.get("mode", "exec"),
        "tools": tools_run,
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
    provider_name = params.get("provider", "openai")
    model = params.get("model", "gpt-4o")

    thread_id = params.get("thread_id", f"thread-{len(audit.read_last())}")
    # Epic C: thread state = history + base + turn counter (wipe-proof home).
    state = load_state(thread_id) or new_state(thread_id)
    # Epic B: turn param wins; policy.json default otherwise.
    sandbox = resolve_sandbox(params, load_policy())
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
        # inside build_messages, rebuilt every Epic J round.
        return await _execute_turn(
            payload, params, user_msg, provider, model,
            thread_id, turn, client_socket, state,
            skill_messages(params.get("skills")),
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

        if method == "capabilities":
            return {
                "id": request_id,
                "result": {
                    "protocol": 1,
                    "methods": [
                        "turn", "cancel", "capabilities", "approve",
                        "tools_refresh", "tools", "tools/register",
                        "tools/unregister", "policy", "skills", "mcp",
                        "providers", "turn/steer",
                        "thread/resume", "thread/fork", "thread/compact",
                        "thread/list", "thread/read", "memories",
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
            # Get/set sandbox default + revoke standing grants.
            policy = load_policy()
            revoked = None
            changed = False
            if params.get("sandbox") in SANDBOX_MODES:
                policy["sandbox"] = params["sandbox"]
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
            return {"id": request_id, "result": {"providers": [
                public_section(n, s) for n, s in sorted(provider_config().items())
                if isinstance(s, dict)
            ]}}

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
            _client_tools[name] = {"description": description,
                                   "owner": client_socket}

            async def _client_proxy(_name: str = name, **kwargs):
                return await _call_client_tool(_name, kwargs)

            TOOLS[name] = _client_proxy
            EXTRA_WRITE.add(name)
            audit.append({"type": "client_tool", "action": "register",
                          "name": name})
            return {"id": request_id, "result": {
                "name": name, "description": description}}

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

        if method == "thread/resume":            # Open a thread: state file first, audit replay fallback,
            # brand-new empty thread otherwise (idempotent).
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            state, source = materialize_state(tid)
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
            # M4: redirect a thread mid-flight. Cancels the running turn
            # (if any), records the steer message, optionally starts a new
            # turn carrying it. Turn params may ride along for the new turn.
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
            cancelled = False
            prefix = f"{tid}:"
            hit = next(
                (key for key in RUNNING if key.startswith(prefix)), None,
            )
            if hit is not None:
                task = RUNNING[hit]
                if not task.done():
                    kill_turn(hit)
                    approvals.drop_thread(tid)
                    task.cancel()
                    cancelled = True
                    try:
                        await asyncio.wait_for(task, timeout=10)
                    except (asyncio.CancelledError, asyncio.TimeoutError,
                            Exception):
                        pass
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


async def _send_delta(delta: dict) -> None:
    logger.info(f"_send_delta called with delta={delta}")
    content = delta.get("content", delta.get("message", ""))
    logger.info(f"Broadcasting: type={delta.get('type')}, content={content}")
    await broadcast({"type": delta.get("type"), "content": content})


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
