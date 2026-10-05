"""One connectable endpoint in front of every local model.

The daemon, Kara, or any other agent points at ONE base URL --

    http://127.0.0.1:4602/v1

-- and reaches every local model, with no shared code and no daemon
involvement. 4602 is the daemon's on-device / "local" canonical base, so
the daemon's existing local provider connects here untouched. The gateway speaks plain OpenAI, so a client only needs a
base_url; it never imports this package.

Endpoints:

  GET  /v1/models              all discovered models (OpenAI model objects)
  POST /v1/chat/completions    routed to the model named in the request
  GET  /localmodels            the discovery registry (same as the file)
  GET  /health                 liveness + model count

On a request for a model that is not running yet, the gateway starts it
(detached) and waits for it, then proxies the call. Streaming responses
are passed through chunk by chunk.
"""

from __future__ import annotations

import http.client
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import manager as m

GATEWAY_PORT = 4602  # the daemon's on-device / "local" canonical base
_START_WAIT_S = 90


def _match(name: str) -> dict | None:
    """The model to serve: by id/name, else a running one, else the first."""
    models = m.list_models()
    if not models:
        return None
    if name:
        for x in models:
            if name in (x["id"], x["name"]):
                return x
    for x in models:
        if x["running"]:
            return x
    return models[0]


class _Handler(BaseHTTPRequestHandler):
    server_version = "localmodels-gateway/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the console quiet
        pass

    def _json(self, code: int, obj: object) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _registry(self) -> dict:
        return {"models": [
            {"id": x["id"], "name": x["name"],
             "base_url": x["base_url"], "running": x["running"]}
            for x in m.list_models()
        ]}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/v1/models", "/models"):
            data = [{"id": x["id"], "object": "model", "owned_by": "localmodels",
                     "running": x["running"], "base_url": x["base_url"]}
                    for x in m.list_models()]
            self._json(200, {"object": "list", "data": data})
            return
        if path in ("/localmodels", "/registry"):
            self._json(200, self._registry())
            return
        if path in ("/health", "/"):
            self._json(200, {"ok": True, "models": len(m.discover())})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions",
                        "/v1/completions", "/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return
        target = _match(str(body.get("model") or ""))
        if target is None:
            self._json(404, {"error": {"message": "no local models available"}})
            return
        port = target["port"]
        if not m.is_up(port):
            m.ensure(target["id"], block=False)
            deadline = time.time() + _START_WAIT_S
            while time.time() < deadline and not m.is_up(port):
                time.sleep(1)
        if not m.is_up(port):
            self._json(503, {"error": {
                "message": f"local model '{target['id']}' did not come up"}})
            return
        # Route by the served model name; llama-server ignores it but it keeps
        # the response's "model" field honest.
        body["model"] = target["name"]
        payload = json.dumps(body).encode()
        upstream_path = "/v1/" + path.split("/")[-1]  # chat/completions|completions
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
            conn.request("POST", upstream_path, payload,
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
        except OSError as e:
            self._json(502, {"error": {"message": f"local server unreachable: {e}"}})
            return
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            conn.close()


def make_server(port: int = GATEWAY_PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Build the gateway (does not block)."""
    return ThreadingHTTPServer((host, port), _Handler)


def serve(port: int = GATEWAY_PORT, host: str = "127.0.0.1") -> None:
    """Run the gateway until interrupted."""
    httpd = make_server(port, host)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
