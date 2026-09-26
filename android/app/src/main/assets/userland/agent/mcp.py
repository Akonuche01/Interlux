"""Epic E: MCP client — spawn stdio MCP servers, proxy their tools.

Epic Q adds remote servers (streamable HTTP) beside stdio:

Config (wipe-proof): ~/.interlux/agent/mcp.json
  {"servers": {
     "local": {"command": ["python3", "-u", "server.py"], "env": {...}},
     "hosted": {"type": "http", "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer ..."}, "timeout": 30}}}

Transport: newline-delimited JSON-RPC 2.0 over stdio, or streamable HTTP
(single POST per message; JSON or SSE replies; sticky Mcp-Session-Id).
Handshake: initialize -> notifications/initialized -> tools/list.
Registered MCP tools become `mcp_<server>__<tool>` in the normal registry:
same approval path (EXTRA_WRITE: always asks, session-grantable via Epic B),
same audit trail, same sandbox accounting. Failures are logged and surfaced
through the `mcp` RPC — never silent.

Boundary: header auth only. Full OAuth (browser dance, refresh rotation)
needs app-side secure storage and is explicitly out of daemon scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from .transport import broadcast

logger = logging.getLogger("mcp")

CONFIG_DIR = Path(
    os.environ.get("INTERLUX_AGENT_CONFIG", "~/.interlux/agent")
).expanduser()
CONFIG_FILE = CONFIG_DIR / "mcp.json"

PROTOCOL_VERSION = "2025-06-18"
HANDSHAKE_TIMEOUT = 15.0
CALL_TIMEOUT = 30.0


def config_path() -> Path:
    return CONFIG_FILE

def load_config() -> dict:
    try:
        if not CONFIG_FILE.exists():
            return {}
        cfg = json.loads(CONFIG_FILE.read_text())
        if not isinstance(cfg, dict):
            return {}
        servers = cfg.get("servers")
        return {"servers": servers} if isinstance(servers, dict) else {}
    except Exception as e:
        logger.error(f"mcp.json unreadable: {e}")
        return {}


def normalize_call_result(res: dict, server_name: str, tool: str) -> dict:
    """MCP tools/call result -> daemon tool result (shared stdio + HTTP)."""
    res = res if isinstance(res, dict) else {}
    parts = []
    for item in res.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    text = "\n".join(parts) if parts else json.dumps(res, ensure_ascii=False)
    if res.get("isError"):
        return {
            "status": "error",
            "server": server_name,
            "tool": tool,
            "message": text[:4000],
        }
    return {
        "status": "success",
        "server": server_name,
        "tool": tool,
        "output": text[:20000],
    }


class McpServer:
    """One stdio MCP server: handshake, request routing, tool proxying."""
    def __init__(self, name: str, command: list, env: dict | None = None,
                 cwd: str | None = None):
        self.name = name
        self.command = [str(c) for c in command]
        self.env = dict(env or {})
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 1
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.stderr_tail: list[str] = []
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self.error: str = ""
        self._closing = False

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if self.proc is None or self.proc.returncode is not None:
            return "dead"
        return "running"

    async def start(self) -> None:
        """Spawn + handshake + tools/list. Raises on failure (logged by caller)."""
        env = {**os.environ, **self.env}
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.cwd,
        )
        self._closing = False
        self.reader_task = asyncio.create_task(self._read_loop())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        init = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "iagent", "version": "1.0"},
            },
            timeout=HANDSHAKE_TIMEOUT,
        )
        self.server_info = init.get("serverInfo", {}) if isinstance(init, dict) else {}
        await self.notify("notifications/initialized")
        listed = await self.request("tools/list", {}, timeout=HANDSHAKE_TIMEOUT)
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        logger.info(
            f"mcp[{self.name}] up: {self.server_info.get('name', '?')} "
            f"tools={len(self.tools)}"
        )

    async def request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT):
        if self.proc is None or self.proc.returncode is not None:
            raise RuntimeError(f"mcp[{self.name}] not running")
        rid = self.next_id
        self.next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
        except Exception as e:
            logger.error(f"mcp[{self.name}] notify failed: {e}")

    async def _reply(self, rid, result=None, error=None) -> None:
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        try:
            if self.proc and self.proc.stdin:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode())
                await self.proc.stdin.drain()
        except Exception:
            pass

    async def _drain_stderr(self) -> None:
        """Keep stderr flowing (PIPE would block a chatty server) + keep a tail."""
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                self.stderr_tail.append(line.decode(errors="replace")[:300])
                self.stderr_tail = self.stderr_tail[-50:]
        except Exception:
            pass

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Non-JSON chatter on stdout: log, never crash the loop.
                    logger.debug(f"mcp[{self.name}] stdout: {line[:200]!r}")
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self.pending.get(msg.get("id"))
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(
                                    f"mcp[{self.name}] error: {msg['error']}"
                                )
                            )
                        else:
                            fut.set_result(msg.get("result"))
                elif "id" in msg and "method" in msg:
                    # Server-initiated request: answer ping, refuse the rest.
                    if msg["method"] == "ping":
                        await self._reply(msg["id"], {})
                    else:
                        await self._reply(msg["id"], error={
                            "code": -32601,
                            "message": f"method not supported: {msg['method']}",
                        })
                # notifications (and anything else) are ignored
        except Exception as e:
            logger.error(f"mcp[{self.name}] read loop: {e}")
        finally:
            # Fail every in-flight request loudly.
            for fut in list(self.pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(f"mcp[{self.name}] disconnected"))
            self.pending.clear()
            if not self._closing:
                self.error = "server exited"
                logger.error(f"mcp[{self.name}] server exited")

    async def list_tools(self) -> list[dict]:
        if self.status != "running":
            return []
        listed = await self.request("tools/list", {})
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return self.tools

    async def call_tool(self, tool: str, arguments: dict) -> dict:
        """MCP tools/call -> normalized daemon tool result."""
        res = await self.request(
            "tools/call", {"name": tool, "arguments": arguments or {}}
        )
        return normalize_call_result(res, self.name, tool)

    async def stop(self) -> None:
        self._closing = True
        if self.proc is None:
            return
        try:
            if self.proc.returncode is None:
                self.proc.stdin.close()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await self.proc.wait()
        except Exception as e:
            logger.error(f"mcp[{self.name}] stop: {e}")
        finally:
            self.proc = None
            for task in (self.reader_task, self.stderr_task):
                if task:
                    task.cancel()
            self.reader_task = None
            self.stderr_task = None


class RemoteMcpServer:
    """One streamable-HTTP MCP server (Epic Q).

    One POST per JSON-RPC message; replies are a single JSON document or an
    SSE stream. A server-sent Mcp-Session-Id sticks to later requests.
    Interface mirrors McpServer so registry, proxy, describe() and the
    started/stopped broadcasts treat both transports alike.
    """

    def __init__(self, name: str, url: str, headers: dict | None = None,
                 timeout: float = 30.0):
        self.name = name
        self.url = str(url)
        self.headers = {str(k): str(v)
                        for k, v in dict(headers or {}).items()}
        try:
            self.timeout = max(1.0, min(float(timeout or 30), 300))
        except (TypeError, ValueError):
            self.timeout = 30.0
        self.session_id: str | None = None
        self.next_id = 1
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self.error: str = ""
        self.started = False
        self.closed = False

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if not self.started or self.closed:
            return "dead"
        return "running"

    def _post_sync(self, payload: dict | None) -> tuple[int, str, bytes]:
        data = json.dumps(payload).encode() if payload is not None else b""
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=data or None,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid and not self.session_id:
                    self.session_id = sid
                return (r.status, r.headers.get("Content-Type", ""), r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode(errors="replace")
            raise RuntimeError(f"mcp[{self.name}] HTTP {e.code}: {body}")

    @staticmethod
    def _parse_messages(status: int, ctype: str, body: bytes) -> list[dict]:
        if status == 202 or not body:
            return []
        out: list[dict] = []
        if "text/event-stream" in (ctype or ""):
            for line in body.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data in ("", "[DONE]"):
                    continue
                try:
                    obj = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
        else:
            try:
                obj = json.loads(body.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                out.append(obj)
        return out

    async def _roundtrip(self, payload: dict | None) -> list[dict]:
        if self.closed:
            raise RuntimeError(f"mcp[{self.name}] not running")
        try:
            status, ctype, body = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, self._post_sync, payload),
                timeout=self.timeout + 5,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"mcp[{self.name}] request timed out")
        return self._parse_messages(status, ctype, body)

    async def _reply(self, rid, result=None, error=None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        try:
            await self._roundtrip(msg)
        except Exception:
            pass

    async def start(self) -> None:
        """Handshake over HTTP. Raises on failure (logged by caller)."""
        init = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "iagent", "version": "1.0"},
            },
            timeout=HANDSHAKE_TIMEOUT,
        )
        self.server_info = init.get("serverInfo", {}) if isinstance(init, dict) else {}
        await self.notify("notifications/initialized")
        listed = await self.request("tools/list", {}, timeout=HANDSHAKE_TIMEOUT)
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        self.started = True
        logger.info(
            f"mcp[{self.name}] up (http): {self.server_info.get('name', '?')} "
            f"tools={len(self.tools)}"
        )

    async def request(self, method: str, params: dict,
                      timeout: float = CALL_TIMEOUT):
        # Note: no started-check here — the handshake itself runs requests.
        if self.closed:
            raise RuntimeError(f"mcp[{self.name}] not running")
        rid = self.next_id
        self.next_id += 1
        try:
            narrow = max(1.0, min(float(timeout or 30), 300))
        except (TypeError, ValueError):
            narrow = 30.0
        old_timeout, self.timeout = self.timeout, narrow
        try:
            messages = await self._roundtrip(
                {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": params})
        finally:
            self.timeout = old_timeout
        for msg in messages:
            mid = msg.get("id")
            if "method" in msg and mid is not None and mid != rid:
                if msg["method"] == "ping":
                    await self._reply(mid, {})
                else:
                    await self._reply(mid, error={
                        "code": -32601,
                        "message": f"method not supported: {msg['method']}",
                    })
            elif mid == rid:
                if "error" in msg:
                    raise RuntimeError(
                        f"mcp[{self.name}] error: {msg['error']}")
                return msg.get("result")
        raise RuntimeError(f"mcp[{self.name}] no response to {method}")

    async def notify(self, method: str, params: dict | None = None) -> None:
        try:
            await self._roundtrip(
                {"jsonrpc": "2.0", "method": method, "params": params or {}})
        except Exception as e:
            logger.error(f"mcp[{self.name}] notify failed: {e}")

    async def list_tools(self) -> list[dict]:
        if self.status != "running":
            return []
        listed = await self.request("tools/list", {})
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return self.tools

    async def call_tool(self, tool: str, arguments: dict) -> dict:
        """MCP tools/call -> normalized daemon tool result."""
        res = await self.request(
            "tools/call", {"name": tool, "arguments": arguments or {}}
        )
        return normalize_call_result(res, self.name, tool)

    async def stop(self) -> None:
        self.closed = True


# name -> server; tool key -> (server_name, mcp_tool_name)
_servers: dict[str, McpServer] = {}
_tool_index: dict[str, tuple[str, str]] = {}
_registration_hook = None


def set_registration_hook(hook) -> None:
    """serve.py injects fn(add_keys, remove_keys) -> registered tool names."""
    global _registration_hook
    _registration_hook = hook


def _tool_key(server: str, tool: str) -> str:
    safe_s = "".join(c if c.isalnum() or c in "-_" else "_" for c in server)
    safe_t = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool)
    return f"mcp_{safe_s}__{safe_t}"


async def start_all() -> list[str]:
    """Start/refresh servers from config. Returns newly registered tools."""
    cfg = load_config()
    wanted = cfg.get("servers", {})
    registered: list[str] = []

    # Stop servers removed from config (and unregister their proxy tools).
    removed_keys: list[str] = []
    for name in list(_servers):
        if name not in wanted:
            await _servers[name].stop()
            for key, (sn, _t) in list(_tool_index.items()):
                if sn == name:
                    _tool_index.pop(key, None)
                    removed_keys.append(key)
            del _servers[name]
            try:
                await broadcast({"type": "mcpServer/stopped", "server": name})
            except Exception:
                logger.exception(f"mcp[{name}] stopped broadcast failed")

    for name, spec in wanted.items():
        if not isinstance(spec, dict):
            logger.error(f"mcp[{name}] invalid config (need an object)")
            continue
        stype = str(spec.get("type", "stdio") or "stdio").lower()
        if stype not in ("stdio", "http"):
            logger.error(f"mcp[{name}] unknown type {stype!r}")
            continue
        if stype == "http" and not spec.get("url"):
            logger.error(f"mcp[{name}] http server needs url")
            continue
        if stype == "stdio" and not spec.get("command"):
            logger.error(f"mcp[{name}] invalid config (need command[])")
            continue
        srv = _servers.get(name)
        if srv is not None and srv.status == "running":
            try:
                await srv.list_tools()  # hot-refresh tool list
            except Exception as e:
                logger.error(f"mcp[{name}] tools/list refresh failed: {e}")
                continue
        else:
            if srv is not None:
                try:
                    await srv.stop()
                except Exception:
                    pass
            if stype == "http":
                srv = RemoteMcpServer(
                    name,
                    spec["url"],
                    headers=spec.get("headers"),
                    timeout=spec.get("timeout", 30),
                )
            else:
                srv = McpServer(
                    name,
                    spec["command"],
                    env=spec.get("env"),
                    cwd=spec.get("cwd"),
                )
            _servers[name] = srv
            try:
                await srv.start()
            except Exception as e:
                tail = "; ".join(getattr(srv, "stderr_tail", [])[-3:])
                srv.error = (str(e)[:400] + (f" | stderr: {tail}" if tail else ""))[:500]
                logger.error(f"mcp[{name}] failed to start: {srv.error}")
                continue
            try:
                await broadcast({
                    "type": "mcpServer/started",
                    "server": name,
                    "tools": [t.get("name", "") for t in srv.tools],
                })
            except Exception:
                logger.exception(f"mcp[{name}] started broadcast failed")
        for t in srv.tools:
            key = _tool_key(name, t["name"])
            if key in _tool_index:
                continue
            _tool_index[key] = (name, t["name"])
            registered.append(key)

    if (registered or removed_keys) and _registration_hook is not None:
        try:
            _registration_hook(registered, removed_keys)
        except Exception:
            logger.exception("mcp tool registration hook failed")
    return registered


async def stop_all() -> None:
    for name, srv in list(_servers.items()):
        await srv.stop()
        try:
            await broadcast({"type": "mcpServer/stopped", "server": name})
        except Exception:
            logger.exception(f"mcp[{name}] stopped broadcast failed")
    _servers.clear()
    _tool_index.clear()


def is_mcp_tool(tool_name: str) -> bool:
    return tool_name in _tool_index


async def call(tool_key: str, arguments: dict) -> dict:
    """Registry proxy: run an MCP tool by its registered key."""
    entry = _tool_index.get(tool_key)
    if entry is None:
        return {"status": "error", "message": f"unknown mcp tool: {tool_key}"}
    server_name, mcp_tool = entry
    srv = _servers.get(server_name)
    if srv is None or srv.status != "running":
        return {
            "status": "error",
            "server": server_name,
            "tool": mcp_tool,
            "message": f"mcp server not running: {server_name}",
        }
    try:
        return await srv.call_tool(mcp_tool, arguments)
    except Exception as e:
        return {
            "status": "error",
            "server": server_name,
            "tool": mcp_tool,
            "message": str(e)[:2000],
        }


def describe() -> dict:
    """`mcp` RPC payload: status of every configured server."""
    cfg = load_config()
    configured = set(cfg.get("servers", {}))
    out = []
    for name in sorted(configured | set(_servers)):
        srv = _servers.get(name)
        if srv is None:
            out.append({"name": name, "status": "not started", "tools": [],
                        "error": ""})
        else:
            out.append({
                "name": name,
                "transport": ("http" if isinstance(srv, RemoteMcpServer)
                              else "stdio"),
                "status": srv.status,
                "tools": [t.get("name", "") for t in srv.tools],
                "server_info": srv.server_info,
                "error": srv.error,
            })
    return {"servers": out, "config": str(CONFIG_FILE),
            "registered_tools": sorted(_tool_index)}
