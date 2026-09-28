"""
SCP: Service Communication Proxy — the indirect-communication hub of the owned SBA.

The SCP sits between an NF consumer and an NF producer and carries their SBI request for them:
the consumer hands the SCP a request addressed to a TARGET NF-TYPE (not a concrete instance),
the SCP does DELEGATED DISCOVERY against the NRF on the consumer's behalf, selects a producer
instance, forwards the request, and returns the producer's response verbatim. This is 3GPP
"indirect communication with delegated discovery" — Communication Model D (TS 23.501 6.2.19,
TS 29.500 6.10). It is a transparent SBI reverse proxy with NRF-based routing.

This is a STANDALONE proxy: no other NF is edited, and no NF is yet configured to route THROUGH
the SCP. The spec proves the proxy works in isolation (a consumer that DOES address the SCP gets
its request correctly discovered, forwarded, and answered). Wiring the core to prefer the SCP
(populate each NF's 3gpp-Sbi-Target-apiRoot with the SCP, Model C/D everywhere) is a follow-up
wave — see procedures/scp_indirect_sbi.txt ledger.

Communication models (TS 29.500 6.10.2), for orientation:
  Model A  direct, no NRF discovery              consumer -> producer
  Model B  direct, consumer discovers via NRF    consumer -> NRF; consumer -> producer
  Model C  indirect, consumer discovers          consumer -> NRF; consumer -> SCP -> producer
  Model D  indirect, SCP discovers (delegated)   consumer -> SCP -> {NRF; producer}   <-- THIS

How a consumer addresses the SCP (TS 29.500 5.2.3.2 SBI routing headers):
  the consumer sends its normal SBI request (method + resource path + body) to the SCP, and
  conveys the routing intent in headers rather than in the URL authority:
    3gpp-Sbi-Discovery-Target-Nf-Type   the producer NF-type to discover+select (Model D)
    3gpp-Sbi-Discovery-Service-Names    optional service-name filter for discovery
    3gpp-Sbi-Discovery-Requester-Nf-Type the consumer's own NF-type (defaults to SCP)
    3gpp-Sbi-Target-apiRoot             an already-selected producer apiRoot (Model C / reselection)
  The SCP discovers the target NF-type in the NRF, round-robin-selects an instance, and forwards
  the original method/path/query/body there.

Labeled simplifications (ledgered in procedures/scp_indirect_sbi.txt):
  - SBI is modeled as JSON-over-HTTP/1.1 at procedure fidelity (design stance 1), same as every
    owned NF; the SCP forwards method + path + query + JSON body + the correlation id. It does not
    replay the full inbound header set (only the minimal SBI-meaningful ones) — honest for a
    procedure-level proxy; a bit-level HTTP/2 proxy is a fidelity follow-up.
  - Instance selection is round-robin over REGISTERED candidates (TS 29.500 6.10.3 load
    balancing). Priority / capacity / load weighting and circuit-breaking are not modeled yet.
  - No live NF caching: the SCP discovers per request against the NRF (always fresh). A cached
    NF-set with change notifications (Nnrf) is a follow-up.

Run: python3 scp.py   (SBI proxy on 127.0.0.1:7019, registers with the NRF as SCP)
"""

import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import request
from domain import netconfig, obs

PORT = netconfig.port("scp")
NRF = netconfig.url("nrf")

# TS 29.500 5.2.3.2 SBI routing / delegated-discovery headers. HTTP header lookup is
# case-insensitive (http.server uses email.message), so the exact casing here is only cosmetic.
HDR_DISC_TARGET = "3gpp-Sbi-Discovery-Target-Nf-Type"
HDR_DISC_SERVICE = "3gpp-Sbi-Discovery-Service-Names"
HDR_DISC_REQUESTER = "3gpp-Sbi-Discovery-Requester-Nf-Type"
HDR_TARGET_APIROOT = "3gpp-Sbi-Target-apiRoot"

# Round-robin cursor per target NF-type (TS 29.500 6.10.3). In-process, like the reference SCP.
_rr_cursor = {}


def discover_instances(nf_type, requester, service_names=None):
    """Delegated discovery (Model D): the SCP calls Nnrf_NFDiscovery on the consumer's behalf
    (TS 29.510 5.3). Returns the list of matching REGISTERED nfInstances (possibly empty)."""
    q = f"{NRF}/nnrf-disc/v1/nf-instances?target-nf-type={nf_type}&requester-nf-type={requester}"
    if service_names:
        q += f"&service-names={service_names}"
    try:
        status, body = request("GET", q)
    except OSError:
        return []
    if status != 200:
        return []
    return [i for i in body.get("nfInstances", []) if i.get("nfStatus", "REGISTERED") == "REGISTERED"]


def select_instance(nf_type, instances):
    """Round-robin selection over the discovered candidates (TS 29.500 6.10.3 load balancing)."""
    cursor = _rr_cursor.get(nf_type, 0)
    chosen = instances[cursor % len(instances)]
    _rr_cursor[nf_type] = (cursor + 1) % len(instances)
    return chosen


def instance_api_root(instance):
    """Resolve a selected nfInstance to its SBI apiRoot base URL (scheme://host:port).
    Mirrors the discovery-response shape every owned NF registers (nfServices[].ipEndPoints[])."""
    services = instance.get("nfServices") or []
    for svc in services:
        for ep in svc.get("ipEndPoints") or []:
            if ep.get("port"):
                host = ep.get("ipv4Address") or (instance.get("ipv4Addresses") or ["127.0.0.1"])[0]
                return f"http://{host}:{ep['port']}"
    return None


def error_body(status, title, cause, detail):
    """ProblemDetails (TS 29.500 5.2.7) as raw bytes for the proxy path."""
    return json.dumps({"title": title, "status": status, "cause": cause, "detail": detail}).encode()


def proxy(method, parsed, headers, raw_body):
    """Indirect communication with delegated discovery (Model D, TS 29.500 6.10.2.2).

    Returns (status, content_type, body_bytes). Honest failures are ProblemDetails, never faked
    success: no target header -> 400; nothing discovered -> 404; producer unreachable -> 502."""
    target_type = (headers.get(HDR_DISC_TARGET) or "").strip().upper()
    api_root = (headers.get(HDR_TARGET_APIROOT) or "").strip()
    service_names = headers.get(HDR_DISC_SERVICE)
    requester = (headers.get(HDR_DISC_REQUESTER) or "SCP").strip().upper()

    if not target_type and not api_root:
        obs.counter("scp_proxy_errors_total", cause="NO_TARGET").inc()
        obs.log("proxy_rejected", level="warning", path=parsed.path,
                reason="no 3gpp-Sbi-Discovery-Target-Nf-Type / 3gpp-Sbi-Target-apiRoot")
        return 400, "application/problem+json", error_body(
            400, "Bad Request", "MANDATORY_IE_INCORRECT",
            "an SBI request through the SCP must carry a target: either the "
            "3gpp-Sbi-Discovery-Target-Nf-Type header (Model D) or 3gpp-Sbi-Target-apiRoot (Model C)")

    # Model D: delegated discovery + selection. Model C fallback: an explicit apiRoot is honoured
    # as-is when no discovery type is given.
    if target_type:
        instances = discover_instances(target_type, requester, service_names)
        if not instances:
            obs.counter("scp_proxy_errors_total", cause="NF_NOT_FOUND").inc()
            obs.log("proxy_no_target", level="warning", target=target_type, path=parsed.path)
            return 404, "application/problem+json", error_body(
                404, "Not Found", "TARGET_NF_NOT_FOUND",
                f"the SCP found no REGISTERED {target_type} in the NRF for delegated discovery")
        chosen = select_instance(target_type, instances)
        base = instance_api_root(chosen)
        if base is None:
            obs.counter("scp_proxy_errors_total", cause="NO_ENDPOINT").inc()
            return 502, "application/problem+json", error_body(
                502, "Bad Gateway", "TARGET_NF_UNREACHABLE",
                f"selected {target_type} instance advertised no SBI ipEndPoint")
        selected_id = chosen.get("nfInstanceId")
    else:
        base = api_root.rstrip("/")
        selected_id = None

    # Forward the original request verbatim (method + path + query + JSON body) to the producer.
    target_url = base + parsed.path
    if parsed.query:
        target_url += "?" + parsed.query
    body = json.loads(raw_body) if raw_body else None
    try:
        status, reply = request(method, target_url, body)
    except OSError as exc:
        obs.counter("scp_proxy_errors_total", cause="FORWARD_FAILED").inc()
        obs.log("proxy_forward_failed", level="error", target=target_type or api_root,
                url=target_url, error=str(exc))
        return 502, "application/problem+json", error_body(
            502, "Bad Gateway", "TARGET_NF_UNREACHABLE",
            f"the SCP could not reach the selected producer at {base}")

    obs.counter("scp_forwarded_requests_total", target=(target_type or "APIROOT")).inc()
    obs.log("proxy_forwarded", method=method, path=parsed.path, target=(target_type or api_root),
            selected=selected_id, base=base, status=status)
    return status, "application/json", json.dumps(reply).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        corr = obs.get_corr()
        if corr:
            self.send_header(obs.HEADER, corr)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        obs.set_corr(self.headers.get(obs.HEADER))

        # Management + observability surfaces served directly by the proxy (never forwarded).
        if method == "GET" and parsed.path == "/metrics":
            self._send(200, "text/plain; version=0.0.4", obs.render_metrics().encode())
            return
        if method == "GET" and parsed.path == "/":
            self._send(200, "application/json", json.dumps({
                "nf": "scp",
                "role": "indirect SBI proxy (delegated discovery, TS 23.501 6.2.19 / TS 29.500 6.10)",
                "metrics": "/metrics", "status": "/scp/status",
                "howto": ("send any SBI request here with header "
                          "3gpp-Sbi-Discovery-Target-Nf-Type: <NFTYPE> (Model D)")}).encode())
            return
        if method == "GET" and parsed.path == "/scp/status":
            _scrape()
            forwarded = {lbls[0][1]: v for (n, lbls), v in _snapshot().items()
                         if n == "scp_forwarded_requests_total"}
            self._send(200, "application/json", json.dumps({
                "nf": "scp", "nfStatus": "REGISTERED", "nrf": NRF,
                "forwardedByTarget": forwarded}).encode())
            return

        # Everything else is an SBI request to be proxied (Model D / Model C).
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        status, content_type, data = proxy(method, parsed, self.headers, raw_body)
        self._send(status, content_type, data)

    def do_GET(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def _snapshot():
    with obs._lock:
        return dict(obs._counters)


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2, with scpInfo (TS 29.510 6.1.6.2.30) marking this as an
    # SCP so a consumer/NRF can discover the proxy. The SCP advertises no producer service of its
    # own — it is a router, not a resource holder.
    profile = {"nfType": "SCP", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "nscp-routing",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}],
               "scpInfo": {"scpPrefix": "/", "scpDomainList": ["default"]}}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("scp_rr_targets").set(len(_rr_cursor))


def serve():
    if not obs.initialized():
        obs.init("scp")
    obs.on_scrape(_scrape)
    bind = netconfig.bind_host()
    server = ThreadingHTTPServer((bind, PORT), Handler)
    obs.log("listening", addr=f"{bind}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    obs.init("scp")
    register_with_nrf()
    serve()
