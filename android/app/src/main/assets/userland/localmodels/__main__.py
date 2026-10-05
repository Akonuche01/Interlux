"""CLI for the standalone local-model manager.

Usage (from the userland, or ``python -m localmodels`` from the repo):

    python -m localmodels list              # models + ports + running state
    python -m localmodels start <id>        # start a model (detached)
    python -m localmodels stop  <id>        # stop a model
    python -m localmodels autostart         # bring up the autostart list
    python -m localmodels registry          # rewrite local_models.json
    python -m localmodels paths             # show resolved paths
    python -m localmodels serve [port]      # run the connectable gateway (default 4602)

No daemon involved: this only manages llama-server processes and writes
the registry file other agents read.
"""

from __future__ import annotations

import json
import sys

from . import manager as m


def _cmd_list() -> int:
    models = m.list_models()
    if not models:
        print(f"no models in {m.MODELS_DIR}")
        return 0
    for x in models:
        state = "running" if x["running"] else "stopped"
        print(f"{x['id']:<32} {state:<8} {x['base_url']}")
    return 0


def _cmd_start(mid: str) -> int:
    print(json.dumps(m.ensure(mid), indent=1))
    return 0


def _cmd_stop(mid: str) -> int:
    print(json.dumps(m.stop(mid), indent=1))
    return 0


def _cmd_autostart() -> int:
    print(json.dumps({"started": m.autostart()}, indent=1))
    return 0


def _cmd_registry() -> int:
    m.write_registry()
    print(m.REGISTRY_FILE.read_text())
    return 0


def _cmd_paths() -> int:
    print(json.dumps({
        "userland": str(m.userland_dir()),
        "home": str(m.home_dir()),
        "models_dir": str(m.MODELS_DIR),
        "binary": str(m.BIN),
        "registry": str(m.REGISTRY_FILE),
        "autostart": str(m.AUTOSTART_FILE),
    }, indent=1))
    return 0


def _cmd_serve(rest: list[str]) -> int:
    from . import gateway
    port = gateway.GATEWAY_PORT
    if rest:
        try:
            port = int(rest[0])
        except ValueError:
            pass
    print(f"localmodels gateway on http://127.0.0.1:{port}/v1", flush=True)
    gateway.serve(port)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "list"
    rest = args[1:]
    if cmd == "list":
        return _cmd_list()
    if cmd == "start" and rest:
        return _cmd_start(rest[0])
    if cmd == "stop" and rest:
        return _cmd_stop(rest[0])
    if cmd == "autostart":
        return _cmd_autostart()
    if cmd == "registry":
        return _cmd_registry()
    if cmd == "paths":
        return _cmd_paths()
    if cmd == "serve":
        return _cmd_serve(rest)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
