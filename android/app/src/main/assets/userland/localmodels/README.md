# localmodels — standalone local-model serving

A **self-contained** local-model subsystem for Interlux. It is deliberately
separate from the agent daemon: nothing here imports the daemon, and the
daemon does not import it. Providers, policy, and the daemon's turn path are
untouched.

## What it does

1. **Discovers** every `*.gguf` under the models directory (re-scanned on
   every call — nothing is hardcoded).
2. **Serves** each model with its own `llama-server` on a stable loopback
   port, spawned **detached** (own session, reparented to init) so it
   survives the manager, the app, and the daemon being killed.
3. **Publishes** a registry file that **any** agent can read, plus a plain
   OpenAI-compatible endpoint per model.

## Layout (resolved at runtime)

| Path | Meaning |
| --- | --- |
| `<userland>/localmodels/` | this package (device); `<repo>/localmodels/` on the host |
| `<home>/.models/*.gguf` | the models the user drops in |
| `<userland>/bin/llama-server` | the served binary (Android ARM64) |
| `<home>/.interlux/localmodels/local_models.json` | the registry other agents read |
| `<home>/.interlux/localmodels/autostart.json` | models to bring up on boot |

Override with `INTERLUX_USERLAND` and/or `HOME`.

## Use it

```sh
python -m localmodels list          # models, ports, running state
python -m localmodels start <id>    # start a model (detached)
python -m localmodels stop  <id>    # stop a model
python -m localmodels autostart     # bring up the autostart list
python -m localmodels registry      # rewrite local_models.json
python -m localmodels paths         # show resolved paths
python -m localmodels serve [port]  # run the connectable gateway (default 4699)
```

## Connect to it — one endpoint for everyone

`python -m localmodels serve` runs a **gateway** on `http://127.0.0.1:4602/v1`.

**4602 is deliberate.** It is the daemon's on-device / `"local"` canonical base
(`agent/providers/on_device.py`, `CANONICAL_BASE`). So the daemon's *existing*
local provider already points here — it connects with **zero code changes**,
no new provider type, nothing in the daemon touched. Agents that aren't the
daemon connect to the same base URL directly.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/models` | every model, as OpenAI model objects |
| `POST /v1/chat/completions` | routed to the model named in the request (auto-started if stopped) |
| `GET /localmodels` | the discovery registry |
| `GET /health` | liveness + model count |

Per-model ports live at 4603..4697; the gateway owns 4602 so they never
collide.

## How any agent consumes it directly

Read `local_models.json`:

```json
{ "models": [
  { "id": "qwen2.5-0.5b-q4", "name": "qwen2.5-0.5b-q4.gguf",
    "base_url": "http://127.0.0.1:4617/v1", "running": true }
] }
```

…then speak plain OpenAI to that `base_url`:

```sh
curl http://127.0.0.1:4617/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

Kara, or any other agent the user chooses, needs only that file — no daemon
protocol, no shared code.
