"""
sbi_http: minimal JSON-over-HTTP adapter for 3GPP SBI-style interfaces.

Real SBI runs on HTTP/2 (TS 29.500 section 5.2). This adapter models SBI at the procedure level over
HTTP/1.1 with JSON bodies, per design stance 1 in docs/stack/end_to_end_stack_master_plan.txt (procedure-level
fidelity, not bit-level). Resource paths preserve the real 3GPP shapes so the learning transfers.

Stdlib only, no third-party dependencies: the whole stack must run with a bare python3.
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from domain import netconfig, obs


class Raw:
    """A response body that is NOT SBI JSON -- returned as-is with its own media type.

    Every NF here speaks JSON because every NF here talks to another NF. The
    evidence service is the exception: its reader is a PERSON, and an auditor
    who has to run `curl | jq` to see whether the evidence holds will not look
    at it. So it serves HTML on `/` and the identical facts as JSON underneath.

    Deliberately minimal -- no template engine, no static file tree, no third
    party anything. A handler returns Raw(text, content_type) and _reply sends
    it. The stdlib-only rule in CLAUDE.md is not negotiable for a two-line
    convenience.
    """

    __slots__ = ("text", "content_type")

    def __init__(self, text, content_type="text/html; charset=utf-8"):
        self.text = text
        self.content_type = content_type


class SbiApp:
    """Route table mapping (method, path pattern) to handler(params, query, body) -> (status, dict)."""

    def __init__(self, name):
        self.name = name
        self.routes = []

    def route(self, method, pattern):
        segments = [s for s in pattern.split("/") if s]

        def register(handler):
            self.routes.append((method, segments, handler))
            return handler

        return register

    def match(self, method, path):
        parts = [s for s in path.split("/") if s]
        for m, segments, handler in self.routes:
            if m != method or len(segments) != len(parts):
                continue
            params = {}
            matched = True
            for seg, part in zip(segments, parts):
                if seg.startswith("{") and seg.endswith("}"):
                    params[seg[1:-1]] = part
                elif seg != part:
                    matched = False
                    break
            if matched:
                return handler, params
        return None, None


def serve(app, port):
    """Blocking server loop for one network function."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            # Correlation (issue #27): adopt the inbound id for this handling thread, so
            # every obs.log() line and every synchronous southbound request() carries it.
            obs.set_corr(self.headers.get(obs.HEADER))
            # Distributed tracing (issue #27): open a SERVER span for this request, adopting an
            # inbound W3C `traceparent` or starting a trace derived from the corr id. Default-INERT:
            # obs.begin_server_span returns None when tracing is unconfigured, so this whole block
            # is a no-op and the request path stays byte-identical. Never touches the response body.
            span = obs.begin_server_span(self.headers.get(obs.TRACEPARENT),
                                         f"{method} {parsed.path}")
            try:
                self._serve(method, parsed)
            finally:
                if span is not None:
                    span.end()
                    obs.clear_trace_context()

        def _serve(self, method, parsed):
            # /metrics (issue #27): every sbi_http-served NF exposes its obs registry in
            # Prometheus text exposition format, no per-service route needed.
            if method == "GET" and parsed.path == "/metrics":
                self._reply_text(200, obs.render_metrics())
                return
            # Root index: every NF answers "/" with what it is and the routes it serves,
            # so a browser or curl hitting a bare host:port gets a map, not a 404.
            #
            # FALLBACK ONLY. A service that registers its own "GET /" wins -- this
            # used to fire unconditionally and silently shadowed the evidence
            # page's route, returning the route map with a 200 so nothing looked
            # broken. An NF that declares a root handler means it.
            if (method == "GET" and parsed.path == "/"
                    and app.match("GET", "/")[0] is None):
                self._reply(200, {
                    "nf": app.name,
                    "metrics": "/metrics",
                    "routes": sorted({f"{m} /" + "/".join(seg) for m, seg, _ in app.routes}),
                })
                return
            handler, params = app.match(method, parsed.path)
            if handler is None:
                self._reply(*problem(404, "Not Found", detail=parsed.path))
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                # OAuth-style endpoints (the osac_stub Keycloak fixture) POST
                # application/x-www-form-urlencoded; hand those to the handler as a flat
                # dict instead of killing the connection with a JSON traceback.
                body = {k: v[0] for k, v in parse_qs(raw.decode(errors="replace")).items()}
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            status, reply = handler(params, query, body)
            self._reply(status, reply)

        def _reply(self, status, payload):
            if isinstance(payload, Raw):
                data = payload.text.encode()
                self.send_response(status)
                self.send_header("Content-Type", payload.content_type)
                self._finish(data)
                return
            data = json.dumps(payload).encode()
            self.send_response(status)
            # SBI error responses are ProblemDetails with their own media type (TS 29.500 5.2.7)
            content_type = "application/problem+json" if status >= 400 else "application/json"
            self.send_header("Content-Type", content_type)
            self._finish(data)

        def _reply_text(self, status, text):
            data = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self._finish(data)

        def _finish(self, data):
            corr = obs.get_corr()
            if corr:
                self.send_header(obs.HEADER, corr)   # echo the correlation id back
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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

    if not obs.initialized():
        obs.init(app.name)   # fallback so every served NF logs with a sensible nf field
    bind = netconfig.bind_host()
    server = ThreadingHTTPServer((bind, port), Handler)
    obs.log("listening", addr=f"{bind}:{port}")
    server.serve_forever()


def problem(status, title, detail=None, cause=None):
    """ProblemDetails error body per TS 29.500 section 5.2.7. Returns (status, dict) for handlers."""
    body = {"title": title, "status": status}
    if detail is not None:
        body["detail"] = detail
    if cause is not None:
        body["cause"] = cause
    return status, body


def request(method, url, body=None, headers=None):
    """JSON client. Returns (status, dict). HTTP errors return their status; connection errors raise.
    Propagates the thread's correlation id as X-Correlation-Id (issue #27) when one is bound.

    `headers` is an OPTIONAL dict of extra request headers (default None -> byte-identical to the
    pre-existing two-arg call: only Content-Type + the correlation id are sent). It exists so the
    opt-in SCP-routing path below can attach the 3gpp-Sbi-Discovery-* delegated-discovery headers;
    every existing caller passes no headers and is completely unaffected."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    corr = obs.get_corr()
    if corr:
        hdrs[obs.HEADER] = corr
    # Distributed tracing (issue #27): open a child CLIENT span and stamp its own `traceparent`
    # so the producer nests one trace-id below this call. Default-INERT: begin_client_span returns
    # (None, None) when tracing is unconfigured, so no header is added and the wire bytes are
    # byte-identical to the pre-tracing call. Runs BEFORE `headers` so an explicit override wins.
    span, traceparent = obs.begin_client_span(f"{method} {urlparse(url).path}")
    if traceparent:
        hdrs[obs.TRACEPARENT] = traceparent
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=int(os.environ.get("SBI_HTTP_TIMEOUT", "20"))) as resp:  # 2026-07-20: real-core (kubectl) provisioning needs >5s
            status, payload = resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    except Exception:
        if span is not None:
            span.end(ok=False, error=True)
        raise
    if span is not None:
        span.end(ok=status < 500, http_status=status)
    return status, payload


# ===================================================================================
# OPT-IN indirect-SBI routing through the SCP (3GPP Communication Model D, TS 29.500 6.10).
# ===================================================================================
# This block adds a NEW outbound helper, discover_and_request(): the discover-then-call path an
# NF consumer runs to reach a producer BY NF-TYPE. By DEFAULT it behaves exactly like the inline
# "discover in the NRF, resolve an apiRoot, call the producer directly" that every owned NF does
# today (Model B). When (and ONLY when) env TELCO_SCP_ROUTE=1 AND an SCP is discoverable in the
# NRF, the SAME request is sent THROUGH the SCP instead (Model D): the consumer POSTs/GETs the SCP
# carrying the target NF-type in the 3gpp-Sbi-Discovery-* headers, and the SCP does delegated
# discovery + forwards + returns the producer response transparently. The (status, dict) the caller
# receives is the producer's own answer either way, so a caller cannot tell direct from indirect.
#
# INERTNESS CONTRACT (make-or-break): nothing above this line changed behaviour (request() gained an
# optional headers kwarg that defaults to the old wire bytes). This helper is NEW code that no
# existing NF or spec calls, and even a caller that DOES use it takes the byte-identical direct path
# unless the operator opts in with TELCO_SCP_ROUTE=1 AND an SCP is actually registered. Default OFF.

# The SCP's exact delegated-discovery header contract (TS 29.500 5.2.3.2; mirrors services/core/scp).
_HDR_DISC_TARGET = "3gpp-Sbi-Discovery-Target-Nf-Type"
_HDR_DISC_SERVICE = "3gpp-Sbi-Discovery-Service-Names"
_HDR_DISC_REQUESTER = "3gpp-Sbi-Discovery-Requester-Nf-Type"


def scp_routing_enabled():
    """True iff the operator opted into indirect-SBI routing (env TELCO_SCP_ROUTE=1). Default OFF."""
    return os.environ.get("TELCO_SCP_ROUTE") == "1"


def _instance_api_root(instance):
    """Resolve a discovered nfInstance to its SBI apiRoot (scheme://host:port), or None."""
    for svc in instance.get("nfServices") or []:
        for ep in svc.get("ipEndPoints") or []:
            if ep.get("port"):
                host = ep.get("ipv4Address") or (instance.get("ipv4Addresses") or ["127.0.0.1"])[0]
                return f"http://{host}:{ep['port']}"
    return None


def _discover(nrf_url, target_nf_type, requester_nf_type, service_names=None):
    """Nnrf_NFDiscovery (TS 29.510 5.3): REGISTERED instances of target_nf_type, or [] on any miss."""
    q = (f"{nrf_url}/nnrf-disc/v1/nf-instances?target-nf-type={target_nf_type}"
         f"&requester-nf-type={requester_nf_type}")
    if service_names:
        q += f"&service-names={service_names}"
    try:
        status, body = request("GET", q)
    except OSError:
        return []
    if status != 200:
        return []
    return [i for i in body.get("nfInstances", [])
            if i.get("nfStatus", "REGISTERED") == "REGISTERED"]


def discover_and_request(method, resource_path, target_nf_type, nrf_url,
                         requester_nf_type="NF", service_names=None, body=None):
    """Outbound SBI call along the discover-then-call path, with OPT-IN indirect SCP routing.

    Args mirror what an NF already has in hand: the SBI `method` + `resource_path` (+ optional
    `body`), the producer `target_nf_type` to reach, the `nrf_url`, and the consumer's own
    `requester_nf_type`. Returns (status, dict); raises on connection error, exactly like request().

    DEFAULT / fallback (direct, Model B) — env unset OR no SCP registered:
        discover target_nf_type in the NRF, resolve the first instance's apiRoot, call it directly.
        This is byte-for-byte the inline discover-then-call every owned NF runs today.

    OPT-IN (indirect, Model D) — TELCO_SCP_ROUTE=1 AND an SCP discoverable in the NRF:
        send the SAME method/path/body to the SCP with the target NF-type in the delegated-discovery
        headers; the SCP discovers + forwards + returns the producer response transparently. A
        consumer-side counter (sbi_scp_routed_requests_total) records each routed request."""
    if scp_routing_enabled():
        scp = _discover(nrf_url, "SCP", requester_nf_type)
        scp_base = _instance_api_root(scp[0]) if scp else None
        if scp_base:
            hdrs = {_HDR_DISC_TARGET: target_nf_type, _HDR_DISC_REQUESTER: requester_nf_type}
            if service_names:
                hdrs[_HDR_DISC_SERVICE] = service_names
            obs.counter("sbi_scp_routed_requests_total", target=target_nf_type.upper()).inc()
            obs.log("sbi_routed_via_scp", target=target_nf_type, path=resource_path, scp=scp_base)
            return request(method, scp_base + resource_path, body, headers=hdrs)
    # DIRECT path (default, and the exact fallback when no SCP is registered).
    instances = _discover(nrf_url, target_nf_type, requester_nf_type, service_names)
    if not instances:
        raise LookupError(f"NRF has no {target_nf_type}")
    base = _instance_api_root(instances[0])
    if base is None:
        raise LookupError(f"{target_nf_type} advertises no SBI endpoint")
    return request(method, base + resource_path, body)
