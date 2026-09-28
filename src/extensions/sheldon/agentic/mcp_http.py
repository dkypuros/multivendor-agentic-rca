"""MCP over HTTP — serve any of the layer/vendor MCP servers as a listening service.

  --server ocloud  (default)  extensions.sheldon.agentic.mcp_server   — Metal3/OCM/O2-IMS/Redfish
  --server ran                extensions.sheldon.agentic.mcp_ran      — OAI gNB
  --server intel              extensions.sheldon.agentic.mcp_intel    — NIC HW-timestamp (emulated)
  --server redhat             extensions.sheldon.agentic.mcp_redhat   — linuxptp / cloud-event-proxy

  POST /mcp     one JSON-RPC request (or batch) -> the JSON-RPC response(s)
  GET  /healthz liveness (server name + tool count)

Run: python3 -m extensions.sheldon.agentic.mcp_http [--server NAME] [--host 0.0.0.0] [--port 8850]
Stdlib only (http.server). The chosen module supplies handle(), SERVER and TOOLS.
"""
import argparse
import importlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVERS = {
    "ocloud": "extensions.sheldon.agentic.mcp_server",
    "ran":    "extensions.sheldon.agentic.mcp_ran",
    "intel":  "extensions.sheldon.agentic.mcp_intel",
    "redhat": "extensions.sheldon.agentic.mcp_redhat",
}
MOD = None                                              # the selected server module


class _Handler(BaseHTTPRequestHandler):
    server_version = "sheldon-mcp-http/1.0"

    def _send(self, code, obj):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/healthz":
            return self._send(200, {"status": "ok", "server": MOD.SERVER, "tools": len(MOD.TOOLS)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "parse error"}})
        if isinstance(req, list):
            return self._send(200, [r for r in (MOD.handle(x) for x in req) if r is not None])
        resp = MOD.handle(req)
        return self._send(202, None) if resp is None else self._send(200, resp)

    def log_message(self, *args):
        pass


def main():
    global MOD
    ap = argparse.ArgumentParser(description="MCP-over-HTTP server")
    ap.add_argument("--server", default=os.environ.get("MCP_SERVER", "ocloud"), choices=list(SERVERS))
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8850")))
    args = ap.parse_args()
    MOD = importlib.import_module(SERVERS[args.server])
    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print("mcp-http: %s (%s) on http://%s:%d/mcp  (%d tools)"
          % (MOD.SERVER["name"], args.server, args.host, args.port, len(MOD.TOOLS)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
