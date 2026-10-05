"""Local-model registry and per-model llama-server management.

The daemon OWNS on-device inference. Every GGUF the user drops into the
models directory becomes a local model automatically -- nothing is
hardcoded. Each model gets a stable loopback port and its own
llama-server process, spawned *detached* (its own session, reparented
to init) so it survives the daemon -- and the app -- being killed.

Every server is a plain OpenAI-compatible endpoint
(http://127.0.0.1:<port>/v1) that ANY agent can speak, and a registry
file (local_models.json) lets non-Interlux agents discover them without
knowing the daemon protocol at all.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from .home import engine_config_dir, engine_home, engine_userland_dir

MODELS_DIR = engine_home() / ".models"
BIN = engine_userland_dir() / "bin" / "llama-server"
PORT_BASE = 4602
PORT_POOL = 96  # 4602..4697, collision-checked
AUTOSTART_FILE = engine_config_dir() / "local_autostart.json"
REGISTRY_FILE = engine_config_dir() / "local_models.json"

# id -> {"proc": Popen|None, "port": int, "path": str}
_running: dict[str, dict] = {}


def _port_for(path: Path) -> int:
    """Stable port for a model path (hash-based, collision-checked)."""
    h = hash(str(path.resolve())) & 0xFFFFFFFF
    return PORT_BASE + (h % PORT_POOL)


def _find(model_id: str) -> dict | None:
    for m in discover():
        if m["id"] == model_id:
            return m
    return None


def discover() -> list[dict]:
    """Every GGUF in the models dir. Re-scanned on every call (dynamic)."""
    out: list[dict] = []
    if MODELS_DIR.is_dir():
        for p in sorted(MODELS_DIR.glob("*.gguf")):
            try:
                size = round(p.stat().st_size / 1_048_576, 1)
            except OSError:
                size = 0.0
            out.append({
                "id": p.stem,
                "name": p.name,
                "path": str(p),
                "size_mb": size,
            })
    return out


def is_up(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1"


def _pid_for_port(port: int) -> int | None:
    """Best-effort: find a llama-server bound to --port <port> via /proc."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    want = f"--port {port}"
    for d in proc.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        try:
            cmd = Path(d, "cmdline").read_bytes()
            cmd = cmd.replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "llama-server" in cmd and want in cmd:
            return int(d.name)
    return None


def _proc_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        if not _proc_alive(pid):
            return
        time.sleep(0.5)


def _autostart_load() -> list[str]:
    try:
        if AUTOSTART_FILE.exists():
            data = json.loads(AUTOSTART_FILE.read_text())
            if isinstance(data, list):
                return [str(x) for x in data]
    except (OSError, ValueError):
        pass
    return []


def _autostart_save(ids: list[str]) -> None:
    try:
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(json.dumps(sorted(set(ids)), indent=1))
    except OSError:
        pass


def add_autostart(model_id: str) -> None:
    ids = _autostart_load()
    if model_id not in ids:
        ids.append(model_id)
        _autostart_save(ids)


def remove_autostart(model_id: str) -> None:
    ids = [i for i in _autostart_load() if i != model_id]
    _autostart_save(ids)


def ensure(model_id: str, block: bool = True) -> dict:
    """Start the model's llama-server if not already up.

    The process is spawned detached (new session) so it outlives the
    daemon and the app. Starting a model opts it into the autostart list,
    so it comes back on the next daemon boot / app restart.
    """
    target = _find(model_id)
    if target is None:
        return {"ok": False, "error": f"no such local model: {model_id}"}
    port = _port_for(Path(target["path"]))
    if is_up(port):
        _running[model_id] = {"proc": None, "port": port,
                              "path": target["path"]}
        add_autostart(model_id)
        write_registry()
        return {"ok": True, "running": True, "port": port}
    if not BIN.is_file() or not os.access(str(BIN), os.X_OK):
        return {"ok": False,
                "error": "llama-server binary missing at bin/llama-server "
                         "(bundle an Android ARM64 llama-server build into the userland bin/)"}
    userland = engine_userland_dir()
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{userland}/lib:{userland}"
    env["HOME"] = str(userland / "home")
    env["OPENSSL_CONF"] = "/dev/null"
    log = engine_config_dir() / f"llama-{model_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {
        "env": env,
        "cwd": str(userland / "home"),
        "stdout": str(log),
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True  # reparent to init
    try:
        proc = subprocess.Popen(
            [str(BIN), "-m", target["path"], "--host", "127.0.0.1",
             "--port", str(port), "-c", "2048"],
            **kwargs,
        )
    except OSError as e:
        return {"ok": False, "error": f"spawn failed: {e}"}
    _running[model_id] = {"proc": proc, "port": port, "path": target["path"]}
    add_autostart(model_id)
    if not block:
        write_registry()
        return {"ok": True, "running": False, "port": port, "spawning": True}
    for _ in range(48):
        if is_up(port):
            write_registry()
            return {"ok": True, "running": True, "port": port}
        time.sleep(2.5)
    write_registry()
    return {"ok": True, "running": is_up(port), "port": port,
            "spawning": not is_up(port)}


def stop(model_id: str) -> dict:
    """Stop a model's llama-server and drop it from the autostart list."""
    target = _find(model_id)
    if target is None:
        return {"ok": False, "error": f"no such local model: {model_id}"}
    port = _port_for(Path(target["path"]))
    entry = _running.get(model_id)
    proc = entry.get("proc") if isinstance(entry, dict) else None
    killed = False
    if proc is not None and proc.poll() is None:
        _kill(proc.pid)
        killed = True
    else:
        pid = _pid_for_port(port)
        if pid:
            _kill(pid)
            killed = True
    for _ in range(20):
        if not is_up(port):
            break
        time.sleep(0.5)
    _running.pop(model_id, None)
    remove_autostart(model_id)
    write_registry()
    return {"ok": True, "stopped": killed}


def autostart() -> list[str]:
    """Bring every autostart-listed model up (non-blocking spawn)."""
    started: list[str] = []
    for mid in _autostart_load():
        if _find(mid) is None:
            continue
        res = ensure(mid, block=False)
        if res.get("ok"):
            started.append(mid)
    return started


def list_models() -> list[dict]:
    out = []
    for m in discover():
        port = _port_for(Path(m["path"]))
        out.append({
            "id": m["id"],
            "name": m["name"],
            "size_mb": m["size_mb"],
            "port": port,
            "base_url": base_url(port),
            "running": is_up(port),
        })
    return out


def as_provider_block(model: dict) -> dict:
    port = _port_for(Path(model["path"]))
    return {
        "provider": model["id"],
        "base_url": base_url(port),
        "has_key": False,
        "key_hint": "",
        "local": True,
        "model": model["name"],
        "port": port,
        "running": is_up(port),
    }


def write_registry() -> None:
    """Discovery file for ANY agent (not just Interlux clients)."""
    try:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_FILE.write_text(json.dumps({
            "models": [
                {"id": m["id"], "name": m["name"],
                 "base_url": m["base_url"], "running": m["running"]}
                for m in list_models()
            ],
        }, indent=1))
    except OSError:
        pass
