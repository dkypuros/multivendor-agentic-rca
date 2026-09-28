"""Generic MCP JSON-RPC core — one handle() shared by every layer/vendor server.

A server is just a TOOLS dict + a SERVER identity; this builds the JSON-RPC handler they share, so
mcp_http can serve any of them and the gateway can front all of them. Read-only tools here; the
O-Cloud server keeps its own guarded ACTION path in mcp_server.py.
"""
import json

PROTOCOL = "2024-11-05"


def _result(rid, payload):
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def make_handle(TOOLS, SERVER, unavailable_exc=Exception):
    """Return a handle(req) closure over this server's tool surface."""
    def handle(req):
        rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        if method == "initialize":
            return _result(rid, {"protocolVersion": PROTOCOL, "serverInfo": SERVER,
                                 "capabilities": {"tools": {}}})
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": [
                {"name": n, "description": d, "inputSchema": s}
                for n, (d, s, _) in TOOLS.items()]})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in TOOLS:
                return _error(rid, -32602, "unknown tool: %s" % name)
            try:
                payload = TOOLS[name][2](args)
            except unavailable_exc as exc:
                return _result(rid, {"content": [{"type": "text", "text": json.dumps(
                    {"error": "unavailable", "detail": str(exc)[:240]})}], "isError": True})
            except Exception as exc:                       # never crash the caller's loop
                return _result(rid, {"content": [{"type": "text", "text": json.dumps(
                    {"error": type(exc).__name__, "detail": str(exc)[:240]})}], "isError": True})
            return _result(rid, {"content": [
                {"type": "text", "text": json.dumps(payload, indent=2)}]})
        return _error(rid, -32601, "method not found: %s" % method)
    return handle
