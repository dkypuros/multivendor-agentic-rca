"""MCP Gateway — the governed ingress in front of the layer/vendor MCP servers.

One HTTP door for every agent. It authenticates a bearer token, resolves it to an INVOKER + a tool
SCOPE, AGGREGATES `tools/list` across the backend servers (filtered to the scope), and ROUTES each
`tools/call` to the backend that owns the tool — refusing any call outside scope (-32001). The
gateway holds no cluster credential and forwards no token downward; the tool list is the per-invoker
blast radius, enforced here.

  Auth   policy mode: Authorization: Bearer <token> -> invoker (static tokens in the policy file)
         capif  mode: decode a CAPIF access-token JWT and check iss=capif-core, exp, scope.
                NOTE: the lab CAPIF issues UNSIGNED tokens and this gateway does not verify a
                signature -- it demonstrates the authorization flow, not a security boundary.
  Route  tool name -> backend (policy.routes, fnmatch globs) -> POST <backend>/mcp
  Enforce  tools/list filtered to scope; tools/call denied (-32001) if out of scope
  Audit  one line per call: invoker, method, tool, backend, allow/deny

Run: python3 -m extensions.sheldon.agentic.gateway [--host 0.0.0.0] [--port 8800] [--policy PATH]
Stdlib + PyYAML; no MCP SDK.
"""
import argparse
import base64
import fnmatch
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

PROTOCOL = "2024-11-05"
GATEWAY = {"name": "sheldon-mcp-gateway", "version": "2.0.0"}
DEFAULT_POLICY = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "harness", "sheldon", "gateway-policy.yaml"))


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _jwt_payload(token):
    """Decode a JWT payload without verifying the signature (CAPIF's is a shaped stub)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg))
    except (ValueError, base64.binascii.Error):
        return None


class Policy:
    def __init__(self, doc):
        tv = doc.get("tokenVerification", {}) or {}
        self.mode = tv.get("mode", "policy")
        self.capif_issuer = tv.get("capifIssuer", "capif-core")
        self.backends = {b["name"]: b["url"] for b in doc.get("backends", [])}
        self.routes = doc.get("routes", [])
        self.invokers = {i["id"]: i for i in doc.get("invokers", [])}
        self.by_token = {i["token"]: i for i in doc.get("invokers", []) if i.get("token")}
        # capif mode: a CAPIF api-name (from the token's scope claim) -> tool globs
        self.capif_scope_map = doc.get("capifScopeMap", {})

    def invoker_for(self, token):
        if not token:
            return None
        if self.mode == "capif":                        # a CAPIF-issued, unexpired, scoped token
            claims = _jwt_payload(token)
            if not claims or claims.get("iss") != self.capif_issuer:
                return None
            if claims.get("exp", 0) < int(time.time()):
                return None
            # CAPIF scope is "3gpp#<aefId>:<apiName>"; the apiName maps to the tool surface it grants.
            api = (claims.get("scope", "") or "").split(":")[-1]
            globs = self.capif_scope_map.get(api)
            if not globs:                               # unknown/absent api scope -> no access
                return None
            return {"id": claims.get("sub", "capif-invoker"), "scope": globs, "capifApi": api}
        return self.by_token.get(token)                 # policy mode: static lab token

    def backend_for(self, tool):
        for r in self.routes:
            if fnmatch.fnmatch(tool, r["match"]):
                return self.backends.get(r["backend"])
        return None

    @staticmethod
    def in_scope(invoker, tool):
        return any(fnmatch.fnmatch(tool, pat) for pat in invoker.get("scope", []))


def _forward(url, req, timeout=30):
    data = json.dumps(req).encode()
    r = urllib.request.Request(url.rstrip("/") + "/mcp", data=data,
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "sheldon-mcp-gateway/2.0"
    policy = None

    def _send(self, code, obj):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _audit(self, inv, method, tool, backend, verdict):
        print("[gateway] invoker=%s method=%s tool=%s backend=%s -> %s"
              % (inv.get("id") if inv else "-", method, tool or "-", backend or "-", verdict), flush=True)

    def do_GET(self):
        if self.path.rstrip("/") == "/healthz":
            return self._send(200, {"status": "ok", "gateway": GATEWAY, "mode": self.policy.mode,
                                    "backends": list(self.policy.backends)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        invoker = self.policy.invoker_for(token)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._send(400, _err(None, -32700, "parse error"))
        rid, method = req.get("id"), req.get("method")
        if invoker is None:
            self._audit(None, method, None, None, "401")
            return self._send(401, _err(rid, -32000,
                "unauthorized: present a valid %s bearer token" % self.policy.mode))

        if method == "initialize":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL, "serverInfo": GATEWAY, "capabilities": {"tools": {}}}})
        if method in ("notifications/initialized", "initialized"):
            return self._send(202, None)
        if method == "ping":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {}})

        # tools/list — aggregate across every backend, filter to the invoker's scope
        if method == "tools/list":
            merged, seen = [], set()
            for name, url in self.policy.backends.items():
                try:
                    resp = _forward(url, {"jsonrpc": "2.0", "id": rid, "method": "tools/list"})
                except (urllib.error.URLError, OSError):
                    continue                             # a down backend just contributes nothing
                for t in (resp.get("result") or {}).get("tools", []):
                    tn = t.get("name", "")
                    if tn not in seen and Policy.in_scope(invoker, tn):
                        merged.append(t); seen.add(tn)
            self._audit(invoker, method, None, "*", "%d tools in scope" % len(merged))
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": merged}})

        # tools/call — scope check, then route to the owning backend
        if method == "tools/call":
            tool = (req.get("params") or {}).get("name")
            if not Policy.in_scope(invoker, tool or ""):
                self._audit(invoker, method, tool, None, "DENIED out-of-scope")
                return self._send(403, _err(rid, -32001,
                    "tool '%s' is not in invoker '%s' scope" % (tool, invoker.get("id"))))
            backend = self.policy.backend_for(tool or "")
            if not backend:
                return self._send(502, _err(rid, -32002, "no backend routes tool '%s'" % tool))
            try:
                resp = _forward(backend, req)
            except (urllib.error.URLError, OSError) as exc:
                return self._send(502, _err(rid, -32003, "backend unreachable: %s" % exc))
            self._audit(invoker, method, tool, backend, "forwarded")
            return self._send(200, resp)

        return self._send(200, _err(rid, -32601, "method not found: %s" % method))

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="MCP gateway (governed multi-backend ingress)")
    ap.add_argument("--host", default=os.environ.get("GATEWAY_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT", "8800")))
    ap.add_argument("--policy", default=os.environ.get("GATEWAY_POLICY", DEFAULT_POLICY))
    args = ap.parse_args()
    with open(args.policy) as fh:
        _Handler.policy = Policy(yaml.safe_load(fh))
    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print("mcp-gateway: %s on http://%s:%d/mcp  mode=%s  backends=%s"
          % (GATEWAY["name"], args.host, args.port, _Handler.policy.mode,
             list(_Handler.policy.backends)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
